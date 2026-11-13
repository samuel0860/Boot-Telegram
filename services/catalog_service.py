"""Leitura do catálogo (planos, categorias e conteúdos) para o bot."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from database import session_scope
from models import AccessType, Category, Plan, Product, Subscription, User
from services.access_service import utcnow


@dataclass(frozen=True)
class PlanView:
    id: int
    name: str
    description: str | None
    price: Decimal
    currency: str
    access_type: AccessType
    duration_label: str
    product_names: list[str]


@dataclass(frozen=True)
class CategoryView:
    name: str
    description: str | None
    products: list[tuple[str, str | None]]  # (nome, descrição pública)


@dataclass(frozen=True)
class SubscriptionView:
    id: int
    plan_name: str
    starts_at: datetime
    expires_at: datetime | None
    valid: bool
    status: str


def list_active_plans() -> list[PlanView]:
    with session_scope() as session:
        plans = session.scalars(
            select(Plan)
            .where(Plan.is_active.is_(True))
            .options(selectinload(Plan.products))
            .order_by(Plan.sort_order, Plan.price, Plan.id)
        ).all()
        return [
            PlanView(
                id=p.id,
                name=p.name,
                description=p.description,
                price=p.price,
                currency=p.currency,
                access_type=p.access_type,
                duration_label=p.duration_label,
                product_names=[prod.name for prod in p.products if prod.is_active],
            )
            for p in plans
        ]


def list_catalog() -> list[CategoryView]:
    with session_scope() as session:
        categories = session.scalars(
            select(Category)
            .where(Category.is_active.is_(True))
            .options(selectinload(Category.products))
            .order_by(Category.sort_order, Category.name)
        ).all()
        result = []
        for c in categories:
            products = [(p.name, p.description) for p in c.products if p.is_active]
            if products:
                result.append(CategoryView(c.name, c.description, products))
        uncategorized = session.scalars(
            select(Product).where(Product.category_id.is_(None), Product.is_active.is_(True)).order_by(Product.sort_order)
        ).all()
        if uncategorized:
            result.append(CategoryView("Outros", None, [(p.name, p.description) for p in uncategorized]))
        return result


def list_user_subscriptions(telegram_id: int) -> list[SubscriptionView]:
    now = utcnow()
    with session_scope() as session:
        subs = session.scalars(
            select(Subscription)
            .join(User, User.id == Subscription.user_id)
            .where(User.telegram_id == telegram_id)
            .options(selectinload(Subscription.plan))
            .order_by(Subscription.created_at.desc())
            .limit(10)
        ).all()
        return [
            SubscriptionView(
                id=s.id,
                plan_name=s.plan.name,
                starts_at=s.starts_at,
                expires_at=s.expires_at,
                valid=s.is_valid(now),
                status=s.status.value,
            )
            for s in subs
        ]
