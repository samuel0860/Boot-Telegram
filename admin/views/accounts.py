"""Administradores, minha conta, senha, 2FA, Telegram e alertas."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from admin import totp
from admin.auth import (
    current_admin,
    hash_password,
    require_superuser,
    verify_csrf,
    verify_password,
)
from admin.common import audit, flash, redirect, render
from config import settings
from core import alert_service, settings_service
from database import get_db
from models import (
    AdminUser,
)

router = APIRouter()


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
                  summary_hour=values.get("daily_summary_hour") or "21",
                  totp_setup=_totp_setup_context(request, admin))


def _totp_setup_context(request: Request, admin: AdminUser) -> dict | None:
    secret = request.session.get("totp_setup")
    if not secret or admin.totp_enabled:
        return None
    uri = totp.provisioning_uri(secret, admin.username)
    return {"qr": totp.qr_svg(uri), "secret": totp.format_secret(secret)}


@router.post("/account/2fa/start")
def account_2fa_start(request: Request, _csrf: None = Depends(verify_csrf),
                      admin: AdminUser = Depends(current_admin)):
    if not admin.totp_enabled:
        request.session["totp_setup"] = totp.new_secret()
    return redirect("/admin/account#2fa")


@router.post("/account/2fa/confirm")
def account_2fa_confirm(request: Request, code: str = Form(..., max_length=12), _csrf: None = Depends(verify_csrf),
                        admin: AdminUser = Depends(current_admin), db: Session = Depends(get_db)):
    secret = request.session.get("totp_setup")
    if not secret or admin.totp_enabled:
        return redirect("/admin/account#2fa")
    step = totp.verify(secret, code)
    if step is None:
        flash(request, "Código inválido. Confira se o horário do celular está automático e tente de novo.", "error")
        return redirect("/admin/account#2fa")
    admin.totp_secret = totp.store(secret)
    admin.totp_enabled = True
    admin.totp_last_step = step
    request.session.pop("totp_setup", None)
    audit(db, request, admin, "admin_2fa_enabled")
    db.commit()
    flash(request, "Verificação em duas etapas ativada. Nos próximos logins o painel vai pedir o código do aplicativo.")
    return redirect("/admin/account#2fa")


@router.post("/account/2fa/disable")
def account_2fa_disable(request: Request, password: str = Form(..., max_length=256),
                        code: str = Form(..., max_length=12), _csrf: None = Depends(verify_csrf),
                        admin: AdminUser = Depends(current_admin), db: Session = Depends(get_db)):
    if not admin.totp_enabled:
        return redirect("/admin/account#2fa")
    step = totp.verify(totp.load(admin.totp_secret), code, last_step=admin.totp_last_step)
    if not verify_password(admin.password_hash, password) or step is None:
        flash(request, "Senha ou código incorretos.", "error")
        return redirect("/admin/account#2fa")
    admin.totp_enabled = False
    admin.totp_secret = None
    admin.totp_last_step = None
    audit(db, request, admin, "admin_2fa_disabled")
    db.commit()
    flash(request, "Verificação em duas etapas desativada.", "warn")
    return redirect("/admin/account#2fa")


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
