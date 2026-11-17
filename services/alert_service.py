"""Administradores no Telegram: vínculo pelo painel, alertas e resumo diário.

- Vínculo: o painel gera um código; o admin envia /vincular CÓDIGO ao bot.
- Alertas: problemas que exigem ação (WhatsApp desconectado, falha na busca de
  ofertas, pagamento não entregue, webhooks com erro). Cada alerta tem
  "cooldown" para não repetir a mesma mensagem.
- Resumo diário: um balanço do dia no horário configurado.
"""
from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from config import settings
from database import session_scope
from models import (
    Order,
    OrderStatus,
    PromoOffer,
    User,
    WebhookEvent,
    WebhookStatus,
)
from services import settings_service
from services.access_service import log_event, utcnow
from services.texts import brl

logger = logging.getLogger(__name__)
TZ = ZoneInfo(settings.display_timezone)
LINK_CODE_TTL = timedelta(minutes=15)
ALERT_COOLDOWN = timedelta(hours=6)
HEARTBEAT_STALE = timedelta(minutes=3)


def _json(raw: str | None, default):
    try:
        value = json.loads(raw) if raw else default
        return value if isinstance(value, type(default)) else default
    except ValueError:
        return default


# --------------------------------------------------------------------------- #
# Administradores vinculados
# --------------------------------------------------------------------------- #
def linked_admin_ids(values: dict[str, str]) -> set[int]:
    return {int(i) for i in _json(values.get("tg_admin_ids"), []) if str(i).lstrip("-").isdigit()}


def admin_ids(values: dict[str, str] | None = None) -> set[int]:
    if values is None:
        with session_scope() as session:
            values = settings_service.get_all(session)
    return set(settings.admin_telegram_ids) | linked_admin_ids(values)


def create_link_code(admin_user_id: int) -> str:
    code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
    with session_scope() as session:
        settings_service.set_values(session, {"tg_link_code": json.dumps({
            "code": code, "admin_user_id": admin_user_id,
            "expires": (utcnow() + LINK_CODE_TTL).isoformat(),
        })}, admin_user_id)
    return code


def current_link_code(values: dict[str, str]) -> dict | None:
    data = _json(values.get("tg_link_code"), {})
    try:
        if data and datetime.fromisoformat(data["expires"]) > utcnow():
            return data
    except (KeyError, ValueError):
        pass
    return None


def redeem_link_code(code: str, telegram_id: int, name: str) -> bool:
    code = (code or "").strip().upper()
    with session_scope() as session:
        values = settings_service.get_all(session)
        data = current_link_code(values)
        if not data or not secrets.compare_digest(data["code"], code):
            return False
        ids = sorted(linked_admin_ids(values) | {telegram_id})
        settings_service.set_values(session, {"tg_admin_ids": json.dumps(ids), "tg_link_code": ""},
                                    data.get("admin_user_id"))
        log_event(session, "telegram_admin_linked", admin_user_id=data.get("admin_user_id"),
                  telegram_id=telegram_id, name=name[:64])
    return True


def unlink_admin(telegram_id: int, admin_user_id: int | None) -> None:
    with session_scope() as session:
        values = settings_service.get_all(session)
        ids = sorted(linked_admin_ids(values) - {telegram_id})
        settings_service.set_values(session, {"tg_admin_ids": json.dumps(ids)}, admin_user_id)
        log_event(session, "telegram_admin_unlinked", admin_user_id=admin_user_id, telegram_id=telegram_id)


# --------------------------------------------------------------------------- #
# Saúde do bot
# --------------------------------------------------------------------------- #
def heartbeat() -> None:
    with session_scope() as session:
        settings_service.set_values(session, {"bot_heartbeat_at": utcnow().isoformat()})


def bot_online(values: dict[str, str], now: datetime | None = None) -> bool:
    try:
        last = datetime.fromisoformat(values.get("bot_heartbeat_at") or "")
    except ValueError:
        return False
    return (now or utcnow()) - last < HEARTBEAT_STALE


# --------------------------------------------------------------------------- #
# Alertas
# --------------------------------------------------------------------------- #
def detect_problems(values: dict[str, str], wa_state: str | None, now: datetime | None = None) -> list[tuple[str, str]]:
    """Retorna [(chave, mensagem)] dos problemas atuais. wa_state=None se não verificado."""
    from services import promo_service, whatsapp_service

    now = now or utcnow()
    problems: list[tuple[str, str]] = []

    wa = whatsapp_service.wa_config(values)
    if wa.enabled and wa_state is not None and wa_state != "open":
        problems.append(("wa_disconnected",
                         "📵 <b>WhatsApp desconectado.</b> Os envios estão parados. "
                         "Abra o painel → WhatsApp e leia o QR Code de novo."))

    promo = settings_service.promo_config(values)
    last = promo_service.last_result(values)
    if promo.enabled and last and not last.get("ok", True):
        problems.append(("promo_error", "⚠️ <b>Falha na busca de ofertas do Mercado Livre.</b>\n"
                                        f"{last.get('summary', '')[:300]}\nConfira as credenciais no painel → Bot de promoções."))

    with session_scope() as session:
        undelivered = session.scalar(
            select(func.count()).select_from(Order).where(
                Order.status == OrderStatus.PAID, Order.delivered_at.is_(None),
                Order.paid_at < now - timedelta(minutes=10)))
        failed_hooks = session.scalar(
            select(func.count()).select_from(WebhookEvent).where(
                WebhookEvent.status == WebhookStatus.FAILED, WebhookEvent.created_at > now - timedelta(hours=1)))
    if undelivered:
        problems.append(("undelivered", f"📦 <b>{undelivered} pedido(s) pago(s) sem entrega</b> há mais de 10 min. "
                                        "Veja em Pedidos no painel."))
    if failed_hooks >= 3:
        problems.append(("webhook_failed", f"🔔 <b>{failed_hooks} webhooks de pagamento com falha</b> na última hora. "
                                           "Veja em Webhooks no painel."))
    return problems


def alerts_to_send(problems: list[tuple[str, str]], now: datetime | None = None) -> list[str]:
    """Aplica o cooldown e registra o envio. Problemas resolvidos liberam o alerta de novo."""
    now = now or utcnow()
    with session_scope() as session:
        values = settings_service.get_all(session)
        if values.get("alerts_enabled") != "1":
            return []
        state = _json(values.get("alerts_state"), {})
        sent_at = state.get("sent", {})
        active = {k for k, _ in problems}
        sent_at = {k: v for k, v in sent_at.items() if k in active}  # resolvidos: esquece
        messages = []
        for key, msg in problems:
            last = sent_at.get(key)
            if last and now - datetime.fromisoformat(last) < ALERT_COOLDOWN:
                continue
            sent_at[key] = now.isoformat()
            messages.append(msg)
        state["sent"] = sent_at
        settings_service.set_values(session, {"alerts_state": json.dumps(state)})
    return messages


# --------------------------------------------------------------------------- #
# Resumo diário
# --------------------------------------------------------------------------- #
def summary_due(values: dict[str, str], now: datetime | None = None) -> bool:
    now = (now or utcnow()).astimezone(TZ)
    try:
        hour = int(values.get("daily_summary_hour") or 21)
    except ValueError:
        hour = 21
    if values.get("alerts_enabled") != "1" or not (0 <= hour <= 23) or now.hour < hour:
        return False
    return _json(values.get("alerts_state"), {}).get("summary_date") != now.date().isoformat()


def mark_summary_sent(now: datetime | None = None) -> None:
    now = (now or utcnow()).astimezone(TZ)
    with session_scope() as session:
        values = settings_service.get_all(session)
        state = _json(values.get("alerts_state"), {})
        state["summary_date"] = now.date().isoformat()
        settings_service.set_values(session, {"alerts_state": json.dumps(state)})


def daily_summary(now: datetime | None = None) -> str:
    now = now or utcnow()
    start = now.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    with session_scope() as session:
        new_users = session.scalar(select(func.count()).select_from(User).where(User.created_at >= start))
        sales = session.execute(
            select(func.count(Order.id), func.coalesce(func.sum(Order.amount), 0))
            .where(Order.status == OrderStatus.PAID, Order.paid_at >= start)).one()
        posted = session.scalar(select(func.count()).select_from(PromoOffer).where(PromoOffer.posted_at >= start))
        wa_sent = session.scalar(select(func.count()).select_from(PromoOffer).where(PromoOffer.wa_sent_at >= start))
    return (
        f"📊 <b>Resumo de hoje</b> ({now.astimezone(TZ):%d/%m})\n\n"
        f"👥 Novos usuários no bot: <b>{new_users}</b>\n"
        f"💰 Vendas: <b>{sales[0]}</b> · {brl(sales[1])}\n"
        f"📢 Ofertas no Telegram: <b>{posted}</b>\n"
        f"💬 Ofertas no WhatsApp: <b>{wa_sent}</b>"
    )
