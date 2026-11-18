"""Atendimento e vendas: menu, planos, cupons, PIX, verificação e reenvio de conteúdo."""
from __future__ import annotations

import base64
import binascii
import io
import logging
import time

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    ContextTypes,
)

from database import session_scope
from models import OrderStatus
from sales import access_service, catalog_service, payment_service, sales_service, telegram_service, texts, user_service
from sales.bot.common import (
    CHECK_COOLDOWN_SECONDS,
    active_coupon,
    back_button,
    ensure_allowed,
    main_menu,
    plans_keyboard,
    plans_text,
    remember_coupon,
    reply,
    run_db,
)
from sales.payments import PaymentProviderError, get_payment_client

logger = logging.getLogger("bot")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    if tg is None or update.effective_chat.type != ChatType.PRIVATE:
        return
    source, coupon_code = sales_service.split_start_param(context.args[0] if context.args else None)
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
    if coupon_code:  # link com cupom: t.me/SeuBot?start=tiktok__PROMO10
        await apply_coupon(update, context, coupon_code)


async def apply_coupon(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str, *, quiet: bool = False) -> bool:
    """Valida o cupom, guarda na conversa e mostra os planos com o desconto."""
    try:
        coupon = await run_db(sales_service.coupon_view, code)
    except sales_service.CouponError as exc:
        if not quiet:
            await update.effective_chat.send_message(f"⚠️ {texts.escape(str(exc))}", parse_mode=ParseMode.HTML)
        return False
    remember_coupon(context, coupon)
    plans = await run_db(catalog_service.list_active_plans)
    plan_name = next((p.name for p in plans if p.id == coupon.plan_id), None) if coupon.plan_id else None
    await update.effective_chat.send_message(texts.coupon_applied(coupon.code, coupon.percent_off, plan_name),
                                             parse_mode=ParseMode.HTML)
    if plans:
        await update.effective_chat.send_message(plans_text(plans, coupon), parse_mode=ParseMode.HTML,
                                                 reply_markup=plans_keyboard(plans, coupon))
    return True


async def cmd_coupon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/cupom CODIGO"""
    if not await ensure_allowed(update):
        return
    if not context.args:
        current = active_coupon(context)
        extra = f"\n\nCupom ativo agora: <b>{texts.escape(current.code)}</b>" if current else ""
        await update.effective_chat.send_message(
            "🎟 Para usar um cupom, envie: <code>/cupom SEUCODIGO</code>" + extra, parse_mode=ParseMode.HTML)
        return
    await apply_coupon(update, context, context.args[0])


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update):
        return
    await update.effective_chat.send_message(
        "ℹ️ <b>Comandos</b>\n/start — menu principal\n/planos — ver planos\n/cupom CODIGO — usar cupom de desconto\n"
        "/meusacessos — seus acessos\n/suporte — ajuda",
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
    coupon = active_coupon(context)
    await reply(update, plans_text(plans, coupon), plans_keyboard(plans, coupon))


async def show_buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    plans = await run_db(catalog_service.list_active_plans)
    if not plans:
        await reply(update, texts.NO_PLANS, InlineKeyboardMarkup([back_button()]))
        return
    coupon = active_coupon(context)
    hint = f"\n🎟 Cupom <b>{texts.escape(coupon.code)}</b> aplicado." if coupon else ""
    await reply(update, f"🛒 <b>Comprar acesso</b>\n\nEscolha a oferta desejada:{hint}", plans_keyboard(plans, coupon))


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
    coupon = active_coupon(context)
    if coupon and not coupon.applies_to(plan_id):
        coupon = None  # cupom de outro plano: compra normal, sem perder o cupom
    try:
        result = await run_db(
            payment_service.create_pix_checkout, update.effective_user.id, plan_id, get_payment_client(),
            coupon.code if coupon else None,
        )
    except payment_service.CouponRejected as exc:
        remember_coupon(context, None)
        await chat.send_message(
            f"⚠️ {texts.escape(str(exc))}", parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Gerar PIX sem cupom",
                                                                     callback_data=f"buy:{plan_id}")],
                                               back_button()]))
        return
    except payment_service.CheckoutError as exc:
        await chat.send_message(f"⚠️ {texts.escape(str(exc))}", parse_mode=ParseMode.HTML,
                                reply_markup=InlineKeyboardMarkup([back_button()]))
        return

    if result.coupon_code and result.list_price and result.list_price != result.amount:
        caption = texts.pix_caption_with_coupon(result.plan_name, result.amount, result.list_price,
                                                result.coupon_code, result.expires_at, result.order_public_id)
    else:
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
    if not await ensure_allowed(update):
        return
    text = (update.effective_message.text or "").strip()
    # o usuário digitou só um código? tenta como cupom (sem mensagem de erro se não for)
    if sales_service.CODE_RE.match(sales_service.normalize_code(text)) and " " not in text:
        if await apply_coupon(update, context, text, quiet=True):
            return
    await update.effective_chat.send_message("Use o menu abaixo 👇", reply_markup=main_menu())
