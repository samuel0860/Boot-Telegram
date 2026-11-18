"""Controle de entrada em grupos/canais privados (remove quem entrou sem acesso válido)."""
from __future__ import annotations

import logging

from telegram import (
    ChatMemberUpdated,
    Update,
)
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    ContextTypes,
)

from config import settings
from database import session_scope
from sales import access_service, telegram_service, user_service
from sales.bot.common import run_db

logger = logging.getLogger("bot")


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
