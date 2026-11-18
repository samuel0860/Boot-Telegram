"""Planos, categorias e produtos/conteúdos."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from admin.auth import (
    current_admin,
    require_superuser,
    verify_csrf,
)
from admin.common import SLUG_RE, audit, flash, parse_optional_int, parse_price, redirect, render, slugify
from config import settings
from database import get_db
from models import (
    AccessType,
    AdminUser,
    Category,
    ContentType,
    Order,
    OrderStatus,
    Plan,
    Product,
)

router = APIRouter()


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
