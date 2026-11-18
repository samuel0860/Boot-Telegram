"""Usuários, pedidos, pagamentos e assinaturas."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from admin.auth import (
    client_ip,
    current_admin,
    require_superuser,
    verify_csrf,
)
from admin.common import flash, paginate, redirect, render
from core import retention_service
from database import get_db
from models import (
    AccessLog,
    AdminUser,
    Order,
    OrderStatus,
    Payment,
    PaymentStatus,
    Subscription,
    SubscriptionStatus,
    User,
)
from sales.access_service import log_event, revoke_subscription, utcnow

router = APIRouter()


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


@router.post("/users/{user_id}/anonymize")
def user_anonymize(user_id: int, request: Request, _csrf: None = Depends(verify_csrf),
                   admin: AdminUser = Depends(require_superuser)):
    if not retention_service.anonymize_user(user_id, admin.id, client_ip(request)):
        raise HTTPException(404)
    flash(request, "Dados pessoais do cliente apagados. Pedidos e pagamentos foram mantidos.")
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
