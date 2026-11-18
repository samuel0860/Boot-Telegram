"""Painel inicial com os números do dia."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from admin.auth import (
    current_admin,
)
from admin.common import render
from config import settings
from core import alert_service, settings_service
from core.audit import utcnow
from database import get_db
from models import (
    AdminUser,
    OfferBot,
    OfferPost,
    OfferStatus,
    Order,
    OrderStatus,
    Plan,
    Product,
    Subscription,
    SubscriptionStatus,
    User,
    WebhookEvent,
    WebhookStatus,
)

router = APIRouter()


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, admin: AdminUser = Depends(current_admin), db: Session = Depends(get_db)):
    now = utcnow()
    month_ago = now - timedelta(days=30)
    paid = Order.status == OrderStatus.PAID
    stats = {
        "users": db.scalar(select(func.count(User.id))),
        "users_30d": db.scalar(select(func.count(User.id)).where(User.created_at >= month_ago)),
        "active_subs": db.scalar(
            select(func.count(Subscription.id)).where(
                Subscription.status == SubscriptionStatus.ACTIVE,
                Subscription.starts_at <= now,
                or_(Subscription.expires_at.is_(None), Subscription.expires_at > now),
            )
        ),
        "pending_orders": db.scalar(select(func.count(Order.id)).where(Order.status == OrderStatus.PENDING)),
        "revenue_total": db.scalar(select(func.coalesce(func.sum(Order.amount), 0)).where(paid)),
        "revenue_30d": db.scalar(
            select(func.coalesce(func.sum(Order.amount), 0)).where(paid, Order.paid_at >= month_ago)
        ),
        "paid_30d": db.scalar(select(func.count(Order.id)).where(paid, Order.paid_at >= month_ago)),
        "undelivered": db.scalar(select(func.count(Order.id)).where(paid, Order.delivered_at.is_(None))),
        "failed_webhooks": db.scalar(
            select(func.count(WebhookEvent.id)).where(
                WebhookEvent.status == WebhookStatus.FAILED, WebhookEvent.created_at >= month_ago
            )
        ),
    }
    recent = db.scalars(
        select(Order).options(selectinload(Order.user), selectinload(Order.plan)).order_by(Order.id.desc()).limit(10)
    ).all()
    values = settings_service.get_all(db)
    active_plans = db.scalar(select(func.count(Plan.id)).where(Plan.is_active.is_(True)))
    active_products = db.scalar(select(func.count(Product.id)).where(Product.is_active.is_(True)))
    sales_on = alert_service.bot_online(values)
    offers_on = alert_service.bot_online(values, key=alert_service.OFFERS_HEARTBEAT)
    linked = bool(alert_service.admin_ids(values))

    # Módulo de ofertas
    since = now - timedelta(hours=24)
    bots = db.scalars(select(OfferBot).order_by(OfferBot.id)).all()
    offers = {
        "bots": len(bots),
        "active": sum(1 for b in bots if b.is_active),
        "posted_24h": db.scalar(select(func.count(OfferPost.id)).where(OfferPost.posted_at >= since)),
        "wa_24h": db.scalar(select(func.count(OfferPost.id)).where(OfferPost.wa_sent_at >= since)),
        "waiting": db.scalar(select(func.count(OfferPost.id)).where(OfferPost.status == OfferStatus.NEW)),
        "failed": db.scalar(select(func.count(OfferPost.id)).where(OfferPost.status == OfferStatus.FAILED,
                                                                   OfferPost.updated_at >= since)),
    }
    ml_ok = bool((values.get("ml_app_id") and values.get("ml_secret_key"))
                 or (values.get("shopee_app_id") and values.get("shopee_secret")))
    # Primeiros passos: (feito?, título, descrição, link, texto do link, opcional?, módulo)
    setup = [
        (linked, "Vincular o seu Telegram", "Receba alertas e o resumo diário.", "/admin/account", "Vincular", False,
         "geral"),
        (sales_on, "Bot de vendas no ar", "O bot responde ao /start e executa as tarefas automáticas.",
         None, None, False, "vendas"),
        (active_plans > 0 and active_products > 0, "Planos e conteúdos",
         "Cadastre o que você vende e o conteúdo liberado após o pagamento.", "/admin/plans", "Ver planos", False,
         "vendas"),
        (settings.payments_configured, f"Pagamentos ({settings.payment_gateway_label})",
         "Credenciais do gateway no .env para o bot gerar cobranças.", None, None, False, "vendas"),
        (ml_ok, "Lojas (Mercado Livre / Shopee)", "Credenciais das APIs de afiliado, usadas por todos os bots.",
         "/admin/offers/settings", "Configurar", True, "ofertas"),
        (offers["active"] > 0, "Primeiro bot de ofertas ligado",
         "Token do Telegram e/ou WhatsApp, destinos e filtros.", "/admin/offers/bots", "Configurar", True, "ofertas"),
    ]
    return render(request, "admin/dashboard.html", admin, stats=stats, recent=recent,
                  payments_configured=settings.payments_configured, gateway=settings.payment_gateway_label,
                  webhook_configured=bool(settings.mp_webhook_secret),
                  setup=setup, setup_done=sum(1 for s in setup if s[0]), sales_online=sales_on,
                  offers_online=offers_on, offers=offers)
