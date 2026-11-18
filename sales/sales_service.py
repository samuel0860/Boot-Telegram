"""Recursos de vendas do funil do bot (PIX): cupons, recuperação de PIX expirado,
oferta de outro plano após a compra (upsell) e relatório do funil por origem.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import and_, distinct, func, select
from sqlalchemy.orm import Session

from config import settings
from models import AccessType, Coupon, Order, OrderStatus, Plan, Subscription, SubscriptionStatus, User
from sales.access_service import log_event, utcnow

CENT = Decimal("0.01")
CODE_RE = re.compile(r"^[A-Z0-9_-]{3,32}$")


# --------------------------------------------------------------------------- #
# Cupons
# --------------------------------------------------------------------------- #
class CouponError(Exception):
    """Mensagem amigável para o usuário."""


def normalize_code(code: str | None) -> str:
    return (code or "").strip().upper()


def discounted(price: Decimal, percent_off: int) -> Decimal:
    value = (Decimal(price) * (100 - percent_off) / 100).quantize(CENT, rounding=ROUND_HALF_UP)
    return max(value, CENT)


def coupon_uses(session: Session, coupon_id: int) -> int:
    return session.scalar(
        select(func.count(Order.id)).where(Order.coupon_id == coupon_id, Order.status == OrderStatus.PAID)
    ) or 0


def find_coupon(session: Session, code: str, plan_id: int | None = None, now: datetime | None = None) -> Coupon:
    """Retorna o cupom válido ou lança CouponError explicando o motivo."""
    code = normalize_code(code)
    if not CODE_RE.match(code):
        raise CouponError("Cupom inválido.")
    coupon = session.scalar(select(Coupon).where(Coupon.code == code))
    now = now or utcnow()
    if coupon is None or not coupon.is_active:
        raise CouponError(f"O cupom {code} não existe ou não está mais ativo.")
    if coupon.valid_until and coupon.valid_until < now:
        raise CouponError(f"O cupom {code} expirou.")
    if coupon.max_uses and coupon_uses(session, coupon.id) >= coupon.max_uses:
        raise CouponError(f"O cupom {code} já foi usado o número máximo de vezes.")
    if plan_id is not None and coupon.plan_id and coupon.plan_id != plan_id:
        plan = session.get(Plan, coupon.plan_id)
        raise CouponError(f"O cupom {code} vale só para o plano {plan.name if plan else 'específico'}.")
    return coupon


@dataclass(frozen=True)
class CouponView:
    code: str
    percent_off: int
    plan_id: int | None

    def applies_to(self, plan_id: int) -> bool:
        return self.plan_id is None or self.plan_id == plan_id


def coupon_view(code: str) -> CouponView:
    """Valida o cupom (sem plano) para mostrar ao usuário. Lança CouponError."""
    from database import session_scope

    with session_scope() as session:
        c = find_coupon(session, code)
        return CouponView(c.code, c.percent_off, c.plan_id)


def split_start_param(param: str | None) -> tuple[str | None, str | None]:
    """'tiktok__PROMO10' -> ('tiktok', 'PROMO10'). Link: t.me/SeuBot?start=tiktok__PROMO10"""
    if not param:
        return None, None
    source, sep, coupon = param.partition("__")
    return (source or None), (normalize_code(coupon) if sep and coupon else None)


# --------------------------------------------------------------------------- #
# Recuperação de PIX expirado
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RecoveryTask:
    order_id: int
    telegram_id: int
    plan_id: int
    plan_name: str
    amount: Decimal


def due_recoveries(session: Session, now: datetime | None = None, limit: int = 30) -> list[RecoveryTask]:
    """PIX que expiraram sem pagamento: manda UMA mensagem oferecendo gerar outro.

    Não envia se: o usuário pagou/gerou outro pedido do mesmo plano depois, está
    bloqueado, o plano foi desativado, ou já recebeu uma recuperação nas últimas 24 h.
    Marca recovery_sent_at no ato (o chamador faz commit) — nunca repete.
    """
    if settings.recovery_delay_minutes <= 0:
        return []
    now = now or utcnow()
    orders = session.scalars(
        select(Order)
        .where(
            Order.status == OrderStatus.EXPIRED,
            Order.recovery_sent_at.is_(None),
            Order.expires_at < now - timedelta(minutes=settings.recovery_delay_minutes),
            Order.expires_at > now - timedelta(hours=24),
        )
        .order_by(Order.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    tasks = []
    handled_users: set[int] = set()
    for order in orders:
        order.recovery_sent_at = now
        if order.user_id in handled_users:
            continue
        handled_users.add(order.user_id)
        user = session.get(User, order.user_id)
        plan = session.get(Plan, order.plan_id)
        if user is None or user.is_blocked or plan is None or not plan.is_active:
            continue
        newer = session.scalar(
            select(func.count(Order.id)).where(
                Order.user_id == order.user_id, Order.id > order.id,
                Order.status.in_([OrderStatus.PENDING, OrderStatus.PAID]),
            )
        )
        recent_recovery = session.scalar(
            select(func.count(Order.id)).where(
                Order.user_id == order.user_id, Order.id != order.id,
                Order.recovery_sent_at > now - timedelta(hours=24),
            )
        )
        if newer or recent_recovery:
            continue
        log_event(session, "pix_recovery_sent", user_id=user.id, order_id=order.id)
        tasks.append(RecoveryTask(order.id, user.telegram_id, plan.id, plan.name, plan.price))
    return tasks


# --------------------------------------------------------------------------- #
# Upsell: depois da entrega, oferece um plano maior
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class UpsellTask:
    telegram_id: int
    plan_id: int
    plan_name: str
    price: Decimal
    duration_label: str


def upsell_for_order(session: Session, order_id: int) -> UpsellTask | None:
    """Plano ativo mais barato entre os que custam MAIS que o comprado e que o
    usuário ainda não tem. Uma vez por pedido."""
    if not settings.upsell_enabled:
        return None
    order = session.get(Order, order_id, with_for_update=True)
    if order is None or order.upsell_sent_at is not None or order.status != OrderStatus.PAID:
        return None
    order.upsell_sent_at = utcnow()
    bought = session.get(Plan, order.plan_id)
    owned = select(Subscription.plan_id).where(
        Subscription.user_id == order.user_id,
        Subscription.status == SubscriptionStatus.ACTIVE,
        (Subscription.expires_at.is_(None)) | (Subscription.expires_at > utcnow()),
    )
    candidate = session.scalar(
        select(Plan)
        .where(Plan.is_active.is_(True), Plan.price > bought.price, Plan.id.not_in(owned))
        .order_by(
            # prefere assinatura quando a compra foi um pack
            (Plan.access_type != AccessType.SUBSCRIPTION) if bought.access_type == AccessType.ONE_TIME else Plan.price,
            Plan.price,
        )
        .limit(1)
    )
    if candidate is None:
        return None
    user = session.get(User, order.user_id)
    log_event(session, "upsell_sent", user_id=user.id, order_id=order.id, plan=candidate.slug)
    return UpsellTask(user.telegram_id, candidate.id, candidate.name, candidate.price, candidate.duration_label)


# --------------------------------------------------------------------------- #
# Relatório do funil por origem (parâmetro do /start)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FunnelRow:
    source: str
    users: int
    checkout_users: int
    buyers: int
    orders_paid: int
    revenue: Decimal

    @property
    def checkout_rate(self) -> float:
        return 100 * self.checkout_users / self.users if self.users else 0.0

    @property
    def conversion(self) -> float:
        return 100 * self.buyers / self.users if self.users else 0.0

    @property
    def ticket(self) -> Decimal:
        return (self.revenue / self.orders_paid).quantize(CENT) if self.orders_paid else Decimal("0")


def funnel_by_source(session: Session, since: datetime | None) -> list[FunnelRow]:
    """Usuários que chegaram no período (por origem) → geraram PIX → pagaram."""
    source = func.coalesce(func.nullif(User.source, ""), "(direto)")
    user_filter = [User.created_at >= since] if since else []
    paid = and_(Order.user_id == User.id, Order.status.in_([OrderStatus.PAID, OrderStatus.REFUNDED]))
    rows = session.execute(
        select(
            source.label("source"),
            func.count(distinct(User.id)).label("users"),
            func.count(distinct(Order.user_id)).label("checkout_users"),
        )
        .select_from(User)
        .outerjoin(Order, Order.user_id == User.id)
        .where(*user_filter)
        .group_by(source)
    ).all()
    sales = {
        r.source: r
        for r in session.execute(
            select(
                source.label("source"),
                func.count(distinct(User.id)).label("buyers"),
                func.count(Order.id).label("orders_paid"),
                func.coalesce(func.sum(Order.amount), 0).label("revenue"),
            )
            .select_from(User)
            .join(Order, paid)
            .where(Order.status == OrderStatus.PAID, *user_filter)
            .group_by(source)
        ).all()
    }
    result = [
        FunnelRow(
            source=r.source, users=r.users, checkout_users=r.checkout_users,
            buyers=sales[r.source].buyers if r.source in sales else 0,
            orders_paid=sales[r.source].orders_paid if r.source in sales else 0,
            revenue=Decimal(sales[r.source].revenue) if r.source in sales else Decimal("0"),
        )
        for r in rows
    ]
    return sorted(result, key=lambda r: (r.revenue, r.users), reverse=True)
