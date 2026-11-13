"""Painel administrativo (HTML renderizado no servidor).

Permissões:
- Qualquer administrador ativo: consultar tudo, bloquear/desbloquear usuários
  e encerrar acessos.
- Superusuário: gerenciar planos, preços, categorias, conteúdos e administradores.
"""
from __future__ import annotations

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
    Order,
    OrderStatus,
    Payment,
    PaymentStatus,
    Plan,
    Product,
    Subscription,
    SubscriptionStatus,
    User,
    WebhookEvent,
    WebhookStatus,
)
from services.access_service import log_event, revoke_subscription, utcnow
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
    return render(request, "admin/dashboard.html", admin, stats=stats, recent=recent,
                  payments_configured=settings.payments_configured,
                  webhook_configured=bool(settings.mp_webhook_secret))


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
def account(request: Request, admin: AdminUser = Depends(current_admin)):
    return render(request, "admin/account.html", admin)
