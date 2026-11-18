"""Handlers do Telegram de CADA bot de ofertas (um Application por token).

O bot de ofertas não vende nada: ele publica nos canais/grupos e atende só os
administradores (modo conversor, IDs de canais, status, vínculo).
"""
from __future__ import annotations

import asyncio
import logging
import time
from html import escape

from telegram import ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup, Update
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

from core import alert_service
from database import session_scope
from models import OfferBot
from offers import bots as bot_service
from offers import collector, converter, whatsapp

logger = logging.getLogger("offers.bot")
_ADMIN_CACHE: dict[str, object] = {"ids": set(), "at": 0.0}


async def run_db(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


def bot_id_of(context: ContextTypes.DEFAULT_TYPE) -> int:
    return context.application.bot_data["offer_bot_id"]


async def admin_ids(force: bool = False) -> set[int]:
    if force or time.monotonic() - float(_ADMIN_CACHE["at"]) > 30:
        _ADMIN_CACHE["ids"] = await run_db(alert_service.admin_ids)
        _ADMIN_CACHE["at"] = time.monotonic()
    return _ADMIN_CACHE["ids"]  # type: ignore[return-value]


async def is_admin(update: Update) -> bool:
    user = update.effective_user
    return user is not None and user.id in await admin_ids()


# --------------------------------------------------------------------------- #
# Comandos
# --------------------------------------------------------------------------- #
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    if await is_admin(update):
        await update.effective_message.reply_text(
            "🤖 <b>Bot de ofertas</b>\n\n"
            "• Cole um link do Mercado Livre, da Shopee ou da Amazon e eu monto o post (dá para escrever "
            "<code>chamada: ...</code> e <code>cupom: ...</code> em linhas abaixo do link).\n"
            "• Encaminhe um post de um canal para descobrir o ID dele.\n"
            "• /status — situação deste bot\n• /id — seu ID do Telegram",
            parse_mode=ParseMode.HTML)
        return
    await update.effective_message.reply_text(
        "👋 Este é um bot de ofertas: as promoções são publicadas nos nossos canais e grupos.")


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        f"🆔 Seu ID do Telegram: <code>{update.effective_user.id}</code>", parse_mode=ParseMode.HTML)


async def cmd_vincular(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    code = context.args[0] if context.args else ""
    user = update.effective_user
    if not await run_db(alert_service.redeem_link_code, code, user.id, user.full_name or ""):
        await update.effective_message.reply_text(
            "❌ Código inválido ou expirado. Gere um novo no painel → Minha conta.")
        return
    await admin_ids(force=True)
    await update.effective_message.reply_text("✅ Telegram vinculado ao painel! Você vai receber os alertas deste bot aqui.")


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update):
        return
    chat = update.effective_chat
    await update.effective_message.reply_text(f"ID deste chat: <code>{chat.id}</code>", parse_mode=ParseMode.HTML)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update):
        return
    bid = bot_id_of(context)

    def _status():
        with session_scope() as session:
            bot = session.get(OfferBot, bid)
            cfg = bot_service.config_for(session, bot)
            tg = [t.name for t in bot.targets if t.channel.value == "telegram" and t.is_active]
            wa = whatsapp.wa_config(bot, cfg)
            last = collector.last_result(bot)
            return bot.name, bot.is_active, tg, wa, last
    name, active, tg, wa, last = await run_db(_status)
    stats = await run_db(collector.stats, bid)
    lines = [f"🤖 <b>{escape(name)}</b> — {'🟢 ligado' if active else '⚪ desligado'}",
             f"📢 Telegram: {len(tg)} destino(s)" + (f" ({escape(', '.join(tg[:3]))})" if tg else ""),
             f"💬 WhatsApp: {'ligado' if wa.ready else 'desligado'} · {len(wa.groups)} grupo(s)",
             f"📊 Na fila: {stats['queued']} · publicadas 24h: {stats['posted_24h']} · WhatsApp 24h: {stats['wa_24h']}"]
    if last.get("summary"):
        lines.append(f"🔎 Última busca: {escape(last['summary'])[:300]}")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# --------------------------------------------------------------------------- #
# Descoberta de canais/grupos (o painel oferece adicionar com 1 clique)
# --------------------------------------------------------------------------- #
def _can_post(member) -> bool:
    if member.status == ChatMemberStatus.ADMINISTRATOR:
        return bool(getattr(member, "can_post_messages", None) in (None, True))
    return member.status == ChatMemberStatus.MEMBER


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    change: ChatMemberUpdated = update.my_chat_member
    chat = change.chat
    if chat.type == ChatType.PRIVATE:
        return
    new = change.new_chat_member
    if new.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
        return
    await run_db(_record, bot_id_of(context), chat.id, chat.title or "", chat.type, _can_post(new))


def _record(bot_id: int, chat_id: int, title: str, kind: str, can_post: bool) -> None:
    with session_scope() as session:
        bot_service.record_seen_chat(session, bot_id, chat_id, title, kind, can_post)


async def on_forwarded(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update):
        return
    origin = update.effective_message.forward_origin
    chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
    if chat is None:
        await update.effective_message.reply_text("Encaminhe uma mensagem de um CANAL para descobrir o ID dele.")
        return
    await update.effective_message.reply_text(
        f"📢 <b>{escape(chat.title or '')}</b>\nID: <code>{chat.id}</code>\n\n"
        "Adicione esse ID no painel → Ofertas → este bot → Destinos do Telegram.", parse_mode=ParseMode.HTML)


# --------------------------------------------------------------------------- #
# Modo conversor
# --------------------------------------------------------------------------- #
def converter_keyboard(offer_id: int, has_whatsapp: bool) -> InlineKeyboardMarkup:
    rows = []
    if has_whatsapp:
        rows.append([InlineKeyboardButton("📢 Publicar (Telegram + WhatsApp)", callback_data=f"conv:pub:{offer_id}")])
    rows.append([InlineKeyboardButton("Só no Telegram", callback_data=f"conv:tg:{offer_id}")]
                + ([InlineKeyboardButton("Só no WhatsApp", callback_data=f"conv:wa:{offer_id}")] if has_whatsapp else []))
    rows.append([InlineKeyboardButton("🗑 Descartar", callback_data=f"conv:del:{offer_id}")])
    return InlineKeyboardMarkup(rows)


async def on_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update):
        return
    msg = update.effective_message
    waiting = await msg.reply_text("🔎 Buscando o produto...")
    try:
        preview = await run_db(converter.import_link, bot_id_of(context), msg.text or "",
                               admin_telegram_id=update.effective_user.id)
    except converter.ConvertError as exc:
        await waiting.edit_text(f"⚠️ {exc}")
        return
    await waiting.delete()
    caption = "👀 <b>Prévia do post</b>\n\n" + preview.caption
    tips = []
    if not any(h in preview.link for h in ("meli.la", "shopee", "shp.ee", "shope.ee", "amazon.com.br", "amzn.to", "a.co/")):
        tips.append("Sem link de afiliado: envie o seu link meli.la para ganhar comissão.")
    if not preview.has_extras:
        tips.append("Dica: escreva <code>chamada: ...</code> e <code>cupom: ...</code> em linhas abaixo do link.")
    if tips:
        caption += "\n\n" + "\n".join(f"<i>{t}</i>" for t in tips)
    markup = converter_keyboard(preview.offer_id, preview.has_whatsapp)
    try:
        if preview.image_url:
            await msg.reply_photo(preview.image_url, caption=caption[:1024], parse_mode=ParseMode.HTML,
                                  reply_markup=markup)
            return
    except TelegramError:
        pass
    await msg.reply_text(caption, parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await is_admin(update):
        await query.answer("Apenas administradores.", show_alert=True)
        return
    parts = (query.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    oid = parts[2] if len(parts) > 2 else ""
    if not oid.isdigit():
        await query.answer()
        return
    bid, offer_id = bot_id_of(context), int(oid)
    if action == "del":
        await run_db(collector.discard_post, offer_id, bid)
        text = "🗑 Oferta descartada."
    else:
        tg, wa = action in ("pub", "tg"), action in ("pub", "wa")
        ok = await run_db(collector.queue_post, offer_id, bid, telegram=tg, whatsapp_too=wa)
        where = {"pub": "Telegram e WhatsApp", "tg": "Telegram", "wa": "WhatsApp"}.get(action, "")
        text = (f"📢 Na fila do {where}. Sai respeitando o ritmo de envio." if ok
                else "ℹ️ Nada a fazer: confira se o bot tem destinos ativos no painel.")
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass
    await update.effective_chat.send_message(text)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Erro no bot de ofertas %s", context.application.bot_data.get("offer_bot_id"),
                 exc_info=context.error)


def build_application(token: str, bot_id: int) -> Application:
    app = ApplicationBuilder().token(token).job_queue(None).build()
    app.bot_data["offer_bot_id"] = bot_id
    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", cmd_start, filters=private))
    app.add_handler(CommandHandler("id", cmd_id, filters=private))
    app.add_handler(CommandHandler("vincular", cmd_vincular, filters=private))
    app.add_handler(CommandHandler("status", cmd_status, filters=private))
    app.add_handler(CommandHandler("chatid", cmd_chatid, filters=filters.ChatType.GROUPS | private))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^conv:"))
    app.add_handler(MessageHandler(private & filters.FORWARDED, on_forwarded))
    app.add_handler(MessageHandler(
        private & filters.TEXT & ~filters.COMMAND & filters.Regex(r"(?i)(meli\.la|mercadoli[vb]re\.com|shopee\.com\.br|shp\.ee|shope\.ee|amazon\.com\.br|amzn\.to|a\.co/)"), on_link))
    app.add_error_handler(on_error)
    return app
