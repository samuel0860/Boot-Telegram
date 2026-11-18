"""Login, verificação em duas etapas e logout."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from admin import totp
from admin.auth import (
    authenticate,
    client_ip,
    complete_login,
    get_csrf_token,
    ip_blocked,
    login_session,
    register_failure,
    verify_csrf,
)
from admin.common import redirect, render
from database import get_db
from models import (
    AdminUser,
)

router = APIRouter()


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
    if admin.totp_enabled:
        request.session.clear()
        request.session["pending_2fa"] = {"id": admin.id, "exp": int(time.time()) + PENDING_2FA_SECONDS}
        get_csrf_token(request)
        return redirect("/admin/login/2fa")
    login_session(request, admin)
    return redirect("/admin/")


PENDING_2FA_SECONDS = 300


def _pending_2fa_admin(request: Request, db: Session) -> AdminUser | None:
    data = request.session.get("pending_2fa") or {}
    if not data or data.get("exp", 0) < time.time():
        request.session.pop("pending_2fa", None)
        return None
    admin = db.get(AdminUser, data.get("id"))
    return admin if admin and admin.is_active and admin.totp_enabled else None


@router.get("/login/2fa", response_class=HTMLResponse)
def login_2fa_page(request: Request, db: Session = Depends(get_db)):
    if _pending_2fa_admin(request, db) is None:
        return redirect("/admin/login")
    return render(request, "admin/login.html", None, error=None, step="2fa")


@router.post("/login/2fa")
def login_2fa_submit(request: Request, code: str = Form(..., max_length=12), _csrf: None = Depends(verify_csrf),
                     db: Session = Depends(get_db)):
    admin = _pending_2fa_admin(request, db)
    if admin is None:
        return redirect("/admin/login")
    ip = client_ip(request)
    if ip_blocked(db, ip):
        request.session.pop("pending_2fa", None)
        return render(request, "admin/login.html", None, error="Muitas tentativas. Aguarde alguns minutos.")
    admin = db.get(AdminUser, admin.id, with_for_update=True)
    step = totp.verify(totp.load(admin.totp_secret), code, last_step=admin.totp_last_step)
    if step is None:
        register_failure(db, admin, ip, "admin_2fa_failed")
        db.commit()
        return render(request, "admin/login.html", None, error="Código inválido. Confira o aplicativo e tente de novo.",
                      step="2fa")
    admin.totp_last_step = step
    complete_login(db, admin, ip)
    login_session(request, admin)
    return redirect("/admin/")


@router.post("/logout")
def logout(request: Request, _csrf: None = Depends(verify_csrf)):
    request.session.clear()
    return redirect("/admin/login")
