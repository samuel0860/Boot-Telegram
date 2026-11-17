"""Bot do Telegram (processo separado do backend web).

Execução:  python bot.py
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import io
import logging
import time

from telegram import (
    BotCommand,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import ConfigError, configure_logging, settings
from database import check_connection, session_scope
from models import AccessType, OrderStatus
from payments import PaymentProviderError, get_payment_client
from services import (
    access_service,
    alert_service,
    converter_service,
    catalog_service,
    payment_service,
    promo_service,
    settings_service,
    telegram_service,
    texts,
    user_service,
    whatsapp_service,
)

logger = logging.getLogger("bot")

CHECK_COOLDOWN_SECONDS = 10


# --------------------------------------------------------------------------- #
# Teclados
# --------------------------------------------------------------------------- #
def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📚 Ver conteúdo disponível", callback_data="menu:content")],
            [InlineKeyboardButton("💎 Conhecer os planos", callback_data="menu:plans")],
            [InlineKeyboardButton("🛒 Comprar acesso", callback_data="menu:buy")],
            [InlineKeyboardButton("🔑 Meus acessos", callback_data="menu:access")],
            [InlineKeyboardButton("💬 Suporte", callback_data="menu:support")],
        ]
    )


def back_button() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton("⬅️ Voltar ao menu", callback_data="menu:home")]


def plans_keyboard(plans: list[catalog_service.PlanView]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"🛒 {p.name} — {texts.brl(p.price)}"[:64], callback_data=f"buy:{p.id}")]
        for p in plans
    ]
    rows.append(back_button())
    return InlineKeyboardMarkup(rows)


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
async def run_db(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


async def reply(update: Update, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Responde editando a mensagem do botão (quando possível) ou enviando nova."""
    query = update.callback_query
    if query is not None and query.message is not None:
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup,
                                          disable_web_page_preview=True)
            return
        except TelegramError:
            pass  # mensagem com foto/igual -> envia nova
    await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML, reply_markup=markup,
                                             disable_web_page_preview=True)


async def ensure_allowed(update: Update) -> bool:
    user = update.effective_user
    if user is None or update.effective_chat is None or update.effective_chat.type != ChatType.PRIVATE:
        return False
    if await run_db(user_service.is_blocked, user.id):
        await update.effective_chat.send_message(texts.BLOCKED)
        return False
    return True


def plans_text(plans: list[catalog_service.PlanView]) -> str:
    lines = ["💎 <b>Nossos planos</b>\n"]
    for p in plans:
        kind = "📦 Pack" if p.access_type == AccessType.ONE_TIME else "🔁 Assinatura"
        lines.append(f"<b>{texts.escape(p.name)}</b> — <b>{texts.brl(p.price)}</b>")
        lines.append(f"{kind} · ⏳ {p.duration_label}")
        if p.description:
            lines.append(texts.escape(p.description))
        if p.product_names:
            lines.append("Inclui: " + ", ".join(texts.escape(n) for n in p.product_names))
        lines.append("")
    lines.append("👇 Toque em uma oferta para gerar o PIX.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Comandos
# --------------------------------------------------------------------------- #
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    if tg is None or update.effective_chat.type != ChatType.PRIVATE:
        return
    source = context.args[0] if context.args else None
    info = await run_db(
        user_service.register_telegram_user,
        telegram_id=tg.id,
        username=tg.username,
        first_name=tg.first_name,
        last_name=tg.last_name,
        language_code=tg.language_code,
        source=source,
    )
    if info.is_blocked:
        await update.effective_chat.send_message(texts.BLOCKED)
        return
    logger.info("/start de %s (novo=%s, origem=%s)", tg.id, info.is_new, source)
    await update.effective_chat.send_message(
        texts.welcome(tg.first_name, info.is_new), parse_mode=ParseMode.HTML, reply_markup=main_menu()
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update):
        return
    await update.effective_chat.send_message(
        "ℹ️ <b>Comandos</b>\n/start — menu principal\n/planos — ver planos\n/meusacessos — seus acessos\n/suporte — ajuda",
        parse_mode=ParseMode.HTML,
    )


async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, "🏠 <b>Menu principal</b>\nEscolha uma opção:", main_menu())


async def show_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    catalog = await run_db(catalog_service.list_catalog)
    if not catalog:
        await reply(update, texts.NO_CONTENT, InlineKeyboardMarkup([back_button()]))
        return
    lines = ["📚 <b>Conteúdo disponível</b>\n"]
    for cat in catalog:
        lines.append(f"📂 <b>{texts.escape(cat.name)}</b>")
        if cat.description:
            lines.append(f"<i>{texts.escape(cat.description)}</i>")
        for name, desc in cat.products:
            lines.append(f"  • {texts.escape(name)}" + (f" — {texts.escape(desc)}" if desc else ""))
        lines.append("")
    lines.append("🔒 O conteúdo é liberado automaticamente após a confirmação do pagamento.")
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("💎 Ver planos", callback_data="menu:plans")], back_button()])
    await reply(update, "\n".join(lines), markup)


async def show_plans(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    plans = await run_db(catalog_service.list_active_plans)
    if not plans:
        await reply(update, texts.NO_PLANS, InlineKeyboardMarkup([back_button()]))
        return
    await reply(update, plans_text(plans), plans_keyboard(plans))


async def show_buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    plans = await run_db(catalog_service.list_active_plans)
    if not plans:
        await reply(update, texts.NO_PLANS, InlineKeyboardMarkup([back_button()]))
        return
    await reply(update, "🛒 <b>Comprar acesso</b>\n\nEscolha a oferta desejada:", plans_keyboard(plans))


async def show_support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, texts.support(), InlineKeyboardMarkup([back_button()]))


async def show_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    subs = await run_db(catalog_service.list_user_subscriptions, update.effective_user.id)
    if not subs:
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("💎 Ver planos", callback_data="menu:plans")], back_button()])
        await reply(update, "🔑 Você ainda não possui acessos. Que tal conhecer nossos planos?", markup)
        return
    lines = ["🔑 <b>Meus acessos</b>\n"]
    rows = []
    for s in subs:
        if s.valid:
            state = "✅ ativo"
        elif s.status == "active":
            state = "🕒 agendado (renovação)"
        elif s.status == "revoked":
            state = "⛔ encerrado"
        else:
            state = "⌛ expirado"
        lines.append(f"<b>{texts.escape(s.plan_name)}</b> — {state}\nValidade: {texts.fmt_dt(s.expires_at)}\n")
        if s.valid:
            rows.append([InlineKeyboardButton(f"📥 Receber: {s.plan_name}"[:64], callback_data=f"resend:{s.id}")])
    rows.append(back_button())
    await reply(update, "\n".join(lines), InlineKeyboardMarkup(rows))


# --------------------------------------------------------------------------- #
# Compra / PIX
# --------------------------------------------------------------------------- #
async def buy_plan(update: Update, context: ContextTypes.DEFAULT_TYPE, plan_id: int) -> None:
    chat = update.effective_chat
    await chat.send_message("⏳ Gerando seu PIX, aguarde...")
    try:
        result = await run_db(
            payment_service.create_pix_checkout, update.effective_user.id, plan_id, get_payment_client()
        )
    except payment_service.CheckoutError as exc:
        await chat.send_message(f"⚠️ {texts.escape(str(exc))}", parse_mode=ParseMode.HTML,
                                reply_markup=InlineKeyboardMarkup([back_button()]))
        return

    caption = texts.pix_caption(result.plan_name, result.amount, result.expires_at, result.order_public_id)
    if result.reused:
        caption = "♻️ Você já tem um PIX pendente para esta oferta:\n\n" + caption
    sent_photo = False
    if result.qr_code_base64:
        try:
            image = io.BytesIO(base64.b64decode(result.qr_code_base64))
            image.name = "pix.png"
            await chat.send_photo(photo=image, caption=caption, parse_mode=ParseMode.HTML)
            sent_photo = True
        except (binascii.Error, TelegramError) as exc:
            logger.warning("Não foi possível enviar a imagem do QR Code: %s", exc)
    if not sent_photo:
        await chat.send_message(caption, parse_mode=ParseMode.HTML)

    buttons = [
        [InlineKeyboardButton("✅ Já paguei — verificar", callback_data=f"check:{result.order_public_id}")],
        [InlineKeyboardButton("❌ Cancelar pedido", callback_data=f"cancel:{result.order_public_id}")],
    ]
    if result.ticket_url:
        buttons.insert(1, [InlineKeyboardButton("🌐 Abrir página do PIX", url=result.ticket_url)])
    await chat.send_message(texts.pix_copy_paste(result.qr_code), parse_mode=ParseMode.HTML,
                            reply_markup=InlineKeyboardMarkup(buttons))


async def check_payment(update: Update, context: ContextTypes.DEFAULT_TYPE, order_public_id: str) -> None:
    """Consulta o status REAL no provedor. Nunca libera só pelo clique."""
    now = time.monotonic()
    last = context.user_data.get("last_check", 0.0)
    if now - last < CHECK_COOLDOWN_SECONDS:
        await update.callback_query.answer("Aguarde alguns segundos antes de verificar novamente.", show_alert=True)
        return
    context.user_data["last_check"] = now
    try:
        status = await run_db(
            payment_service.reconcile_order, order_public_id, get_payment_client(), update.effective_user.id
        )
    except PaymentProviderError:
        await update.effective_chat.send_message("⚠️ Não foi possível consultar o pagamento agora. Tente em instantes.")
        return
    if status is None:
        await update.effective_chat.send_message("⚠️ Pedido não encontrado.")
    elif status == OrderStatus.PAID:
        await update.effective_chat.send_message("✅ Pagamento confirmado! Liberando seu acesso...")
        await telegram_service.process_pending_deliveries(context.bot)
    elif status in (OrderStatus.CANCELLED, OrderStatus.EXPIRED, OrderStatus.FAILED):
        await update.effective_chat.send_message(
            "⚠️ Este pedido não está mais ativo. Se você já pagou, aguarde: pagamentos confirmados são "
            "reconhecidos automaticamente. Para comprar novamente, escolha um plano.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 Ver planos", callback_data="menu:plans")]]),
        )
    elif status == OrderStatus.REFUNDED:
        await update.effective_chat.send_message("ℹ️ Este pagamento foi estornado.")
    else:
        await update.effective_chat.send_message(texts.PAYMENT_PENDING)


async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE, order_public_id: str) -> None:
    ok = await run_db(payment_service.cancel_pending_order, update.effective_user.id, order_public_id)
    msg = "❌ Pedido cancelado." if ok else "ℹ️ Este pedido não pode mais ser cancelado."
    await update.effective_chat.send_message(msg, reply_markup=InlineKeyboardMarkup([back_button()]))


async def resend_content(update: Update, context: ContextTypes.DEFAULT_TYPE, subscription_id: int) -> None:
    now = time.monotonic()
    if now - context.user_data.get("last_resend", 0.0) < 60:
        await update.callback_query.answer("Aguarde 1 minuto para solicitar novamente.", show_alert=True)
        return
    context.user_data["last_resend"] = now

    def _package():
        with session_scope() as session:
            return access_service.package_for_resend(session, update.effective_user.id, subscription_id)

    package = await run_db(_package)
    if package is None:
        await update.effective_chat.send_message("⚠️ Este acesso não está ativo.")
        return
    await telegram_service.deliver_package(context.bot, package, resend=True)


# --------------------------------------------------------------------------- #
# Roteador de botões
# --------------------------------------------------------------------------- #
MENU_ACTIONS = {
    "home": show_menu,
    "content": show_content,
    "plans": show_plans,
    "buy": show_buy,
    "support": show_support,
    "access": show_access,
}


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await ensure_allowed(update):
        await query.answer()
        return
    data = query.data or ""
    action, _, arg = data.partition(":")
    # check/resend podem responder com alerta (cooldown); os demais respondem já.
    if action not in ("check", "resend", "conv"):
        await query.answer()
    if action == "menu" and arg in MENU_ACTIONS:
        await MENU_ACTIONS[arg](update, context)
    elif action == "buy" and arg.isdigit():
        await buy_plan(update, context, int(arg))
    elif action == "check":
        await check_payment(update, context, arg)
    elif action == "cancel":
        await cancel_order(update, context, arg)
    elif action == "resend" and arg.isdigit():
        await resend_content(update, context, int(arg))
    elif action == "conv":
        await on_converter_action(update, context, arg)
    if action in ("check", "resend"):
        try:
            await query.answer()
        except TelegramError:
            pass  # já respondido com alerta


async def cmd_plans(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await ensure_allowed(update):
        await show_plans(update, context)


async def cmd_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await ensure_allowed(update):
        await show_access(update, context)


async def cmd_support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await ensure_allowed(update):
        await show_support(update, context)


async def fallback_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await ensure_allowed(update):
        await update.effective_chat.send_message("Use o menu abaixo 👇", reply_markup=main_menu())


# --------------------------------------------------------------------------- #
# Ferramentas para administradores
# (IDs vinculados pelo painel com /vincular, ou em ADMIN_TELEGRAM_IDS no .env)
# --------------------------------------------------------------------------- #
_ADMIN_CACHE: dict[str, object] = {"ids": set(), "at": 0.0}


async def get_admin_ids(force: bool = False) -> set[int]:
    if force or time.monotonic() - float(_ADMIN_CACHE["at"]) > 30:
        _ADMIN_CACHE["ids"] = await run_db(alert_service.admin_ids)
        _ADMIN_CACHE["at"] = time.monotonic()
    return _ADMIN_CACHE["ids"]  # type: ignore[return-value]


async def is_admin(update: Update) -> bool:
    user = update.effective_user
    return user is not None and user.id in await get_admin_ids()


async def admin_file_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin envia um arquivo/foto/vídeo ao bot e recebe o file_id para cadastrar no painel."""
    if not await is_admin(update):
        return
    msg = update.effective_message
    if msg.document:
        kind, file_id = "file", msg.document.file_id
    elif msg.video:
        kind, file_id = "video", msg.video.file_id
    elif msg.photo:
        kind, file_id = "photo", msg.photo[-1].file_id
    else:
        return
    await msg.reply_text(
        f"🗂 Tipo de conteúdo: <b>{kind}</b>\nfile_id (copie para o painel):\n<code>{file_id}</code>",
        parse_mode=ParseMode.HTML,
    )


async def admin_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/chatid — mostra o ID do grupo/canal (use dentro do grupo)."""
    if not await is_admin(update):
        return
    chat = update.effective_chat
    await update.effective_message.reply_text(
        f"ID deste chat: <code>{chat.id}</code>\nTipo: {chat.type}", parse_mode=ParseMode.HTML
    )


async def channel_post_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Em canais, poste '/chatid' — o bot responde com o ID (o autor não é identificado em canais)."""
    post = update.channel_post
    if post and post.text and post.text.strip().startswith("/chatid"):
        await post.reply_text(f"ID deste canal: <code>{post.chat.id}</code>", parse_mode=ParseMode.HTML)


# --------------------------------------------------------------------------- #
# Controle de entrada em grupos/canais privados
# --------------------------------------------------------------------------- #
def _joined(update: ChatMemberUpdated) -> bool:
    old, new = update.old_chat_member.status, update.new_chat_member.status
    was_in = old in (ChatMemberStatus.MEMBER, ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR) or (
        old == ChatMemberStatus.RESTRICTED and update.old_chat_member.is_member
    )
    is_in = new in (ChatMemberStatus.MEMBER, ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR) or (
        new == ChatMemberStatus.RESTRICTED and update.new_chat_member.is_member
    )
    return is_in and not was_in


async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cmu = update.chat_member
    if cmu is None or not _joined(cmu):
        return
    member = cmu.new_chat_member.user
    chat_id = cmu.chat.id
    if member.is_bot or member.id in settings.admin_telegram_ids:
        return
    if cmu.new_chat_member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        return

    def _check():
        with session_scope() as session:
            if chat_id not in access_service.managed_chat_ids(session):
                return None
            return access_service.is_authorized_for_chat(session, member.id, chat_id)

    authorized = await run_db(_check)
    if authorized is None:
        return  # chat não gerenciado pelo sistema
    user_id = await run_db(user_service.get_user_id, member.id)
    if authorized:
        await telegram_service._record("joined_chat", user_id=user_id, chat_id=chat_id, telegram_id=member.id)
        return
    error = await telegram_service.remove_from_chat(context.bot, chat_id, member.id)
    await telegram_service._record(
        "unauthorized_join_removed", user_id=user_id, chat_id=chat_id, telegram_id=member.id, error=error
    )
    logger.warning("Entrada não autorizada removida: usuário %s no chat %s (erro=%s)", member.id, chat_id, error)


# --------------------------------------------------------------------------- #
# Jobs periódicos
# --------------------------------------------------------------------------- #
async def job_deliveries(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await telegram_service.process_pending_deliveries(context.bot)
    except Exception:  # noqa: BLE001
        logger.exception("Erro no job de entregas")


async def job_reconcile(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await run_db(payment_service.reconcile_pending_orders, get_payment_client())
    except Exception:  # noqa: BLE001
        logger.exception("Erro no job de reconciliação")


async def job_expirations(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await telegram_service.process_expirations(context.bot)
        await telegram_service.process_reminders(context.bot)
    except Exception:  # noqa: BLE001
        logger.exception("Erro no job de expiração")


# --------------------------------------------------------------------------- #
# Bot de promoções (Mercado Livre -> canal)
# --------------------------------------------------------------------------- #
_promo_cycle_running = False


async def job_promotions(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dispara o ciclo de coleta no intervalo configurado e publica a fila no canal."""
    global _promo_cycle_running
    try:
        def _load():
            with session_scope() as session:
                values = settings_service.get_all(session)
            return values, settings_service.promo_config(values)

        values, cfg = await run_db(_load)
        if not _promo_cycle_running and promo_service.is_cycle_due(values, cfg):
            _promo_cycle_running = True

            async def _cycle():
                global _promo_cycle_running
                try:
                    await run_db(promo_service.run_cycle, triggered_by="bot")
                except Exception:  # noqa: BLE001
                    logger.exception("Erro no ciclo de promoções")
                finally:
                    _promo_cycle_running = False

            context.application.create_task(_cycle(), name="promo_cycle")

        job = await run_db(promo_service.claim_next_post)
        if job is None:
            return
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Ver oferta", url=job.link)]])
        target = promo_service.channel_target(job.channel_id)
        message_id, error = None, None
        try:
            if job.image_url:
                try:
                    msg = await context.bot.send_photo(target, photo=job.image_url, caption=job.caption,
                                                       parse_mode=ParseMode.HTML, reply_markup=markup)
                except TelegramError as exc:
                    if "chat not found" in str(exc).lower() or "rights" in str(exc).lower():
                        raise
                    msg = await context.bot.send_message(target, job.caption, parse_mode=ParseMode.HTML,
                                                         reply_markup=markup)
            else:
                msg = await context.bot.send_message(target, job.caption, parse_mode=ParseMode.HTML,
                                                     reply_markup=markup)
            message_id = msg.message_id
        except TelegramError as exc:
            error = f"Telegram: {exc}"
            logger.warning("Falha ao publicar oferta %s no canal %s: %s", job.offer_id, job.channel_id, exc)
        await run_db(promo_service.finish_post, job.offer_id, message_id, error)
    except Exception:  # noqa: BLE001
        logger.exception("Erro no job de promoções")


_wa_running = False


async def job_whatsapp(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Envia ofertas da fila para os grupos do WhatsApp, respeitando os limites."""
    global _wa_running
    if _wa_running:
        return
    try:
        job, reason = await run_db(whatsapp_service.claim_next)
    except Exception:  # noqa: BLE001
        logger.exception("Erro ao consultar a fila do WhatsApp")
        return
    if job is None:
        return
    _wa_running = True

    async def _send():
        global _wa_running
        try:
            def _work():
                import time as _t
                with whatsapp_service.default_client() as client:
                    return whatsapp_service.send_job(job, client, _t.sleep)

            try:
                sent, errors = await run_db(_work)
            except whatsapp_service.WhatsAppError as exc:
                sent, errors = [], [str(exc)]
            await run_db(whatsapp_service.finish, job.offer_id, sent, errors)
            if errors:
                logger.warning("WhatsApp: oferta %s enviada a %d grupo(s), erros: %s",
                               job.offer_id, len(sent), errors[:3])
            else:
                logger.info("WhatsApp: oferta %s enviada a %d grupo(s)", job.offer_id, len(sent))
        except Exception:  # noqa: BLE001
            logger.exception("Erro no envio para o WhatsApp")
        finally:
            _wa_running = False

    context.application.create_task(_send(), name="whatsapp_send")


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/id — mostra o seu ID do Telegram (use em ADMIN_TELEGRAM_IDS)."""
    await update.effective_message.reply_text(
        f"🆔 Seu ID do Telegram: <code>{update.effective_user.id}</code>", parse_mode=ParseMode.HTML
    )


async def admin_forwarded(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin encaminha um post do canal para o bot -> recebe o ID do canal."""
    if not await is_admin(update):
        await fallback_text(update, context)
        return
    origin = update.effective_message.forward_origin
    chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
    if chat is None:
        await update.effective_message.reply_text("Encaminhe uma mensagem de um CANAL para descobrir o ID dele.")
        return
    await update.effective_message.reply_text(
        f"📢 Canal: <b>{texts.escape(chat.title or '')}</b>\nID: <code>{chat.id}</code>\n\n"
        "Cole esse ID no painel → Promoções → Canal.", parse_mode=ParseMode.HTML
    )


# --------------------------------------------------------------------------- #
# Vínculo do Telegram do administrador
# --------------------------------------------------------------------------- #
async def cmd_vincular(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/vincular CÓDIGO — liga este Telegram ao painel (alertas, resumo e conversor)."""
    code = context.args[0] if context.args else ""
    user = update.effective_user
    ok = await run_db(alert_service.redeem_link_code, code, user.id, user.full_name or "")
    if not ok:
        await update.effective_message.reply_text(
            "❌ Código inválido ou expirado. Gere um novo no painel → Minha conta → Conectar meu Telegram.")
        return
    await get_admin_ids(force=True)
    await update.effective_message.reply_text(
        "✅ <b>Telegram vinculado ao painel!</b>\n\n"
        "Agora você recebe alertas e o resumo diário aqui.\n\n"
        "<b>Dica:</b> cole um link do Mercado Livre (meli.la ou da página do produto) nesta conversa "
        "e eu monto o post da oferta para você publicar.\n\n"
        "Comandos: /resumo · /status", parse_mode=ParseMode.HTML)


async def cmd_resumo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update):
        return
    await update.effective_message.reply_text(await run_db(alert_service.daily_summary), parse_mode=ParseMode.HTML)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update):
        return

    def _status():
        with session_scope() as session:
            values = settings_service.get_all(session)
        wa_state = None
        if whatsapp_service.wa_config(values).enabled:
            try:
                with whatsapp_service.default_client() as c:
                    wa_state = c.state(whatsapp_service.wa_config(values).instance)
            except whatsapp_service.WhatsAppError:
                wa_state = "indisponível"
        return alert_service.detect_problems(values, wa_state), values

    problems, values = await run_db(_status)
    promo = settings_service.promo_config(values)
    wa = whatsapp_service.wa_config(values)
    lines = ["🩺 <b>Status</b>\n",
             f"Bot de promoções: {'🟢 ligado' if promo.enabled else '⚪ desligado'}",
             f"WhatsApp: {'🟢 ativo' if wa.enabled else '⚪ desligado'}",
             f"Pagamentos PIX: {'🟢 configurado' if settings.payments_configured else '⚪ não configurado'}", ""]
    lines += [msg for _, msg in problems] or ["✅ Nenhum problema encontrado."]
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# --------------------------------------------------------------------------- #
# Modo conversor: admin cola um link do Mercado Livre e recebe o post pronto
# --------------------------------------------------------------------------- #
def converter_keyboard(offer_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Publicar (Telegram + WhatsApp)", callback_data=f"conv:pub:{offer_id}")],
        [InlineKeyboardButton("Só no Telegram", callback_data=f"conv:tg:{offer_id}"),
         InlineKeyboardButton("🗑 Descartar", callback_data=f"conv:del:{offer_id}")],
    ])


async def on_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update):
        await fallback_text(update, context)
        return
    msg = update.effective_message
    waiting = await msg.reply_text("🔎 Buscando o produto no Mercado Livre...")
    try:
        preview = await run_db(converter_service.import_link, msg.text or "",
                               admin_telegram_id=update.effective_user.id)
    except converter_service.ConvertError as exc:
        await waiting.edit_text(f"⚠️ {exc}")
        return
    await waiting.delete()
    caption = "👀 <b>Prévia do post</b>\n\n" + preview.caption
    if "meli.la" not in preview.link:
        caption += "\n\n<i>Sem link de afiliado: envie um link meli.la para ganhar comissão.</i>"
    try:
        if preview.image_url:
            await msg.reply_photo(preview.image_url, caption=caption[:1024], parse_mode=ParseMode.HTML,
                                  reply_markup=converter_keyboard(preview.offer_id))
            return
    except TelegramError:
        pass
    await msg.reply_text(caption, parse_mode=ParseMode.HTML, reply_markup=converter_keyboard(preview.offer_id),
                         disable_web_page_preview=True)


async def on_converter_action(update: Update, context: ContextTypes.DEFAULT_TYPE, arg: str) -> None:
    query = update.callback_query
    if not await is_admin(update):
        await query.answer("Apenas administradores.", show_alert=True)
        return
    action, _, oid = arg.partition(":")
    if not oid.isdigit():
        await query.answer()
        return
    offer_id = int(oid)

    def _do():
        from models import PromoOffer, WaStatus
        if action == "del":
            promo_service.discard_offer(offer_id, None, None)
            return "🗑 Oferta descartada."
        promo_service.queue_offer(offer_id, None, None, None)
        if action == "tg":
            with session_scope() as session:
                offer = session.get(PromoOffer, offer_id)
                if offer and offer.wa_status == WaStatus.PENDING:
                    offer.wa_status = WaStatus.NONE
            return "📢 Na fila do canal do Telegram. Publica em instantes."
        with session_scope() as session:
            wa_on = whatsapp_service.wa_config(settings_service.get_all(session)).ready
        return ("📢 Na fila do Telegram" + (" e do WhatsApp (respeitando o ritmo de envio)." if wa_on
                                             else ". O WhatsApp está desligado no painel."))

    result = await run_db(_do)
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass
    await update.effective_chat.send_message(result)


# --------------------------------------------------------------------------- #
# Alertas, resumo diário e sinal de vida
# --------------------------------------------------------------------------- #
_last_alert_check = 0.0


async def job_health(context: ContextTypes.DEFAULT_TYPE) -> None:
    global _last_alert_check
    try:
        await run_db(alert_service.heartbeat)
        if time.monotonic() - _last_alert_check < 300:
            return
        _last_alert_check = time.monotonic()

        def _check():
            with session_scope() as session:
                values = settings_service.get_all(session)
            wa = whatsapp_service.wa_config(values)
            wa_state = None
            if wa.enabled:
                try:
                    with whatsapp_service.default_client() as c:
                        wa_state = c.state(wa.instance)
                except whatsapp_service.WhatsAppError:
                    wa_state = "unavailable"
            alerts = alert_service.alerts_to_send(alert_service.detect_problems(values, wa_state))
            summary = alert_service.daily_summary() if alert_service.summary_due(values) else None
            return alerts, summary, alert_service.admin_ids(values)

        alerts, summary, admins = await run_db(_check)
        if not admins:
            return
        for admin_id in admins:
            for text in alerts:
                await telegram_service.safe_send(context.bot, admin_id, text)
            if summary:
                await telegram_service.safe_send(context.bot, admin_id, summary)
        if summary:
            await run_db(alert_service.mark_summary_sent)
    except Exception:  # noqa: BLE001
        logger.exception("Erro no job de saúde/alertas")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Erro ao processar update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat and update.effective_chat.type == ChatType.PRIVATE:
        try:
            await update.effective_chat.send_message("⚠️ Ocorreu um erro inesperado. Tente novamente em instantes.")
        except TelegramError:
            pass


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Menu principal"),
            BotCommand("planos", "Ver planos e preços"),
            BotCommand("meusacessos", "Meus acessos"),
            BotCommand("suporte", "Falar com o suporte"),
        ]
    )
    me = await app.bot.get_me()
    logger.info("Bot iniciado como @%s", me.username)
    if settings.telegram_bot_username and me.username.lower() != settings.telegram_bot_username.lower():
        logger.warning("TELEGRAM_BOT_USERNAME (%s) difere do bot real (@%s) — o link da página ficará errado",
                       settings.telegram_bot_username, me.username)


def build_application() -> Application:
    app = ApplicationBuilder().token(settings.telegram_bot_token).post_init(post_init).build()

    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", start, filters=private))
    app.add_handler(CommandHandler(["help", "ajuda"], help_cmd, filters=private))
    app.add_handler(CommandHandler("planos", cmd_plans, filters=private))
    app.add_handler(CommandHandler("meusacessos", cmd_access, filters=private))
    app.add_handler(CommandHandler("suporte", cmd_support, filters=private))
    app.add_handler(CommandHandler("chatid", admin_chat_id, filters=filters.ChatType.GROUPS | private))
    app.add_handler(CommandHandler("id", cmd_id, filters=private))
    app.add_handler(CommandHandler("vincular", cmd_vincular, filters=private))
    app.add_handler(CommandHandler("resumo", cmd_resumo, filters=private))
    app.add_handler(CommandHandler("status", cmd_status, filters=private))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, channel_post_chat_id))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))
    # Ferramentas de admin (a permissão é verificada dentro de cada handler)
    app.add_handler(MessageHandler(private & filters.FORWARDED, admin_forwarded))
    app.add_handler(MessageHandler(private & (filters.Document.ALL | filters.PHOTO | filters.VIDEO), admin_file_id))
    app.add_handler(MessageHandler(
        private & filters.TEXT & ~filters.COMMAND & filters.Regex(r"(?i)(meli\.la|mercadoli[vb]re\.com)"), on_link))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, fallback_text))
    app.add_error_handler(on_error)

    jq = app.job_queue
    jq.run_repeating(job_deliveries, interval=10, first=5, name="deliveries")
    jq.run_repeating(job_reconcile, interval=120, first=30, name="reconcile")
    jq.run_repeating(job_expirations, interval=300, first=20, name="expirations")
    jq.run_repeating(job_promotions, interval=20, first=15, name="promotions")
    jq.run_repeating(job_whatsapp, interval=30, first=25, name="whatsapp")
    jq.run_repeating(job_health, interval=60, first=10, name="health")
    return app


def main() -> None:
    configure_logging()
    try:
        settings.require_bot()
    except ConfigError as exc:
        logger.error("Configuração inválida: %s — ajuste o .env e reinicie o serviço.", exc)
        time.sleep(30)  # evita reinícios em loop rápido no Docker
        raise SystemExit(1) from None
    if not check_connection():
        raise SystemExit("Não foi possível conectar ao PostgreSQL. Verifique DATABASE_URL.")
    if not settings.payments_configured:
        logger.warning("MP_ACCESS_TOKEN não configurado — compras ficarão indisponíveis.")
    app = build_application()
    # chat_member precisa ser solicitado explicitamente
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
