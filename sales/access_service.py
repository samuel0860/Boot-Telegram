"""Liberação, validação, expiração e revogação de acessos."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.orm import Session

from config import settings
from core.audit import log_event, utcnow  # noqa: F401 — reexportados para o módulo de vendas
from models import (
    ContentType,
    Order,
    OrderStatus,
    Plan,
    Product,
    Subscription,
    SubscriptionStatus,
    User,
    plan_products,
)

logger = logging.getLogger(__name__)

DELIVERY_CLAIM_TIMEOUT = timedelta(minutes=5)


# --------------------------------------------------------------------------- #
# Concessão / revogação
# --------------------------------------------------------------------------- #
def grant_access_for_order(session: Session, order: Order) -> Subscription:
    """Cria o acesso de um pedido PAGO. Idempotente (subscriptions.order_id é UNIQUE).

    Deve ser chamado dentro da transação que marcou o pedido como pago e com
    a linha do pedido bloqueada (SELECT ... FOR UPDATE).
    """
    if order.status != OrderStatus.PAID:
        raise ValueError("Acesso só pode ser liberado para pedidos pagos")

    existing = session.scalar(select(Subscription).where(Subscription.order_id == order.id))
    if existing:
        return existing

    plan = session.get(Plan, order.plan_id)
    now = utcnow()
    starts_at = now
    expires_at = None
    if plan.duration_days:
        # Renovação: soma o novo período ao final do acesso vigente do mesmo plano.
        current_end = session.scalar(
            select(func.max(Subscription.expires_at)).where(
                Subscription.user_id == order.user_id,
                Subscription.plan_id == plan.id,
                Subscription.status == SubscriptionStatus.ACTIVE,
                Subscription.expires_at > now,
            )
        )
        if current_end:
            starts_at = current_end
        expires_at = starts_at + timedelta(days=plan.duration_days)

    sub = Subscription(
        user_id=order.user_id,
        plan_id=plan.id,
        order_id=order.id,
        status=SubscriptionStatus.ACTIVE,
        starts_at=starts_at,
        expires_at=expires_at,
    )
    session.add(sub)
    session.flush()
    log_event(
        session,
        "access_granted",
        user_id=order.user_id,
        subscription_id=sub.id,
        order_id=order.id,
        plan=plan.slug,
        starts_at=starts_at.isoformat(),
        expires_at=expires_at.isoformat() if expires_at else None,
    )
    logger.info("Acesso liberado: pedido=%s usuário=%s plano=%s", order.id, order.user_id, plan.slug)
    return sub


def revoke_subscription(
    session: Session, sub: Subscription, reason: str, *, admin_user_id: int | None = None, ip: str | None = None
) -> bool:
    if sub.status == SubscriptionStatus.REVOKED:
        return False
    sub.status = SubscriptionStatus.REVOKED
    sub.revoked_at = utcnow()
    sub.revoke_reason = reason[:255]
    sub.access_removed_at = None  # o bot removerá dos grupos/canais
    log_event(
        session,
        "access_revoked",
        user_id=sub.user_id,
        subscription_id=sub.id,
        order_id=sub.order_id,
        admin_user_id=admin_user_id,
        ip_address=ip,
        reason=reason,
    )
    return True


def revoke_order_access(session: Session, order: Order, reason: str) -> None:
    sub = session.scalar(select(Subscription).where(Subscription.order_id == order.id).with_for_update())
    if sub:
        revoke_subscription(session, sub, reason)


# --------------------------------------------------------------------------- #
# Verificações de acesso
# --------------------------------------------------------------------------- #
def _valid_subscription_clause(now: datetime):
    return (
        (Subscription.status == SubscriptionStatus.ACTIVE)
        & (Subscription.starts_at <= now)
        & or_(Subscription.expires_at.is_(None), Subscription.expires_at > now)
    )


def user_has_product_access(session: Session, user_id: int, product_id: int) -> bool:
    now = utcnow()
    stmt = select(
        exists()
        .where(Subscription.user_id == user_id)
        .where(_valid_subscription_clause(now))
        .where(plan_products.c.plan_id == Subscription.plan_id)
        .where(plan_products.c.product_id == product_id)
    )
    return bool(session.scalar(stmt))


def managed_chat_ids(session: Session) -> set[int]:
    """IDs de grupos/canais cadastrados como conteúdo (ativos ou não)."""
    values = session.scalars(
        select(Product.content_value).where(Product.content_type == ContentType.TELEGRAM_CHAT)
    ).all()
    result = set()
    for v in values:
        try:
            result.add(int(v.strip()))
        except ValueError:
            continue
    return result


def is_authorized_for_chat(session: Session, telegram_id: int, chat_id: int) -> bool:
    """O usuário tem acesso válido a algum produto que corresponde a este chat?"""
    now = utcnow()
    stmt = (
        select(Subscription.id)
        .join(User, User.id == Subscription.user_id)
        .join(plan_products, plan_products.c.plan_id == Subscription.plan_id)
        .join(Product, Product.id == plan_products.c.product_id)
        .where(
            User.telegram_id == telegram_id,
            User.is_blocked.is_(False),
            Product.content_type == ContentType.TELEGRAM_CHAT,
            Product.content_value == str(chat_id),
            _valid_subscription_clause(now),
        )
        .limit(1)
    )
    return session.scalar(stmt) is not None


def get_user_subscriptions(session: Session, user_id: int) -> list[Subscription]:
    return list(
        session.scalars(
            select(Subscription)
            .where(Subscription.user_id == user_id)
            .order_by(Subscription.created_at.desc())
            .limit(20)
        ).all()
    )


# --------------------------------------------------------------------------- #
# Pacotes de entrega (dados simples para o bot, sem objetos ORM)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DeliveryItem:
    product_id: int
    name: str
    category: str | None
    content_type: ContentType
    content_value: str


@dataclass
class DeliveryPackage:
    order_id: int | None
    subscription_id: int
    user_id: int
    telegram_id: int
    plan_name: str
    starts_at: datetime
    expires_at: datetime | None
    items: list[DeliveryItem] = field(default_factory=list)


def _build_package(session: Session, sub: Subscription) -> DeliveryPackage:
    user = session.get(User, sub.user_id)
    plan = session.get(Plan, sub.plan_id)
    products = session.scalars(
        select(Product)
        .join(plan_products, plan_products.c.product_id == Product.id)
        .where(plan_products.c.plan_id == plan.id, Product.is_active.is_(True))
        .order_by(Product.sort_order, Product.id)
    ).all()
    return DeliveryPackage(
        order_id=sub.order_id,
        subscription_id=sub.id,
        user_id=user.id,
        telegram_id=user.telegram_id,
        plan_name=plan.name,
        starts_at=sub.starts_at,
        expires_at=sub.expires_at,
        items=[
            DeliveryItem(
                product_id=p.id,
                name=p.name,
                category=p.category.name if p.category else None,
                content_type=p.content_type,
                content_value=p.content_value,
            )
            for p in products
        ],
    )


def claim_orders_for_delivery(session: Session, limit: int = 10) -> list[DeliveryPackage]:
    """Reserva pedidos pagos ainda não entregues (seguro com vários workers)."""
    now = utcnow()
    orders = session.scalars(
        select(Order)
        .where(
            Order.status == OrderStatus.PAID,
            Order.delivered_at.is_(None),
            Order.delivery_attempts < settings.delivery_max_attempts,
            or_(Order.delivery_claimed_at.is_(None), Order.delivery_claimed_at < now - DELIVERY_CLAIM_TIMEOUT),
        )
        .order_by(Order.paid_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    packages = []
    for order in orders:
        sub = session.scalar(select(Subscription).where(Subscription.order_id == order.id))
        if sub is None:  # não deveria acontecer; garante consistência
            sub = grant_access_for_order(session, order)
        order.delivery_claimed_at = now
        order.delivery_attempts += 1
        packages.append(_build_package(session, sub))
    return packages


def mark_delivered(session: Session, order_id: int, errors: list[str]) -> None:
    order = session.get(Order, order_id, with_for_update=True)
    order.delivered_at = utcnow()
    order.delivery_error = "; ".join(errors)[:2000] if errors else None
    log_event(session, "content_delivered", user_id=order.user_id, order_id=order.id, errors=errors or None)


def mark_delivery_failed(session: Session, order_id: int, error: str) -> None:
    order = session.get(Order, order_id, with_for_update=True)
    order.delivery_claimed_at = None
    order.delivery_error = error[:2000]
    log_event(session, "delivery_failed", user_id=order.user_id, order_id=order.id, error=error[:500])


def package_for_resend(session: Session, telegram_id: int, subscription_id: int) -> DeliveryPackage | None:
    """Reenvio do conteúdo — somente para acessos válidos do próprio usuário."""
    sub = session.scalar(
        select(Subscription)
        .join(User, User.id == Subscription.user_id)
        .where(Subscription.id == subscription_id, User.telegram_id == telegram_id, User.is_blocked.is_(False))
    )
    if sub is None or not sub.is_valid(utcnow()):
        return None
    return _build_package(session, sub)


# --------------------------------------------------------------------------- #
# Expiração
# --------------------------------------------------------------------------- #
def expire_due_subscriptions(session: Session) -> list[int]:
    now = utcnow()
    ids = session.scalars(
        update(Subscription)
        .where(
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.expires_at.is_not(None),
            Subscription.expires_at <= now,
        )
        .values(status=SubscriptionStatus.EXPIRED, access_removed_at=None)
        .returning(Subscription.id)
    ).all()
    for sub_id in ids:
        sub = session.get(Subscription, sub_id)
        log_event(session, "access_expired", user_id=sub.user_id, subscription_id=sub.id, order_id=sub.order_id)
    if ids:
        logger.info("%d assinatura(s) expirada(s)", len(ids))
    return list(ids)


@dataclass(frozen=True)
class RemovalTask:
    subscription_id: int
    user_id: int
    telegram_id: int
    plan_name: str
    status: SubscriptionStatus
    chat_ids: list[int]
    plan_id: int | None = None


def pending_removals(session: Session, limit: int = 50) -> list[RemovalTask]:
    """Assinaturas expiradas/revogadas cujo usuário ainda não foi removido dos chats."""
    subs = session.scalars(
        select(Subscription)
        .where(
            Subscription.status.in_([SubscriptionStatus.EXPIRED, SubscriptionStatus.REVOKED]),
            Subscription.access_removed_at.is_(None),
        )
        .order_by(Subscription.id)
        .limit(limit)
    ).all()
    tasks = []
    for sub in subs:
        user = session.get(User, sub.user_id)
        plan = session.get(Plan, sub.plan_id)
        chat_ids = []
        chat_products = session.scalars(
            select(Product)
            .join(plan_products, plan_products.c.product_id == Product.id)
            .where(plan_products.c.plan_id == sub.plan_id, Product.content_type == ContentType.TELEGRAM_CHAT)
        ).all()
        for product in chat_products:
            # Só remove se o usuário não tiver OUTRO acesso válido ao mesmo chat.
            if user_has_product_access(session, user.id, product.id):
                continue
            try:
                chat_ids.append(int(product.content_value.strip()))
            except ValueError:
                logger.warning("Produto %s com chat_id inválido: %r", product.id, product.content_value)
        tasks.append(
            RemovalTask(
                subscription_id=sub.id,
                user_id=user.id,
                telegram_id=user.telegram_id,
                plan_name=plan.name,
                status=sub.status,
                chat_ids=sorted(set(chat_ids)),
                plan_id=plan.id if plan.is_active else None,
            )
        )
    return tasks


def mark_access_removed(session: Session, subscription_id: int, removed_from: list[int], errors: list[str]) -> None:
    sub = session.get(Subscription, subscription_id, with_for_update=True)
    sub.access_removed_at = utcnow()
    log_event(
        session,
        "removed_from_chats",
        user_id=sub.user_id,
        subscription_id=sub.id,
        chats=removed_from,
        errors=errors or None,
    )


@dataclass(frozen=True)
class ReminderTask:
    subscription_id: int
    telegram_id: int
    plan_name: str
    expires_at: datetime
    plan_id: int | None = None


def due_reminders(session: Session) -> list[ReminderTask]:
    if settings.expiry_reminder_days <= 0:
        return []
    now = utcnow()
    subs = session.scalars(
        select(Subscription)
        .where(
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.reminder_sent_at.is_(None),
            Subscription.expires_at.is_not(None),
            Subscription.expires_at > now,
            Subscription.expires_at <= now + timedelta(days=settings.expiry_reminder_days),
        )
        .limit(100)
    ).all()
    tasks = []
    for sub in subs:
        # Se já existe renovação futura do mesmo plano, não lembra.
        renewed = session.scalar(
            select(Subscription.id).where(
                Subscription.user_id == sub.user_id,
                Subscription.plan_id == sub.plan_id,
                Subscription.status == SubscriptionStatus.ACTIVE,
                Subscription.starts_at >= sub.expires_at,
            )
        )
        if renewed:
            sub.reminder_sent_at = now
            continue
        user = session.get(User, sub.user_id)
        plan = session.get(Plan, sub.plan_id)
        tasks.append(ReminderTask(sub.id, user.telegram_id, plan.name, sub.expires_at,
                                  plan.id if plan.is_active else None))
    return tasks


def mark_reminder_sent(session: Session, subscription_id: int) -> None:
    sub = session.get(Subscription, subscription_id)
    sub.reminder_sent_at = utcnow()
