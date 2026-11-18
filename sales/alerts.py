"""Alertas e resumo diário do MÓDULO DE VENDAS (enviados pelo bot de vendas)."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select

from core.alert_service import TZ, day_start
from core.audit import utcnow
from database import session_scope
from models import Order, OrderStatus, User, WebhookEvent, WebhookStatus
from sales.texts import brl

SCOPE = "sales"


def detect_problems(now: datetime | None = None) -> list[tuple[str, str]]:
    """[(chave, mensagem)] dos problemas atuais das vendas."""
    now = now or utcnow()
    problems: list[tuple[str, str]] = []
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


def daily_summary(now: datetime | None = None) -> str:
    now = now or utcnow()
    start = day_start(now)
    with session_scope() as session:
        new_users = session.scalar(select(func.count()).select_from(User).where(User.created_at >= start))
        sales = session.execute(
            select(func.count(Order.id), func.coalesce(func.sum(Order.amount), 0))
            .where(Order.status == OrderStatus.PAID, Order.paid_at >= start)).one()
        pix = session.scalar(select(func.count()).select_from(Order).where(Order.created_at >= start))
    return (
        f"📊 <b>Vendas de hoje</b> ({now.astimezone(TZ):%d/%m})\n\n"
        f"👥 Novos usuários no bot: <b>{new_users}</b>\n"
        f"🧾 PIX gerados: <b>{pix}</b>\n"
        f"💰 Vendas: <b>{sales[0]}</b> · {brl(sales[1])}"
    )
