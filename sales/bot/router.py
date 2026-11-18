"""Roteador dos botões (callback_data) do bot."""
from __future__ import annotations

import logging

from telegram import (
    Update,
)
from telegram.error import TelegramError
from telegram.ext import (
    ContextTypes,
)

from sales.bot.common import ensure_allowed
from sales.bot.sales import (
    buy_plan,
    cancel_order,
    check_payment,
    resend_content,
    show_access,
    show_buy,
    show_content,
    show_menu,
    show_plans,
    show_support,
)

logger = logging.getLogger("bot")


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
    if action not in ("check", "resend"):
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
    if action in ("check", "resend"):
        try:
            await query.answer()
        except TelegramError:
            pass  # já respondido com alerta
