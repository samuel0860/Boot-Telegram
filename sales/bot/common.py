"""Teclados e utilidades compartilhadas pelos handlers do bot."""
from __future__ import annotations

import asyncio
import logging

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError

from models import AccessType
from sales import catalog_service, sales_service, texts, user_service

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


def _price_for(p: catalog_service.PlanView, coupon: sales_service.CouponView | None):
    if coupon and coupon.applies_to(p.id):
        return sales_service.discounted(p.price, coupon.percent_off)
    return p.price


def plans_keyboard(plans: list[catalog_service.PlanView],
                   coupon: sales_service.CouponView | None = None) -> InlineKeyboardMarkup:
    rows = []
    for p in plans:
        price = _price_for(p, coupon)
        tag = " 🎟" if price != p.price else ""
        rows.append([InlineKeyboardButton(f"🛒 {p.name} — {texts.brl(price)}{tag}"[:64], callback_data=f"buy:{p.id}")])
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


def plans_text(plans: list[catalog_service.PlanView], coupon: sales_service.CouponView | None = None) -> str:
    lines = ["💎 <b>Nossos planos</b>\n"]
    if coupon:
        lines.append(f"🎟 Cupom <b>{texts.escape(coupon.code)}</b> ativo: {coupon.percent_off}% OFF\n")
    for p in plans:
        kind = "📦 Pack" if p.access_type == AccessType.ONE_TIME else "🔁 Assinatura"
        price = _price_for(p, coupon)
        shown = (f"<s>{texts.brl(p.price)}</s> <b>{texts.brl(price)}</b>" if price != p.price
                 else f"<b>{texts.brl(p.price)}</b>")
        lines.append(f"<b>{texts.escape(p.name)}</b> — {shown}")
        lines.append(f"{kind} · ⏳ {p.duration_label}")
        if p.description:
            lines.append(texts.escape(p.description))
        if p.product_names:
            lines.append("Inclui: " + ", ".join(texts.escape(n) for n in p.product_names))
        lines.append("")
    lines.append("👇 Toque em uma oferta para gerar o PIX.")
    return "\n".join(lines)


def active_coupon(context) -> sales_service.CouponView | None:
    """Cupom guardado na conversa (via /cupom, link do /start ou digitado)."""
    data = context.user_data.get("coupon")
    if not data:
        return None
    return sales_service.CouponView(data["code"], data["percent_off"], data.get("plan_id"))


def remember_coupon(context, coupon: sales_service.CouponView | None) -> None:
    if coupon is None:
        context.user_data.pop("coupon", None)
    else:
        context.user_data["coupon"] = {"code": coupon.code, "percent_off": coupon.percent_off,
                                       "plan_id": coupon.plan_id}


# --------------------------------------------------------------------------- #
# Comandos
