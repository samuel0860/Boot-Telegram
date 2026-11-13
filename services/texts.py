"""Textos do bot (HTML do Telegram). Edite aqui para mudar a comunicação."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from html import escape
from zoneinfo import ZoneInfo

from config import settings

TZ = ZoneInfo(settings.display_timezone)


def brl(value: Decimal) -> str:
    """Formata Decimal como moeda brasileira: 1234.5 -> 'R$ 1.234,50'."""
    formatted = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {formatted}"


def fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "sem vencimento"
    return value.astimezone(TZ).strftime("%d/%m/%Y %H:%M")


def welcome(first_name: str | None, is_new: bool) -> str:
    name = escape(first_name) if first_name else ""
    greeting = f"Olá, <b>{name}</b>! 👋" if name else "Olá! 👋"
    if is_new:
        intro = "Seja muito bem-vindo(a)! 🎉"
    else:
        intro = "Que bom ter você de volta! 💙"
    return (
        f"{greeting}\n{intro}\n\n"
        "✨ Aqui você encontra nosso <b>conteúdo exclusivo</b> e pode escolher "
        "o acesso que melhor combina com você.\n\n"
        "📦 Packs individuais e 💎 assinaturas com liberação automática após o pagamento via PIX.\n\n"
        "👇 Confira nossas opções abaixo e escolha seu plano."
    )


BLOCKED = "🚫 Sua conta está bloqueada. Se acredita que isso é um erro, fale com o suporte."

NO_PLANS = "😕 Nenhuma oferta disponível no momento. Volte em breve!"

NO_CONTENT = "📭 Ainda não há conteúdos publicados. Volte em breve!"


def support() -> str:
    contact = escape(settings.support_contact) if settings.support_contact else "o administrador"
    return (
        "💬 <b>Suporte</b>\n\n"
        f"Precisa de ajuda? Fale com {contact}.\n\n"
        "Ao entrar em contato, informe o código do seu pedido (aparece na mensagem do PIX)."
    )


HOW_TO_PAY = (
    "📲 <b>Como pagar:</b>\n"
    "1. Abra o app do seu banco e escolha <b>PIX</b>.\n"
    "2. Escaneie o QR Code <b>ou</b> use o <b>PIX copia e cola</b> abaixo.\n"
    "3. Confirme o pagamento.\n\n"
    "✅ A liberação é <b>automática</b> assim que o banco confirmar o pagamento."
)


def pix_caption(plan_name: str, amount: Decimal, expires_at: datetime | None, order_public_id: str) -> str:
    return (
        f"🧾 <b>{escape(plan_name)}</b>\n"
        f"💰 Valor: <b>{brl(amount)}</b>\n"
        f"⏳ Pague até: {fmt_dt(expires_at)}\n"
        f"🔖 Pedido: <code>{order_public_id[:8]}</code>\n\n"
        f"{HOW_TO_PAY}"
    )


def pix_copy_paste(code: str) -> str:
    return f"📋 <b>PIX copia e cola</b> (toque para copiar):\n\n<code>{escape(code)}</code>"


PAYMENT_PENDING = (
    "⏳ Ainda não recebemos a confirmação do seu pagamento.\n\n"
    "Se você já pagou, aguarde alguns instantes — a confirmação do banco pode levar até alguns minutos. "
    "Você será avisado automaticamente aqui."
)


def payment_approved(plan_name: str, expires_at: datetime | None) -> str:
    validity = f"📅 Válido até: <b>{fmt_dt(expires_at)}</b>" if expires_at else "♾️ Acesso <b>vitalício</b>"
    return (
        "🎉 <b>Pagamento aprovado!</b>\n\n"
        f"Seu acesso ao <b>{escape(plan_name)}</b> foi liberado.\n{validity}\n\n"
        "Seu conteúdo está logo abaixo 👇"
    )


def invite_message(name: str, link: str, hours: int) -> str:
    return (
        f"🔐 <b>{escape(name)}</b>\n"
        f"Seu convite individual (uso único, válido por {hours}h):\n{escape(link)}"
    )


NO_ITEMS_YET = (
    "ℹ️ O conteúdo deste plano está sendo preparado. Assim que estiver disponível, "
    "use o botão <b>Meus acessos</b> para recebê-lo. Em caso de dúvida, fale com o suporte."
)


def expiry_reminder(plan_name: str, expires_at: datetime) -> str:
    return (
        f"⏰ Seu acesso ao <b>{escape(plan_name)}</b> vence em <b>{fmt_dt(expires_at)}</b>.\n\n"
        "Renove agora para não perder o acesso! 👇"
    )


def access_ended(plan_name: str, revoked: bool) -> str:
    if revoked:
        return f"⚠️ Seu acesso ao <b>{escape(plan_name)}</b> foi encerrado. Em caso de dúvidas, fale com o suporte."
    return (
        f"⌛ Seu acesso ao <b>{escape(plan_name)}</b> expirou.\n\n"
        "Sentiremos sua falta! Renove quando quiser 👇"
    )
