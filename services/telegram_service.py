"""Operações na API do Telegram usadas pelo bot (entrega e controle de grupos).

As funções de banco são síncronas; aqui elas são chamadas com
asyncio.to_thread para não bloquear o event loop do bot.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

from config import settings
from database import session_scope
from models import ContentType
from services import access_service, texts
from services.access_service import DeliveryPackage, log_event

logger = logging.getLogger(__name__)


async def _record(action: str, **kwargs) -> None:
    def _write():
        with session_scope() as session:
            log_event(session, action, **kwargs)

    await asyncio.to_thread(_write)


async def safe_send(bot: Bot, chat_id: int, text: str, reply_markup=None) -> bool:
    """Envia mensagem tratando usuário que bloqueou o bot e flood control."""
    for _ in range(2):
        try:
            await bot.send_message(
                chat_id, text, parse_mode=ParseMode.HTML, reply_markup=reply_markup, disable_web_page_preview=True
            )
            return True
        except RetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 1)
        except Forbidden:
            logger.info("Usuário %s bloqueou o bot", chat_id)
            return False
        except TelegramError as exc:
            logger.warning("Falha ao enviar mensagem para %s: %s", chat_id, exc)
            return False
    return False


async def create_invite_link(bot: Bot, chat_id: int, telegram_id: int, name: str) -> str:
    """Convite individual: uso único e com validade."""
    # Se o usuário foi removido antes (ban+unban), garante que pode voltar.
    try:
        await bot.unban_chat_member(chat_id, telegram_id, only_if_banned=True)
    except TelegramError:
        pass
    invite = await bot.create_chat_invite_link(
        chat_id=chat_id,
        name=name[:32],
        expire_date=datetime.now(timezone.utc) + timedelta(hours=settings.invite_link_ttl_hours),
        member_limit=1,
    )
    return invite.invite_link


async def deliver_package(bot: Bot, package: DeliveryPackage, *, resend: bool = False) -> list[str]:
    """Envia o conteúdo liberado. Retorna a lista de erros por item."""
    errors: list[str] = []
    chat = package.telegram_id

    if not resend:
        await safe_send(bot, chat, texts.payment_approved(package.plan_name, package.expires_at))
    if not package.items:
        await safe_send(bot, chat, texts.NO_ITEMS_YET)
        return errors

    current_category = None
    for item in package.items:
        try:
            if item.category and item.category != current_category:
                current_category = item.category
                await safe_send(bot, chat, f"📂 <b>{texts.escape(item.category)}</b>")

            value = item.content_value.strip()
            if item.content_type == ContentType.TELEGRAM_CHAT:
                link = await create_invite_link(bot, int(value), chat, f"sub{package.subscription_id}")
                await safe_send(bot, chat, texts.invite_message(item.name, link, settings.invite_link_ttl_hours))
                await _record("invite_created", user_id=package.user_id, product_id=item.product_id,
                              subscription_id=package.subscription_id, chat_id=value)
                continue
            if item.content_type == ContentType.LINK:
                markup = InlineKeyboardMarkup([[InlineKeyboardButton(f"🔗 Acessar {item.name}"[:60], url=value)]])
                await safe_send(bot, chat, f"🔗 <b>{texts.escape(item.name)}</b>", reply_markup=markup)
            elif item.content_type == ContentType.TEXT:
                await safe_send(bot, chat, f"📄 <b>{texts.escape(item.name)}</b>\n\n{texts.escape(value)}")
            elif item.content_type == ContentType.FILE:
                await bot.send_document(chat, document=value, caption=item.name[:1024], protect_content=True)
            elif item.content_type == ContentType.PHOTO:
                await bot.send_photo(chat, photo=value, caption=item.name[:1024], protect_content=True)
            elif item.content_type == ContentType.VIDEO:
                await bot.send_video(chat, video=value, caption=item.name[:1024], protect_content=True)
            await _record("product_sent", user_id=package.user_id, product_id=item.product_id,
                          subscription_id=package.subscription_id, resend=resend)
        except Forbidden:
            errors.append("usuário bloqueou o bot")
            break
        except (TelegramError, ValueError) as exc:
            logger.error("Falha ao entregar produto %s ao usuário %s: %s", item.product_id, chat, exc)
            errors.append(f"{item.name}: {exc}")
    return errors


async def remove_from_chat(bot: Bot, chat_id: int, telegram_id: int) -> str | None:
    """Remove (kick) o usuário do grupo/canal: ban seguido de unban.

    O unban permite que ele volte no futuro com um novo convite após renovar.
    Retorna None em caso de sucesso ou a mensagem de erro.
    """
    try:
        member = await bot.get_chat_member(chat_id, telegram_id)
        if member.status in ("creator", "administrator"):
            return "usuário é administrador do chat"
        if member.status in ("left", "kicked"):
            return None
        await bot.ban_chat_member(chat_id, telegram_id)
        await bot.unban_chat_member(chat_id, telegram_id, only_if_banned=True)
        return None
    except BadRequest as exc:
        msg = str(exc).lower()
        if "user not found" in msg or "participant_id_invalid" in msg or "member not found" in msg:
            return None
        return str(exc)
    except TelegramError as exc:
        return str(exc)


# --------------------------------------------------------------------------- #
# Rotinas periódicas (chamadas pelo JobQueue do bot)
# --------------------------------------------------------------------------- #
def renew_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("💎 Ver planos", callback_data="menu:plans")]])


async def process_pending_deliveries(bot: Bot) -> int:
    def _claim():
        with session_scope() as session:
            return access_service.claim_orders_for_delivery(session)

    packages = await asyncio.to_thread(_claim)
    for package in packages:
        try:
            errors = await deliver_package(bot, package)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Erro inesperado na entrega do pedido %s", package.order_id)

            def _fail(order_id=package.order_id, err=str(exc)):
                with session_scope() as session:
                    access_service.mark_delivery_failed(session, order_id, err)

            await asyncio.to_thread(_fail)
            continue

        def _done(order_id=package.order_id, errs=errors):
            with session_scope() as session:
                access_service.mark_delivered(session, order_id, errs)

        await asyncio.to_thread(_done)
    return len(packages)


async def process_expirations(bot: Bot) -> None:
    def _expire_and_list():
        with session_scope() as session:
            access_service.expire_due_subscriptions(session)
        with session_scope() as session:
            return access_service.pending_removals(session)

    tasks = await asyncio.to_thread(_expire_and_list)
    for task in tasks:
        removed, errors = [], []
        for chat_id in task.chat_ids:
            error = await remove_from_chat(bot, chat_id, task.telegram_id)
            if error:
                errors.append(f"{chat_id}: {error}")
                logger.warning("Não foi possível remover %s do chat %s: %s", task.telegram_id, chat_id, error)
            else:
                removed.append(chat_id)

        revoked = task.status.value == "revoked"
        await safe_send(bot, task.telegram_id, texts.access_ended(task.plan_name, revoked),
                        reply_markup=None if revoked else renew_keyboard())

        def _mark(sub_id=task.subscription_id, rem=removed, errs=errors):
            with session_scope() as session:
                access_service.mark_access_removed(session, sub_id, rem, errs)

        await asyncio.to_thread(_mark)


async def process_reminders(bot: Bot) -> None:
    def _list():
        with session_scope() as session:
            return access_service.due_reminders(session)

    for task in await asyncio.to_thread(_list):
        await safe_send(bot, task.telegram_id, texts.expiry_reminder(task.plan_name, task.expires_at),
                        reply_markup=renew_keyboard())

        def _mark(sub_id=task.subscription_id):
            with session_scope() as session:
                access_service.mark_reminder_sent(session, sub_id)

        await asyncio.to_thread(_mark)
