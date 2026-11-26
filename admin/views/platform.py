"""PLATAFORMA: os clientes (contas) que assinam o sistema.

Só a equipe da plataforma (administradores da conta principal) vê esta área. Aqui dá para
criar uma conta à mão, mudar o plano, suspender/reativar e "entrar" na conta de um cliente
para dar suporte (o painel passa a mostrar os dados dele até você clicar em "Voltar").
"""
from __future__ import annotations

import secrets
import string

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from admin.auth import current_admin, hash_password, verify_csrf
from admin.common import audit, flash, redirect, render
from core import plans
from core.tenancy import ALL_ACCOUNTS
from database import get_db
from models import Account, AccountStatus, AdminUser, OfferBot

router = APIRouter()
EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


def require_platform(admin: AdminUser = Depends(current_admin), db: Session = Depends(get_db)) -> AdminUser:
    own = db.get(Account, admin.account_id)
    if own is None or not own.is_platform or not admin.is_superuser:
        raise HTTPException(status_code=403, detail="Área da plataforma.")
    return admin


def temp_password() -> str:
    """Senha provisória forte (letras, números e símbolo), fácil de digitar."""
    alphabet = string.ascii_letters + string.digits
    core = "".join(secrets.choice(alphabet) for _ in range(12))
    return f"{core[:6]}-{core[6:]}!"


def create_account(db: Session, name: str, email: str, plan: str, **extra) -> tuple[Account, str]:
    """Cria a conta do cliente + o administrador dele (login = e-mail). Devolve a senha provisória."""
    account = Account(name=name.strip()[:120], email=email, plan=plan if plan in plans.PLANS else plans.DEFAULT_PLAN,
                      **extra)
    db.add(account)
    db.flush()
    password = temp_password()
    db.add(AdminUser(account_id=account.id, username=email[:64], email=email, is_superuser=True,
                     password_hash=hash_password(password)))
    db.flush()
    return account, password


def _count(db: Session, model) -> dict[int, int]:
    rows = db.execute(select(model.account_id, func.count()).group_by(model.account_id)
                      .execution_options(**{ALL_ACCOUNTS: True})).all()
    return dict(rows)


@router.get("/platform", response_class=HTMLResponse)
def platform_home(request: Request, admin: AdminUser = Depends(require_platform), db: Session = Depends(get_db)):
    accounts = db.scalars(select(Account).order_by(Account.is_platform.desc(), Account.created_at.desc())).all()
    stats = {
        "total": sum(1 for a in accounts if not a.is_platform),
        "active": sum(1 for a in accounts if not a.is_platform and a.status == AccountStatus.ACTIVE),
        "mrr": sum(plans.spec(a.plan).price for a in accounts if not a.is_platform and a.status == AccountStatus.ACTIVE),
    }
    return render(request, "admin/platform.html", admin, accounts=accounts, plans=plans.PLANS, stats=stats,
                  bots=_count(db, OfferBot), admins=_count(db, AdminUser),
                  new_password=request.session.pop("new_account_password", None))


@router.post("/platform/accounts/create")
def platform_create(request: Request, name: str = Form(..., max_length=120), email: str = Form(..., max_length=255),
                    plan: str = Form(plans.DEFAULT_PLAN), _csrf: None = Depends(verify_csrf),
                    admin: AdminUser = Depends(require_platform), db: Session = Depends(get_db)):
    import re

    email = email.strip().lower()
    if not name.strip() or not re.match(EMAIL_RE, email):
        flash(request, "Informe o nome e um e-mail válido.", "error")
        return redirect("/admin/platform")
    try:
        account, password = create_account(db, name, email, plan)
    except IntegrityError:
        db.rollback()
        flash(request, "Já existe um acesso com esse e-mail.", "error")
        return redirect("/admin/platform")
    audit(db, request, admin, "platform_account_created", account_id_created=account.id, plan=account.plan)
    db.commit()
    request.session["new_account_password"] = {"email": email, "password": password, "name": account.name}
    flash(request, f"Conta “{account.name}” criada.")
    return redirect("/admin/platform")


def _account(db: Session, account_id: int) -> Account:
    account = db.get(Account, account_id, with_for_update=True)
    if account is None:
        raise HTTPException(404)
    return account


@router.post("/platform/accounts/{account_id}/plan")
def platform_plan(account_id: int, request: Request, plan: str = Form(...), _csrf: None = Depends(verify_csrf),
                  admin: AdminUser = Depends(require_platform), db: Session = Depends(get_db)):
    account = _account(db, account_id)
    if plan not in plans.PLANS:
        raise HTTPException(400)
    account.plan = plan
    audit(db, request, admin, "platform_account_plan", target_account=account.id, plan=plan)
    db.commit()
    flash(request, f"“{account.name}” agora está no plano {plans.spec(plan).name}.")
    return redirect("/admin/platform")


@router.post("/platform/accounts/{account_id}/status")
def platform_status(account_id: int, request: Request, status: str = Form(...), _csrf: None = Depends(verify_csrf),
                    admin: AdminUser = Depends(require_platform), db: Session = Depends(get_db)):
    account = _account(db, account_id)
    if account.is_platform or status not in {s.value for s in AccountStatus}:
        flash(request, "A conta principal não pode ser suspensa.", "error")
        return redirect("/admin/platform")
    account.status = AccountStatus(status)
    audit(db, request, admin, "platform_account_status", target_account=account.id, status=status)
    db.commit()
    flash(request, f"“{account.name}”: {'ativa' if account.is_active else 'suspensa — o login e os bots param'}.")
    return redirect("/admin/platform")


@router.post("/platform/accounts/{account_id}/enter")
def platform_enter(account_id: int, request: Request, _csrf: None = Depends(verify_csrf),
                   admin: AdminUser = Depends(require_platform), db: Session = Depends(get_db)):
    account = _account(db, account_id)
    request.session["account_id"] = account.id
    audit(db, request, admin, "platform_support_enter", target_account=account.id)
    db.commit()
    flash(request, f"Você está dentro da conta “{account.name}” (suporte). Tudo o que fizer vale para ela.", "warn")
    return redirect("/admin/")


@router.post("/platform/leave")
def platform_leave(request: Request, _csrf: None = Depends(verify_csrf), admin: AdminUser = Depends(current_admin)):
    request.session["account_id"] = admin.account_id
    return redirect("/admin/platform")
