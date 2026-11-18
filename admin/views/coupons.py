"""Cupons de desconto dos planos vendidos no bot."""
from __future__ import annotations

from datetime import datetime, time

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from admin.auth import current_admin, require_superuser, verify_csrf
from admin.common import TZ, audit, flash, redirect, render
from config import settings
from database import get_db
from models import AdminUser, Coupon, Order, OrderStatus, Plan
from sales import sales_service

router = APIRouter()


@router.get("/coupons", response_class=HTMLResponse)
def coupons_list(request: Request, admin: AdminUser = Depends(current_admin), db: Session = Depends(get_db)):
    coupons = db.scalars(select(Coupon).options(selectinload(Coupon.plan))
                         .order_by(Coupon.is_active.desc(), Coupon.id.desc())).all()
    uses = dict(db.execute(
        select(Order.coupon_id, func.count(Order.id))
        .where(Order.coupon_id.is_not(None), Order.status == OrderStatus.PAID)
        .group_by(Order.coupon_id)).all())
    revenue = dict(db.execute(
        select(Order.coupon_id, func.coalesce(func.sum(Order.amount), 0))
        .where(Order.coupon_id.is_not(None), Order.status == OrderStatus.PAID)
        .group_by(Order.coupon_id)).all())
    plans = db.scalars(select(Plan).order_by(Plan.sort_order, Plan.price)).all()
    return render(request, "admin/coupons.html", admin, coupons=coupons, uses=uses, revenue=revenue, plans=plans,
                  bot_username=settings.telegram_bot_username, start_param=settings.telegram_start_param or "bot")


@router.post("/coupons/create")
def coupon_create(
    request: Request,
    code: str = Form(..., max_length=32),
    percent_off: str = Form(..., max_length=3),
    plan_id: str = Form(""),
    max_uses: str = Form("", max_length=7),
    valid_until: str = Form("", max_length=10),
    _csrf: None = Depends(verify_csrf),
    admin: AdminUser = Depends(require_superuser),
    db: Session = Depends(get_db),
):
    code = sales_service.normalize_code(code)
    errors = []
    if not sales_service.CODE_RE.match(code):
        errors.append("Código inválido: use de 3 a 32 letras, números, - ou _ (sem espaços).")
    pct = int(percent_off) if percent_off.strip().isdigit() else 0
    if not 1 <= pct <= 90:
        errors.append("O desconto deve ser de 1% a 90%.")
    plan = db.get(Plan, int(plan_id)) if plan_id.isdigit() else None
    if plan_id and plan is None:
        errors.append("Plano inválido.")
    uses = int(max_uses) if max_uses.strip().isdigit() and int(max_uses) > 0 else None
    until = None
    if valid_until.strip():
        try:
            day = datetime.strptime(valid_until.strip(), "%Y-%m-%d").date()
            until = datetime.combine(day, time(23, 59, 59), tzinfo=TZ)
        except ValueError:
            errors.append("Data de validade inválida.")
    if sales_service.CODE_RE.match(code) and db.scalar(select(Coupon.id).where(Coupon.code == code)):
        errors.append(f"Já existe um cupom {code}.")
    if errors:
        for e in errors:
            flash(request, e, "error")
        return redirect("/admin/coupons")
    db.add(Coupon(code=code, percent_off=pct, plan_id=plan.id if plan else None, max_uses=uses, valid_until=until))
    audit(db, request, admin, "coupon_created", code=code, percent_off=pct, plan_id=plan.id if plan else None)
    db.commit()
    flash(request, f"Cupom {code} criado: {pct}% de desconto.")
    return redirect("/admin/coupons")


@router.post("/coupons/{coupon_id}/toggle")
def coupon_toggle(coupon_id: int, request: Request, _csrf: None = Depends(verify_csrf),
                  admin: AdminUser = Depends(require_superuser), db: Session = Depends(get_db)):
    coupon = db.get(Coupon, coupon_id)
    if coupon is None:
        raise HTTPException(404)
    coupon.is_active = not coupon.is_active
    audit(db, request, admin, "coupon_toggled", code=coupon.code, active=coupon.is_active)
    db.commit()
    flash(request, f"Cupom {coupon.code} {'ativado' if coupon.is_active else 'desativado'}.")
    return redirect("/admin/coupons")
