"""Painel administrativo (HTML renderizado no servidor).

Permissões:
- Qualquer administrador ativo: consultar tudo, bloquear/desbloquear usuários
  e encerrar acessos.
- Superusuário: gerenciar planos, preços, categorias, conteúdos e administradores.
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from math import ceil
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from admin.auth import (
    authenticate,
    client_ip,
    current_admin,
    get_csrf_token,
    hash_password,
    login_session,
    require_superuser,
    verify_csrf,
    verify_password,
)
from config import BASE_DIR, settings
from database import get_db
from models import (
    AccessLog,
    AccessType,
    AdminUser,
    Category,
    ContentType,
    OfferStatus,
    Order,
    OrderStatus,
    Payment,
    PaymentStatus,
    Plan,
    Product,
    PromoOffer,
    Subscription,
    SubscriptionStatus,
    User,
    WaStatus,
    WebhookEvent,
    WebhookStatus,
)
from services import alert_service, promo_service, settings_service, whatsapp_service
from services.access_service import log_event, revoke_subscription, utcnow
from services.mercadolivre import MLError
from services.whatsapp_service import WhatsAppError
from services.texts import brl

router = APIRouter(prefix="/admin", include_in_schema=False)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
TZ = ZoneInfo(settings.display_timezone)
PER_PAGE = 50
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _fmt_dt(value) -> str:
    return value.astimezone(TZ).strftime("%d/%m/%Y %H:%M") if value else "—"


templates.env.filters["brl"] = lambda v: brl(v) if v is not None else "—"
templates.env.filters["dt"] = _fmt_dt
templates.env.globals["csrf_token"] = get_csrf_token


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def render(request: Request, template: str, admin: AdminUser | None, **ctx: Any) -> HTMLResponse:
    flashes = request.session.pop("flash", [])
    return templates.TemplateResponse(
        request, template, {"admin": admin, "flashes": flashes, "now": utcnow(), **ctx}
    )


def flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", []).append({"kind": kind, "message": message})


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def paginate(db: Session, stmt, page: int) -> tuple[list, dict]:
    page = max(page, 1)
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    items = db.scalars(stmt.offset((page - 1) * PER_PAGE).limit(PER_PAGE)).all()
    return list(items), {"page": page, "pages": max(ceil(total / PER_PAGE), 1), "total": total}


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value[:100]


def parse_price(raw: str) -> Decimal:
    raw = raw.strip().replace("R$", "").replace(" ", "")
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    try:
        value = Decimal(raw).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValueError("Preço inválido. Use o formato 9,90.") from exc
    if value <= 0 or value > Decimal("100000"):
        raise ValueError("O preço deve ser maior que zero e menor que 100.000.")
    return value


def parse_optional_int(raw: str, field: str, minimum: int = 1) -> int | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    if not raw.lstrip("-").isdigit() or int(raw) < minimum:
        raise ValueError(f"{field} inválido.")
    return int(raw)


def audit(db: Session, request: Request, admin: AdminUser, action: str, **details: Any) -> None:
    log_event(db, action, admin_user_id=admin.id, ip_address=client_ip(request), **details)


# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #
@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get("admin_id"):
        return redirect("/admin/")
    return render(request, "admin/login.html", None, error=None)


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(..., max_length=64),
    password: str = Form(..., max_length=256),
    _csrf: None = Depends(verify_csrf),
    db: Session = Depends(get_db),
):
    admin, error = authenticate(db, username, password, client_ip(request))
    if admin is None:
        return render(request, "admin/login.html", None, error=error)
    login_session(request, admin)
    return redirect("/admin/")


@router.post("/logout")
def logout(request: Request, _csrf: None = Depends(verify_csrf)):
    request.session.clear()
    return redirect("/admin/login")


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
    promo_cfg = settings_service.promo_config(values)
    wa_cfg = whatsapp_service.wa_config(values)
    active_plans = db.scalar(select(func.count(Plan.id)).where(Plan.is_active.is_(True)))
    active_products = db.scalar(select(func.count(Product.id)).where(Product.is_active.is_(True)))
    bot_on = alert_service.bot_online(values)
    linked = bool(alert_service.admin_ids(values))
    # Primeiros passos: (feito?, título, descrição, link, texto do link, opcional?)
    setup = [
        (bot_on, "Bot do Telegram no ar", "O bot responde ao /start e executa as tarefas automáticas.",
         None, None, False),
        (linked, "Vincular o seu Telegram", "Receba alertas, o resumo diário e poste ofertas colando links no bot.",
         "/admin/account", "Vincular", False),
        (active_plans > 0 and active_products > 0, "Planos e conteúdos",
         "Cadastre o que você vende e o conteúdo liberado após o pagamento.", "/admin/plans", "Ver planos", False),
        (settings.payments_configured, "Pagamentos PIX (Mercado Pago)",
         "Coloque o MP_ACCESS_TOKEN no .env para o bot gerar cobranças.", None, None, False),
        (promo_cfg.credentials_ok and bool(promo_cfg.channel_id), "Bot de promoções",
         "Credenciais do Mercado Livre e o canal onde as ofertas serão postadas.", "/admin/promo", "Configurar", True),
        (wa_cfg.ready, "Ofertas no WhatsApp", "Conecte um número e escolha os grupos.", "/admin/whatsapp",
         "Configurar", True),
    ]
    return render(request, "admin/dashboard.html", admin, stats=stats, recent=recent,
                  payments_configured=settings.payments_configured,
                  webhook_configured=bool(settings.mp_webhook_secret),
                  setup=setup, setup_done=sum(1 for s in setup if s[0]), bot_online=bot_on,
                  promo_enabled=promo_cfg.enabled, wa_enabled=wa_cfg.enabled,
                  promo_stats=promo_service.stats())


# --------------------------------------------------------------------------- #
# Usuários
# --------------------------------------------------------------------------- #
@router.get("/users", response_class=HTMLResponse)
def users_list(request: Request, q: str = "", page: int = 1,
               admin: AdminUser = Depends(current_admin), db: Session = Depends(get_db)):
    stmt = select(User).order_by(User.id.desc())
    q = q.strip()
    if q:
        like = f"%{q}%"
        conds = [User.username.ilike(like), User.first_name.ilike(like), User.last_name.ilike(like)]
        if q.lstrip("-").isdigit():
            conds.append(User.telegram_id == int(q))
        stmt = stmt.where(or_(*conds))
    users, pager = paginate(db, stmt, page)
    return render(request, "admin/users.html", admin, users=users, pager=pager, q=q)


@router.get("/users/{user_id}", response_class=HTMLResponse)
def user_detail(user_id: int, request: Request, admin: AdminUser = Depends(current_admin), db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404)
    orders = db.scalars(
        select(Order).where(Order.user_id == user.id).options(selectinload(Order.plan), selectinload(Order.payments))
        .order_by(Order.id.desc())
    ).all()
    subs = db.scalars(
        select(Subscription).where(Subscription.user_id == user.id).options(selectinload(Subscription.plan))
        .order_by(Subscription.id.desc())
    ).all()
    logs = db.scalars(
        select(AccessLog).where(AccessLog.user_id == user.id).order_by(AccessLog.id.desc()).limit(100)
    ).all()
    return render(request, "admin/user_detail.html", admin, user=user, orders=orders, subs=subs, logs=logs)


@router.post("/users/{user_id}/block")
def user_block(user_id: int, request: Request, block: str = Form(...), _csrf: None = Depends(verify_csrf),
               admin: AdminUser = Depends(current_admin), db: Session = Depends(get_db)):
    user = db.get(User, user_id, with_for_update=True)
    if user is None:
        raise HTTPException(404)
    user.is_blocked = block == "1"
    action = "user_blocked" if user.is_blocked else "user_unblocked"
    log_event(db, action, user_id=user.id, admin_user_id=admin.id, ip_address=client_ip(request))
    db.commit()
    flash(request, "Usuário bloqueado." if user.is_blocked else "Usuário desbloqueado.")
    return redirect(f"/admin/users/{user_id}")


# --------------------------------------------------------------------------- #
# Pedidos, pagamentos (transações) e assinaturas
# --------------------------------------------------------------------------- #
@router.get("/orders", response_class=HTMLResponse)
def orders_list(request: Request, status: str = "", page: int = 1,
                admin: AdminUser = Depends(current_admin), db: Session = Depends(get_db)):
    stmt = select(Order).options(selectinload(Order.user), selectinload(Order.plan)).order_by(Order.id.desc())
    if status in {s.value for s in OrderStatus}:
        stmt = stmt.where(Order.status == OrderStatus(status))
    orders, pager = paginate(db, stmt, page)
    return render(request, "admin/orders.html", admin, orders=orders, pager=pager, status=status,
                  statuses=[s.value for s in OrderStatus])


@router.get("/payments", response_class=HTMLResponse)
def payments_list(request: Request, status: str = "", page: int = 1,
                  admin: AdminUser = Depends(current_admin), db: Session = Depends(get_db)):
    stmt = (
        select(Payment)
        .options(selectinload(Payment.order).selectinload(Order.user), selectinload(Payment.order).selectinload(Order.plan))
        .order_by(Payment.id.desc())
    )
    if status in {s.value for s in PaymentStatus}:
        stmt = stmt.where(Payment.status == PaymentStatus(status))
    payments, pager = paginate(db, stmt, page)
    return render(request, "admin/payments.html", admin, payments=payments, pager=pager, status=status,
                  statuses=[s.value for s in PaymentStatus])


@router.get("/subscriptions", response_class=HTMLResponse)
def subscriptions_list(request: Request, status: str = "active", page: int = 1,
                       admin: AdminUser = Depends(current_admin), db: Session = Depends(get_db)):
    now = utcnow()
    stmt = select(Subscription).options(selectinload(Subscription.user), selectinload(Subscription.plan))
    if status == "active":
        stmt = stmt.where(
            Subscription.status == SubscriptionStatus.ACTIVE,
            or_(Subscription.expires_at.is_(None), Subscription.expires_at > now),
        ).order_by(Subscription.expires_at.asc().nulls_last())
    elif status == "expired":
        stmt = stmt.where(
            or_(
                Subscription.status == SubscriptionStatus.EXPIRED,
                (Subscription.status == SubscriptionStatus.ACTIVE) & (Subscription.expires_at <= now),
            )
        ).order_by(Subscription.expires_at.desc())
    elif status == "revoked":
        stmt = stmt.where(Subscription.status == SubscriptionStatus.REVOKED).order_by(Subscription.revoked_at.desc())
    else:
        status = "all"
        stmt = stmt.order_by(Subscription.id.desc())
    subs, pager = paginate(db, stmt, page)
    return render(request, "admin/subscriptions.html", admin, subs=subs, pager=pager, status=status)


@router.post("/subscriptions/{sub_id}/revoke")
def subscription_revoke(sub_id: int, request: Request, reason: str = Form("", max_length=200),
                        _csrf: None = Depends(verify_csrf),
                        admin: AdminUser = Depends(current_admin), db: Session = Depends(get_db)):
    sub = db.get(Subscription, sub_id, with_for_update=True)
    if sub is None:
        raise HTTPException(404)
    if revoke_subscription(db, sub, reason.strip() or "encerrado pelo administrador",
                           admin_user_id=admin.id, ip=client_ip(request)):
        db.commit()
        flash(request, "Acesso encerrado. O bot removerá o usuário dos grupos/canais em instantes.")
    else:
        flash(request, "Este acesso já estava encerrado.", "warn")
    return redirect(request.headers.get("referer") or "/admin/subscriptions")


# --------------------------------------------------------------------------- #
# Planos / ofertas
# --------------------------------------------------------------------------- #
@router.get("/plans", response_class=HTMLResponse)
def plans_list(request: Request, admin: AdminUser = Depends(current_admin), db: Session = Depends(get_db)):
    plans = db.scalars(select(Plan).options(selectinload(Plan.products)).order_by(Plan.sort_order, Plan.id)).all()
    sold = dict(
        db.execute(select(Order.plan_id, func.count(Order.id)).where(Order.status == OrderStatus.PAID)
                   .group_by(Order.plan_id)).all()
    )
    return render(request, "admin/plans.html", admin, plans=plans, sold=sold)


def _plan_form(request: Request, admin: AdminUser, db: Session, plan: Plan | None, form: dict, error: str | None = None):
    products = db.scalars(select(Product).options(selectinload(Product.category)).order_by(Product.sort_order, Product.id)).all()
    return render(request, "admin/plan_form.html", admin, plan=plan, form=form, error=error, products=products,
                  access_types=[a.value for a in AccessType])


def _plan_to_form(plan: Plan | None) -> dict:
    if plan is None:
        return {"name": "", "slug": "", "description": "", "price": "", "access_type": "subscription",
                "duration_days": "30", "sort_order": "0", "is_active": True, "product_ids": []}
    return {
        "name": plan.name, "slug": plan.slug, "description": plan.description or "",
        "price": f"{plan.price:.2f}".replace(".", ","), "access_type": plan.access_type.value,
        "duration_days": str(plan.duration_days or ""), "sort_order": str(plan.sort_order),
        "is_active": plan.is_active, "product_ids": [p.id for p in plan.products],
    }


def _apply_plan_form(db: Session, plan: Plan, form: dict) -> None:
    name = form["name"].strip()
    if not name:
        raise ValueError("Informe o nome do plano.")
    slug = (form["slug"].strip() or slugify(name)).lower()
    if not SLUG_RE.match(slug):
        raise ValueError("Slug inválido (use letras minúsculas, números e hífens).")
    access_type = AccessType(form["access_type"])
    duration = parse_optional_int(form["duration_days"], "Duração")
    if access_type == AccessType.SUBSCRIPTION and not duration:
        raise ValueError("Assinaturas precisam de duração em dias.")
    plan.name = name[:100]
    plan.slug = slug
    plan.description = form["description"].strip() or None
    plan.price = parse_price(form["price"])
    plan.currency = settings.currency
    plan.access_type = access_type
    plan.duration_days = duration
    plan.sort_order = parse_optional_int(form["sort_order"], "Ordem", minimum=0) or 0
    plan.is_active = bool(form["is_active"])
    ids = [int(i) for i in form["product_ids"]]
    plan.products = list(db.scalars(select(Product).where(Product.id.in_(ids))).all()) if ids else []


def _plan_form_data(name, slug, description, price, access_type, duration_days, sort_order, is_active, product_ids) -> dict:
    return {"name": name, "slug": slug, "description": description, "price": price, "access_type": access_type,
            "duration_days": duration_days, "sort_order": sort_order, "is_active": is_active == "1",
            "product_ids": product_ids}


@router.get("/plans/new", response_class=HTMLResponse)
def plan_new(request: Request, admin: AdminUser = Depends(require_superuser), db: Session = Depends(get_db)):
    return _plan_form(request, admin, db, None, _plan_to_form(None))


@router.get("/plans/{plan_id}/edit", response_class=HTMLResponse)
def plan_edit(plan_id: int, request: Request, admin: AdminUser = Depends(require_superuser), db: Session = Depends(get_db)):
    plan = db.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(404)
    return _plan_form(request, admin, db, plan, _plan_to_form(plan))


@router.post("/plans/save")
def plan_save(
    request: Request,
    plan_id: str = Form(""),
    name: str = Form("", max_length=100),
    slug: str = Form("", max_length=100),
    description: str = Form("", max_length=2000),
    price: str = Form("", max_length=20),
    access_type: str = Form("subscription"),
    duration_days: str = Form("", max_length=6),
    sort_order: str = Form("0", max_length=6),
    is_active: str = Form(""),
    product_ids: list[int] = Form(default=[]),
    _csrf: None = Depends(verify_csrf),
    admin: AdminUser = Depends(require_superuser),
    db: Session = Depends(get_db),
):
    form = _plan_form_data(name, slug, description, price, access_type, duration_days, sort_order, is_active, product_ids)
    plan = db.get(Plan, int(plan_id), with_for_update=True) if plan_id.isdigit() else None
    if plan_id.isdigit() and plan is None:
        raise HTTPException(404)
    is_new = plan is None
    if is_new:
        plan = Plan()
    old_price = None if is_new else plan.price
    try:
        _apply_plan_form(db, plan, form)
        if is_new:
            db.add(plan)
        db.flush()
        audit(db, request, admin, "plan_created" if is_new else "plan_updated", plan_id=plan.id, slug=plan.slug,
              price=str(plan.price), old_price=str(old_price) if old_price is not None else None)
        db.commit()
    except ValueError as exc:
        db.rollback()
        return _plan_form(request, admin, db, None if is_new else db.get(Plan, int(plan_id)), form, str(exc))
    except IntegrityError:
        db.rollback()
        return _plan_form(request, admin, db, None if is_new else db.get(Plan, int(plan_id)), form,
                          "Já existe um plano com este slug.")
    flash(request, "Plano salvo com sucesso.")
    return redirect("/admin/plans")


@router.post("/plans/{plan_id}/toggle")
def plan_toggle(plan_id: int, request: Request, _csrf: None = Depends(verify_csrf),
                admin: AdminUser = Depends(require_superuser), db: Session = Depends(get_db)):
    plan = db.get(Plan, plan_id, with_for_update=True)
    if plan is None:
        raise HTTPException(404)
    plan.is_active = not plan.is_active
    audit(db, request, admin, "plan_toggled", plan_id=plan.id, is_active=plan.is_active)
    db.commit()
    flash(request, f"Oferta {'ativada' if plan.is_active else 'desativada'}.")
    return redirect("/admin/plans")


# --------------------------------------------------------------------------- #
# Categorias
# --------------------------------------------------------------------------- #
@router.get("/categories", response_class=HTMLResponse)
def categories_list(request: Request, admin: AdminUser = Depends(current_admin), db: Session = Depends(get_db)):
    categories = db.scalars(select(Category).order_by(Category.sort_order, Category.name)).all()
    counts = dict(db.execute(select(Product.category_id, func.count(Product.id)).group_by(Product.category_id)).all())
    return render(request, "admin/categories.html", admin, categories=categories, counts=counts)


@router.post("/categories/save")
def category_save(
    request: Request,
    category_id: str = Form(""),
    name: str = Form(..., max_length=100),
    description: str = Form("", max_length=1000),
    sort_order: str = Form("0", max_length=6),
    _csrf: None = Depends(verify_csrf),
    admin: AdminUser = Depends(require_superuser),
    db: Session = Depends(get_db),
):
    name = name.strip()
    if not name:
        flash(request, "Informe o nome da categoria.", "error")
        return redirect("/admin/categories")
    category = db.get(Category, int(category_id)) if category_id.isdigit() else None
    is_new = category is None
    if is_new:
        category = Category(slug=slugify(name) or "categoria")
        db.add(category)
    category.name = name
    category.description = description.strip() or None
    try:
        category.sort_order = parse_optional_int(sort_order, "Ordem", minimum=0) or 0
        db.flush()
        audit(db, request, admin, "category_saved", category_id=category.id)
        db.commit()
        flash(request, "Categoria salva.")
    except (ValueError, IntegrityError) as exc:
        db.rollback()
        msg = "Já existe uma categoria com esse nome." if isinstance(exc, IntegrityError) else str(exc)
        flash(request, msg, "error")
    return redirect("/admin/categories")


@router.post("/categories/{category_id}/toggle")
def category_toggle(category_id: int, request: Request, _csrf: None = Depends(verify_csrf),
                    admin: AdminUser = Depends(require_superuser), db: Session = Depends(get_db)):
    category = db.get(Category, category_id)
    if category is None:
        raise HTTPException(404)
    category.is_active = not category.is_active
    audit(db, request, admin, "category_toggled", category_id=category.id, is_active=category.is_active)
    db.commit()
    return redirect("/admin/categories")


# --------------------------------------------------------------------------- #
# Produtos / conteúdos
# --------------------------------------------------------------------------- #
@router.get("/products", response_class=HTMLResponse)
def products_list(request: Request, admin: AdminUser = Depends(current_admin), db: Session = Depends(get_db)):
    products = db.scalars(
        select(Product).options(selectinload(Product.category), selectinload(Product.plans))
        .order_by(Product.category_id.nulls_last(), Product.sort_order, Product.id)
    ).all()
    return render(request, "admin/products.html", admin, products=products)


def _product_form(request, admin, db, product, form, error=None):
    categories = db.scalars(select(Category).order_by(Category.sort_order, Category.name)).all()
    return render(request, "admin/product_form.html", admin, product=product, form=form, error=error,
                  categories=categories, content_types=[c.value for c in ContentType])


def _product_to_form(product: Product | None) -> dict:
    if product is None:
        return {"name": "", "slug": "", "description": "", "category_id": "", "content_type": "text",
                "content_value": "", "sort_order": "0", "is_active": True}
    return {"name": product.name, "slug": product.slug, "description": product.description or "",
            "category_id": str(product.category_id or ""), "content_type": product.content_type.value,
            "content_value": product.content_value, "sort_order": str(product.sort_order),
            "is_active": product.is_active}


def _validate_content(content_type: ContentType, value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("Informe o conteúdo.")
    if content_type == ContentType.TELEGRAM_CHAT and not re.fullmatch(r"-100\d{5,}", value):
        raise ValueError("Para grupo/canal, informe o ID numérico (ex.: -1001234567890). Use /chatid no grupo.")
    if content_type == ContentType.LINK and not value.startswith("https://"):
        raise ValueError("O link deve começar com https://")
    return value


@router.get("/products/new", response_class=HTMLResponse)
def product_new(request: Request, admin: AdminUser = Depends(require_superuser), db: Session = Depends(get_db)):
    return _product_form(request, admin, db, None, _product_to_form(None))


@router.get("/products/{product_id}/edit", response_class=HTMLResponse)
def product_edit(product_id: int, request: Request, admin: AdminUser = Depends(require_superuser),
                 db: Session = Depends(get_db)):
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(404)
    return _product_form(request, admin, db, product, _product_to_form(product))


@router.post("/products/save")
def product_save(
    request: Request,
    product_id: str = Form(""),
    name: str = Form("", max_length=150),
    slug: str = Form("", max_length=100),
    description: str = Form("", max_length=2000),
    category_id: str = Form(""),
    content_type: str = Form("text"),
    content_value: str = Form("", max_length=4000),
    sort_order: str = Form("0", max_length=6),
    is_active: str = Form(""),
    _csrf: None = Depends(verify_csrf),
    admin: AdminUser = Depends(require_superuser),
    db: Session = Depends(get_db),
):
    form = {"name": name, "slug": slug, "description": description, "category_id": category_id,
            "content_type": content_type, "content_value": content_value, "sort_order": sort_order,
            "is_active": is_active == "1"}
    product = db.get(Product, int(product_id)) if product_id.isdigit() else None
    is_new = product is None
    try:
        if not name.strip():
            raise ValueError("Informe o nome.")
        final_slug = (slug.strip() or slugify(name)).lower()
        if not SLUG_RE.match(final_slug):
            raise ValueError("Slug inválido.")
        ctype = ContentType(content_type)
        if is_new:
            product = Product()
            db.add(product)
        product.name = name.strip()
        product.slug = final_slug
        product.description = description.strip() or None
        product.category_id = int(category_id) if category_id.isdigit() else None
        product.content_type = ctype
        product.content_value = _validate_content(ctype, content_value)
        product.sort_order = parse_optional_int(sort_order, "Ordem", minimum=0) or 0
        product.is_active = form["is_active"]
        db.flush()
        audit(db, request, admin, "product_saved", product_id=product.id, created=is_new)
        db.commit()
    except ValueError as exc:
        db.rollback()
        return _product_form(request, admin, db, None if is_new else db.get(Product, int(product_id)), form, str(exc))
    except IntegrityError:
        db.rollback()
        return _product_form(request, admin, db, None if is_new else db.get(Product, int(product_id)), form,
                             "Já existe um conteúdo com este slug.")
    flash(request, "Conteúdo salvo.")
    return redirect("/admin/products")


@router.post("/products/{product_id}/toggle")
def product_toggle(product_id: int, request: Request, _csrf: None = Depends(verify_csrf),
                   admin: AdminUser = Depends(require_superuser), db: Session = Depends(get_db)):
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(404)
    product.is_active = not product.is_active
    audit(db, request, admin, "product_toggled", product_id=product.id, is_active=product.is_active)
    db.commit()
    return redirect("/admin/products")


# --------------------------------------------------------------------------- #
# Histórico: logs de acesso e webhooks
# --------------------------------------------------------------------------- #
@router.get("/logs", response_class=HTMLResponse)
def logs_list(request: Request, action: str = "", page: int = 1,
              admin: AdminUser = Depends(current_admin), db: Session = Depends(get_db)):
    stmt = select(AccessLog).options(selectinload(AccessLog.user), selectinload(AccessLog.admin_user)) \
        .order_by(AccessLog.id.desc())
    if action:
        stmt = stmt.where(AccessLog.action == action)
    logs, pager = paginate(db, stmt, page)
    actions = db.scalars(select(AccessLog.action).distinct().order_by(AccessLog.action)).all()
    return render(request, "admin/logs.html", admin, logs=logs, pager=pager, action=action, actions=actions)


@router.get("/webhooks", response_class=HTMLResponse)
def webhooks_list(request: Request, status: str = "", page: int = 1,
                  admin: AdminUser = Depends(current_admin), db: Session = Depends(get_db)):
    stmt = select(WebhookEvent).order_by(WebhookEvent.id.desc())
    if status in {s.value for s in WebhookStatus}:
        stmt = stmt.where(WebhookEvent.status == WebhookStatus(status))
    events, pager = paginate(db, stmt, page)
    return render(request, "admin/webhooks.html", admin, events=events, pager=pager, status=status,
                  statuses=[s.value for s in WebhookStatus])


# --------------------------------------------------------------------------- #
# Administradores (somente superusuário)
# --------------------------------------------------------------------------- #
@router.get("/admins", response_class=HTMLResponse)
def admins_list(request: Request, admin: AdminUser = Depends(require_superuser), db: Session = Depends(get_db)):
    admins = db.scalars(select(AdminUser).order_by(AdminUser.id)).all()
    return render(request, "admin/admins.html", admin, admins=admins)


@router.post("/admins/create")
def admins_create(
    request: Request,
    username: str = Form(..., max_length=64),
    email: str = Form("", max_length=255),
    password: str = Form(..., max_length=256),
    is_superuser: str = Form(""),
    _csrf: None = Depends(verify_csrf),
    admin: AdminUser = Depends(require_superuser),
    db: Session = Depends(get_db),
):
    username = username.strip().lower()
    if not re.fullmatch(r"[a-z0-9_.-]{3,64}", username):
        flash(request, "Usuário inválido (3-64 caracteres: letras, números, _ . -).", "error")
        return redirect("/admin/admins")
    try:
        new = AdminUser(username=username, email=email.strip().lower() or None,
                        password_hash=hash_password(password), is_superuser=is_superuser == "1")
        db.add(new)
        db.flush()
        audit(db, request, admin, "admin_created", target_admin_id=new.id, superuser=new.is_superuser)
        db.commit()
        flash(request, f"Administrador {username} criado.")
    except ValueError as exc:
        db.rollback()
        flash(request, str(exc), "error")
    except IntegrityError:
        db.rollback()
        flash(request, "Usuário ou e-mail já cadastrado.", "error")
    return redirect("/admin/admins")


@router.post("/admins/{admin_id}/toggle")
def admins_toggle(admin_id: int, request: Request, _csrf: None = Depends(verify_csrf),
                  admin: AdminUser = Depends(require_superuser), db: Session = Depends(get_db)):
    target = db.get(AdminUser, admin_id)
    if target is None:
        raise HTTPException(404)
    if target.id == admin.id:
        flash(request, "Você não pode desativar sua própria conta.", "error")
        return redirect("/admin/admins")
    target.is_active = not target.is_active
    audit(db, request, admin, "admin_toggled", target_admin_id=target.id, is_active=target.is_active)
    db.commit()
    return redirect("/admin/admins")


@router.post("/password")
def change_password(
    request: Request,
    current_password: str = Form(..., max_length=256),
    new_password: str = Form(..., max_length=256),
    _csrf: None = Depends(verify_csrf),
    admin: AdminUser = Depends(current_admin),
    db: Session = Depends(get_db),
):
    if not verify_password(admin.password_hash, current_password):
        flash(request, "Senha atual incorreta.", "error")
        return redirect("/admin/account")
    try:
        admin.password_hash = hash_password(new_password)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return redirect("/admin/account")
    audit(db, request, admin, "admin_password_changed")
    db.commit()
    flash(request, "Senha alterada.")
    return redirect("/admin/account")


@router.get("/account", response_class=HTMLResponse)
def account(request: Request, admin: AdminUser = Depends(current_admin), db: Session = Depends(get_db)):
    values = settings_service.get_all(db)
    return render(request, "admin/account.html", admin,
                  linked_ids=sorted(alert_service.linked_admin_ids(values)),
                  env_ids=sorted(settings.admin_telegram_ids),
                  link_code=alert_service.current_link_code(values),
                  bot_username=settings.telegram_bot_username,
                  alerts_enabled=values.get("alerts_enabled") == "1",
                  summary_hour=values.get("daily_summary_hour") or "21")


@router.post("/account/telegram/code")
def account_telegram_code(request: Request, _csrf: None = Depends(verify_csrf),
                          admin: AdminUser = Depends(current_admin)):
    alert_service.create_link_code(admin.id)
    return redirect("/admin/account#telegram")


@router.post("/account/telegram/unlink")
def account_telegram_unlink(request: Request, telegram_id: int = Form(...), _csrf: None = Depends(verify_csrf),
                            admin: AdminUser = Depends(require_superuser)):
    alert_service.unlink_admin(telegram_id, admin.id)
    flash(request, "Telegram desvinculado.")
    return redirect("/admin/account#telegram")


@router.post("/account/alerts")
def account_alerts(request: Request, alerts_enabled: str = Form(""), daily_summary_hour: str = Form("21"),
                   _csrf: None = Depends(verify_csrf), admin: AdminUser = Depends(require_superuser),
                   db: Session = Depends(get_db)):
    hour = daily_summary_hour if daily_summary_hour.isdigit() and 0 <= int(daily_summary_hour) <= 23 else "21"
    settings_service.set_values(db, {"alerts_enabled": "1" if alerts_enabled == "1" else "0",
                                     "daily_summary_hour": hour}, admin.id)
    db.commit()
    flash(request, "Preferências de alertas salvas.")
    return redirect("/admin/account#telegram")


# --------------------------------------------------------------------------- #
# Bot de promoções (Mercado Livre)
# --------------------------------------------------------------------------- #
# Pode ser substituída nos testes
promo_client_factory = promo_service.default_client_factory
CHANNEL_RE = re.compile(r"^(@[A-Za-z][A-Za-z0-9_]{4,31}|-100\d{6,})$")


@router.get("/promo", response_class=HTMLResponse)
def promo_home(request: Request, admin: AdminUser = Depends(current_admin), db: Session = Depends(get_db)):
    values = settings_service.get_all(db)
    cfg = settings_service.promo_config(values)
    return render(
        request, "admin/promo.html", admin,
        values=values, cfg=cfg, secret_mask=settings_service.mask(values["ml_secret_key"]),
        categories=settings_service.ML_CATEGORIES,
        selected_categories=set(cfg.categories),
        extra_categories=", ".join(c for c in cfg.categories if c not in settings_service.ML_CATEGORIES),
        stats=promo_service.stats(), last=promo_service.last_result(values),
        last_run=values["promo_last_run_at"], test_results=request.session.pop("promo_test", None),
    )


@router.post("/promo/settings")
def promo_save(
    request: Request,
    ml_app_id: str = Form("", max_length=64),
    ml_secret_key: str = Form("", max_length=128),
    promo_channel_id: str = Form("", max_length=64),
    promo_mode: str = Form("auto"),
    promo_interval_minutes: str = Form("45", max_length=5),
    promo_max_posts_per_cycle: str = Form("3", max_length=3),
    promo_spacing_seconds: str = Form("120", max_length=5),
    promo_min_discount: str = Form("20", max_length=3),
    promo_min_price: str = Form("0", max_length=12),
    promo_max_price: str = Form("0", max_length=12),
    promo_blocked_words: str = Form("", max_length=2000),
    categories: list[str] = Form(default=[]),
    extra_categories: str = Form("", max_length=500),
    promo_keywords: str = Form("", max_length=1000),
    promo_no_repeat_days: str = Form("7", max_length=4),
    promo_active_hours: str = Form("", max_length=20),
    promo_footer: str = Form("", max_length=500),
    _csrf: None = Depends(verify_csrf),
    admin: AdminUser = Depends(require_superuser),
    db: Session = Depends(get_db),
):
    errors = []
    app_id = ml_app_id.strip()
    if app_id and not app_id.isdigit():
        errors.append("O App ID do Mercado Livre é numérico (ex.: 1234567890123456).")
    channel = promo_channel_id.strip()
    if channel and not CHANNEL_RE.match(channel):
        errors.append("Canal inválido: use @nomedocanal ou o ID numérico (ex.: -1001234567890).")
    if promo_active_hours.strip() and settings_service.parse_hours(promo_active_hours) == (None, None):
        errors.append("Horário ativo inválido: use o formato 08:00-23:00 (ou deixe vazio para 24h).")
    all_categories = [c for c in categories if c in settings_service.ML_CATEGORIES]
    for c in settings_service.split_list(extra_categories):
        if not re.fullmatch(r"MLB\d+", c.upper()):
            errors.append(f"Categoria inválida: {c} (formato MLB1234).")
        else:
            all_categories.append(c.upper())
    if errors:
        for e in errors:
            flash(request, e, "error")
        return redirect("/admin/promo")

    values = {
        "ml_app_id": app_id,
        "promo_channel_id": channel,
        "promo_mode": "manual" if promo_mode == "manual" else "auto",
        "promo_interval_minutes": promo_interval_minutes,
        "promo_max_posts_per_cycle": promo_max_posts_per_cycle,
        "promo_spacing_seconds": promo_spacing_seconds,
        "promo_min_discount": promo_min_discount,
        "promo_min_price": promo_min_price,
        "promo_max_price": promo_max_price,
        "promo_blocked_words": promo_blocked_words.strip(),
        "promo_categories": ",".join(dict.fromkeys(all_categories)),
        "promo_keywords": promo_keywords.strip(),
        "promo_no_repeat_days": promo_no_repeat_days,
        "promo_active_hours": promo_active_hours.strip(),
        "promo_footer": promo_footer.strip(),
    }
    if ml_secret_key.strip():  # vazio = mantém a chave atual
        values["ml_secret_key"] = ml_secret_key.strip()
    settings_service.set_values(db, values, admin.id)
    audit(db, request, admin, "promo_settings_saved", secret_changed=bool(ml_secret_key.strip()),
          channel=channel, categories=len(all_categories))
    db.commit()
    flash(request, "Configurações do bot de promoções salvas. O bot aplica no próximo ciclo (sem reiniciar).")
    return redirect("/admin/promo")


@router.post("/promo/toggle")
def promo_toggle(request: Request, _csrf: None = Depends(verify_csrf),
                 admin: AdminUser = Depends(require_superuser), db: Session = Depends(get_db)):
    values = settings_service.get_all(db)
    cfg = settings_service.promo_config(values)
    enable = not cfg.enabled
    if enable and not (cfg.credentials_ok and cfg.channel_id):
        flash(request, "Antes de ligar, preencha App ID, Secret Key e o canal.", "error")
        return redirect("/admin/promo")
    settings_service.set_values(db, {"promo_enabled": "1" if enable else "0"}, admin.id)
    audit(db, request, admin, "promo_toggled", enabled=enable)
    db.commit()
    flash(request, "Bot de promoções LIGADO." if enable else "Bot de promoções desligado.")
    return redirect("/admin/promo")


@router.post("/promo/test")
def promo_test(request: Request, _csrf: None = Depends(verify_csrf),
               admin: AdminUser = Depends(require_superuser), db: Session = Depends(get_db)):
    cfg = settings_service.load_promo_config(db)
    try:
        with promo_client_factory(cfg) as client:
            steps = client.test_connection((cfg.categories or ["MLB1051"])[0])
    except MLError as exc:
        steps = [("Autenticação (App ID + Secret Key)", False, str(exc))]
    request.session["promo_test"] = [{"step": s, "ok": ok, "detail": d[:300]} for s, ok, d in steps]
    return redirect("/admin/promo")


@router.post("/promo/run")
def promo_run(request: Request, _csrf: None = Depends(verify_csrf),
              admin: AdminUser = Depends(require_superuser)):
    result = promo_service.run_cycle(promo_client_factory, triggered_by=f"painel:{admin.username}")
    kind = "error" if result.errors and not result.eligible else ("warn" if result.errors else "ok")
    flash(request, "Busca concluída: " + result.summary(), kind)
    return redirect("/admin/promo/offers")


@router.get("/promo/offers", response_class=HTMLResponse)
def promo_offers(request: Request, status: str = "new", page: int = 1,
                 admin: AdminUser = Depends(current_admin), db: Session = Depends(get_db)):
    stmt = select(PromoOffer)
    if status in {s.value for s in OfferStatus}:
        stmt = stmt.where(PromoOffer.status == OfferStatus(status))
    else:
        status = "all"
    if status == "posted":
        stmt = stmt.order_by(PromoOffer.posted_at.desc().nulls_last())
    elif status == "queued":
        stmt = stmt.order_by(PromoOffer.queued_at)
    else:
        stmt = stmt.order_by(PromoOffer.discount_pct.desc().nulls_last(), PromoOffer.id.desc())
    offers, pager = paginate(db, stmt, page)
    return render(request, "admin/promo_offers.html", admin, offers=offers, pager=pager, status=status,
                  statuses=[s.value for s in OfferStatus], stats=promo_service.stats())


def _valid_affiliate(url: str) -> str | None:
    url = url.strip()
    if not url:
        return ""
    if not url.startswith("https://") or len(url) > 500 or any(c.isspace() for c in url):
        return None
    return url


@router.post("/promo/offers/{offer_id}/queue")
def promo_offer_queue(offer_id: int, request: Request, affiliate_url: str = Form("", max_length=500),
                      _csrf: None = Depends(verify_csrf), admin: AdminUser = Depends(current_admin)):
    url = _valid_affiliate(affiliate_url)
    if url is None:
        flash(request, "Link de afiliado inválido (precisa começar com https://).", "error")
    elif promo_service.queue_offer(offer_id, url, admin.id, client_ip(request)):
        flash(request, "Oferta enviada para a fila — o bot publica no canal em instantes.")
    else:
        flash(request, "Esta oferta já está na fila.", "warn")
    return redirect(request.headers.get("referer") or "/admin/promo/offers")


@router.post("/promo/offers/{offer_id}/affiliate")
def promo_offer_affiliate(offer_id: int, request: Request, affiliate_url: str = Form("", max_length=500),
                          _csrf: None = Depends(verify_csrf), admin: AdminUser = Depends(current_admin)):
    url = _valid_affiliate(affiliate_url)
    if url is None:
        flash(request, "Link de afiliado inválido (precisa começar com https://).", "error")
    else:
        promo_service.set_affiliate(offer_id, url)
        flash(request, "Link de afiliado salvo.")
    return redirect(request.headers.get("referer") or "/admin/promo/offers")


@router.post("/promo/offers/{offer_id}/discard")
def promo_offer_discard(offer_id: int, request: Request, _csrf: None = Depends(verify_csrf),
                        admin: AdminUser = Depends(current_admin)):
    promo_service.discard_offer(offer_id, admin.id, client_ip(request))
    return redirect(request.headers.get("referer") or "/admin/promo/offers")


@router.post("/promo/offers/{offer_id}/restore")
def promo_offer_restore(offer_id: int, request: Request, _csrf: None = Depends(verify_csrf),
                        admin: AdminUser = Depends(current_admin)):
    promo_service.restore_offer(offer_id)
    return redirect(request.headers.get("referer") or "/admin/promo/offers")


# --------------------------------------------------------------------------- #
# WhatsApp (Evolution API)
# --------------------------------------------------------------------------- #
wa_client_factory = whatsapp_service.default_client


@router.get("/whatsapp", response_class=HTMLResponse)
def whatsapp_home(request: Request, qr: int = 0, admin: AdminUser = Depends(current_admin),
                  db: Session = Depends(get_db)):
    values = settings_service.get_all(db)
    cfg = whatsapp_service.wa_config(values)
    state, number, qr_image, groups, error = "unknown", None, None, [], None
    try:
        with wa_client_factory() as client:
            state = client.state(cfg.instance)
            if state == "open":
                number = client.owner_number(cfg.instance)
                try:
                    groups = client.groups(cfg.instance)
                except WhatsAppError as exc:
                    error = f"Não foi possível listar os grupos: {exc}"
            elif qr and state in ("connecting", "close") and admin.is_superuser:
                qr_image = client.qr_code(cfg.instance)
    except WhatsAppError as exc:
        error = str(exc)
    selected = {g["id"] for g in cfg.groups}
    return render(request, "admin/whatsapp.html", admin, cfg=cfg, state=state, number=number,
                  qr_image=qr_image, groups=groups, selected=selected, error=error,
                  active_hours=values.get("promo_active_hours") or "",
                  limits=whatsapp_service.limits_status(cfg, settings_service.promo_config(values)), waiting_qr=bool(qr) and state != "open")


@router.post("/whatsapp/connect")
def whatsapp_connect(request: Request, _csrf: None = Depends(verify_csrf),
                     admin: AdminUser = Depends(require_superuser), db: Session = Depends(get_db)):
    cfg = whatsapp_service.wa_config(settings_service.get_all(db))
    try:
        with wa_client_factory() as client:
            if client.state(cfg.instance) == "missing":
                client.create(cfg.instance)
    except WhatsAppError as exc:
        flash(request, str(exc), "error")
        return redirect("/admin/whatsapp")
    audit(db, request, admin, "whatsapp_connect_started")
    db.commit()
    return redirect("/admin/whatsapp?qr=1")


@router.post("/whatsapp/logout")
def whatsapp_logout(request: Request, _csrf: None = Depends(verify_csrf),
                    admin: AdminUser = Depends(require_superuser), db: Session = Depends(get_db)):
    cfg = whatsapp_service.wa_config(settings_service.get_all(db))
    try:
        with wa_client_factory() as client:
            client.logout(cfg.instance)
        settings_service.set_values(db, {"wa_enabled": "0"}, admin.id)
        audit(db, request, admin, "whatsapp_logout")
        db.commit()
        flash(request, "Número desconectado e envios pausados.")
    except WhatsAppError as exc:
        flash(request, str(exc), "error")
    return redirect("/admin/whatsapp")


@router.post("/whatsapp/groups")
def whatsapp_groups(request: Request, group_ids: list[str] = Form(default=[]), _csrf: None = Depends(verify_csrf),
                    admin: AdminUser = Depends(require_superuser), db: Session = Depends(get_db)):
    cfg = whatsapp_service.wa_config(settings_service.get_all(db))
    ids = [g for g in dict.fromkeys(group_ids) if whatsapp_service.GROUP_JID_RE.match(g)][:20]
    names = {}
    try:
        with wa_client_factory() as client:
            names = {g["id"]: g["name"] for g in client.groups(cfg.instance)}
    except WhatsAppError:
        names = {g["id"]: g["name"] for g in cfg.groups}
    chosen = [{"id": i, "name": names.get(i, i)} for i in ids if i in names]
    settings_service.set_values(db, {"wa_groups": json.dumps(chosen, ensure_ascii=False)}, admin.id)
    audit(db, request, admin, "whatsapp_groups_saved", groups=len(chosen))
    db.commit()
    flash(request, f"{len(chosen)} grupo(s) selecionado(s) para receber as ofertas.")
    return redirect("/admin/whatsapp")


@router.post("/whatsapp/settings")
def whatsapp_settings(
    request: Request,
    wa_warmup_days: str = Form("7", max_length=3),
    wa_warmup_per_day: str = Form("5", max_length=3),
    wa_max_per_window: str = Form("5", max_length=3),
    wa_window_hours: str = Form("4", max_length=3),
    wa_max_per_day: str = Form("20", max_length=3),
    wa_group_delay_seconds: str = Form("10", max_length=4),
    wa_max_age_hours: str = Form("6", max_length=3),
    _csrf: None = Depends(verify_csrf),
    admin: AdminUser = Depends(require_superuser),
    db: Session = Depends(get_db),
):
    raw = {"wa_warmup_days": wa_warmup_days, "wa_warmup_per_day": wa_warmup_per_day,
           "wa_max_per_window": wa_max_per_window, "wa_window_hours": wa_window_hours,
           "wa_max_per_day": wa_max_per_day, "wa_group_delay_seconds": wa_group_delay_seconds,
           "wa_max_age_hours": wa_max_age_hours}
    cfg = whatsapp_service.wa_config(raw)  # aplica os limites mínimos/máximos de segurança
    settings_service.set_values(db, {
        "wa_warmup_days": str(cfg.warmup_days), "wa_warmup_per_day": str(cfg.warmup_per_day),
        "wa_max_per_window": str(cfg.max_per_window), "wa_window_hours": str(cfg.window_hours),
        "wa_max_per_day": str(cfg.max_per_day), "wa_group_delay_seconds": str(cfg.group_delay_seconds),
        "wa_max_age_hours": str(cfg.max_age_hours),
    }, admin.id)
    audit(db, request, admin, "whatsapp_limits_saved")
    db.commit()
    flash(request, "Ritmo de envio salvo.")
    return redirect("/admin/whatsapp")


@router.post("/whatsapp/warmup/reset")
def whatsapp_warmup_reset(request: Request, _csrf: None = Depends(verify_csrf),
                          admin: AdminUser = Depends(require_superuser), db: Session = Depends(get_db)):
    settings_service.set_values(db, {"wa_started_at": utcnow().isoformat()}, admin.id)
    audit(db, request, admin, "whatsapp_warmup_reset")
    db.commit()
    flash(request, "Aquecimento reiniciado a partir de hoje.")
    return redirect("/admin/whatsapp")


@router.post("/whatsapp/toggle")
def whatsapp_toggle(request: Request, _csrf: None = Depends(verify_csrf),
                    admin: AdminUser = Depends(require_superuser), db: Session = Depends(get_db)):
    cfg = whatsapp_service.wa_config(settings_service.get_all(db))
    enable = not cfg.enabled
    if enable and not cfg.groups:
        flash(request, "Selecione pelo menos um grupo antes de ativar os envios.", "error")
        return redirect("/admin/whatsapp")
    values = {"wa_enabled": "1" if enable else "0"}
    if enable and not cfg.started_at:
        values["wa_started_at"] = utcnow().isoformat()  # o aquecimento começa na 1ª ativação
    settings_service.set_values(db, values, admin.id)
    audit(db, request, admin, "whatsapp_toggled", enabled=enable)
    db.commit()
    flash(request, "Envios para o WhatsApp ativados." if enable else "Envios para o WhatsApp pausados.")
    return redirect("/admin/whatsapp")


@router.post("/whatsapp/test")
def whatsapp_test(request: Request, group_id: str = Form(...), _csrf: None = Depends(verify_csrf),
                  admin: AdminUser = Depends(require_superuser), db: Session = Depends(get_db)):
    cfg = whatsapp_service.wa_config(settings_service.get_all(db))
    group = next((g for g in cfg.groups if g["id"] == group_id), None)
    if group is None:
        flash(request, "Escolha um dos grupos selecionados.", "error")
        return redirect("/admin/whatsapp")
    try:
        with wa_client_factory() as client:
            client.send_text(cfg.instance, group["id"], "Teste de conexão do bot de ofertas: tudo certo por aqui.")
        flash(request, f"Mensagem de teste enviada para \"{group['name']}\".")
    except WhatsAppError as exc:
        flash(request, f"Falha no envio: {exc}", "error")
    return redirect("/admin/whatsapp")


@router.post("/promo/offers/{offer_id}/whatsapp")
def promo_offer_whatsapp(offer_id: int, request: Request, affiliate_url: str = Form("", max_length=500),
                         _csrf: None = Depends(verify_csrf),
                         admin: AdminUser = Depends(current_admin), db: Session = Depends(get_db)):
    offer = db.get(PromoOffer, offer_id, with_for_update=True)
    if offer is None:
        raise HTTPException(404)
    url = _valid_affiliate(affiliate_url)
    if url is None:
        flash(request, "Link de afiliado inválido (precisa começar com https://).", "error")
        return redirect(request.headers.get("referer") or "/admin/promo/offers")
    if url:
        offer.affiliate_url = url
    values = settings_service.get_all(db)
    if not whatsapp_service.wa_config(values).ready:
        flash(request, "Ative os envios e selecione grupos na página WhatsApp primeiro.", "error")
    else:
        offer.wa_status = WaStatus.NONE
        whatsapp_service.mark_pending(db, offer, values)
        audit(db, request, admin, "whatsapp_offer_queued", offer_id=offer.id)
        db.commit()
        flash(request, "Oferta na fila do WhatsApp. Ela sai respeitando os limites de envio.")
    return redirect(request.headers.get("referer") or "/admin/promo/offers")
