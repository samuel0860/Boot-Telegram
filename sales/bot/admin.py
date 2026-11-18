"""Ferramentas dos administradores no Telegram: IDs, vínculo, /status, /resumo e modo conversor."""
from __future__ import annotations

import logging
import time

from telegram import (
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    ContextTypes,
)

from config import settings
from core import alert_service
from sales import alerts as sales_alerts
from sales import texts
from sales.bot.common import run_db
from sales.bot.sales import fallback_text

logger = logging.getLogger("bot")


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
        "Use esse ID para cadastrar o grupo/canal como conteúdo de um plano.", parse_mode=ParseMode.HTML
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
        "Agora você recebe aqui os alertas e o resumo diário das <b>vendas</b>.\n"
        "Os bots de ofertas avisam pelo próprio Telegram deles (abra cada um e envie /start).\n\n"
        "Comandos: /resumo · /status", parse_mode=ParseMode.HTML)


async def cmd_resumo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update):
        return
    await update.effective_message.reply_text(await run_db(sales_alerts.daily_summary), parse_mode=ParseMode.HTML)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update):
        return
    problems = await run_db(sales_alerts.detect_problems)
    lines = ["🩺 <b>Status das vendas</b>\n",
             f"Pagamentos ({settings.payment_gateway_label}): "
             f"{'🟢 configurado' if settings.payments_configured else '⚪ não configurado'}", ""]
    lines += [msg for _, msg in problems] or ["✅ Nenhum problema encontrado."]
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
