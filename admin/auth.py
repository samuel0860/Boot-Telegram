"""Autenticação do painel: hash Argon2id, sessão, CSRF e bloqueio por tentativas."""
from __future__ import annotations

import secrets
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Depends, Form, HTTPException, Request
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from config import settings
from core.audit import log_event, utcnow
from database import get_db
from models import AccessLog, AdminUser

_hasher = PasswordHasher()  # Argon2id com parâmetros recomendados
# Hash fixo usado quando o usuário não existe (tempo de resposta uniforme).
_DUMMY_HASH = _hasher.hash(secrets.token_hex(16))

MIN_PASSWORD_LENGTH = 10
# Limites de tentativas (janela = ADMIN_LOCKOUT_MINUTES):
# - por IP, qualquer usuário: barra força bruta vinda de um mesmo endereço;
# - por usuário + IP: o bloqueio vale só para o IP que errou, então um atacante
#   não consegue trancar o administrador legítimo errando a senha de propósito;
# - por conta, alto: só dispara em ataque distribuído (muitos IPs).
IP_MAX_FAILURES = 20
ACCOUNT_MAX_FAILURES = 50
FAILED_ACTIONS = ("admin_login_failed", "admin_2fa_failed")


class NotAuthenticated(Exception):
    pass


def hash_password(password: str) -> str:
    validate_password_strength(password)
    return _hasher.hash(password)


def validate_password_strength(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"A senha deve ter pelo menos {MIN_PASSWORD_LENGTH} caracteres.")
    if password.isdigit() or password.isalpha():
        raise ValueError("A senha deve misturar letras e números/símbolos.")


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _recent_failures(session: Session, ip: str | None, admin_id: int | None = None) -> int:
    if not ip:
        return 0
    since = utcnow() - timedelta(minutes=settings.admin_lockout_minutes)
    cond = [AccessLog.action.in_(FAILED_ACTIONS), AccessLog.ip_address == ip, AccessLog.created_at > since]
    if admin_id is not None:
        cond.append(AccessLog.admin_user_id == admin_id)
    return session.scalar(select(func.count(AccessLog.id)).where(and_(*cond))) or 0


def ip_blocked(session: Session, ip: str | None) -> bool:
    return _recent_failures(session, ip) >= IP_MAX_FAILURES


def register_failure(session: Session, admin: AdminUser | None, ip: str | None, action: str, **details) -> None:
    now = utcnow()
    if admin is not None:
        admin.failed_login_attempts += 1
        if admin.failed_login_attempts >= ACCOUNT_MAX_FAILURES:
            admin.locked_until = now + timedelta(minutes=settings.admin_lockout_minutes)
            admin.failed_login_attempts = 0
    log_event(session, action, admin_user_id=admin.id if admin else None, ip_address=ip, **details)


TOO_MANY = "Muitas tentativas. Aguarde alguns minutos e tente novamente."


def authenticate(session: Session, username: str, password: str, ip: str | None) -> tuple[AdminUser | None, str | None]:
    """Confere usuário e senha. Retorna (admin, erro).

    Se o admin tiver verificação em duas etapas, o login só termina em complete_login()
    depois do código do aplicativo autenticador.
    """
    username = username.strip().lower()
    if ip_blocked(session, ip):
        return None, TOO_MANY
    admin = session.scalar(
        select(AdminUser).where(func.lower(AdminUser.username) == username).with_for_update()
    )
    if admin is None:
        verify_password(_DUMMY_HASH, password)
        register_failure(session, None, ip, "admin_login_failed", username=username[:64])
        session.commit()
        return None, "Usuário ou senha inválidos."

    now = utcnow()
    if admin.locked_until and admin.locked_until > now:
        session.commit()
        return None, "Conta temporariamente bloqueada por excesso de tentativas. Tente mais tarde."
    if _recent_failures(session, ip, admin.id) >= settings.admin_max_failed_logins:
        session.commit()
        return None, "Conta temporariamente bloqueada por excesso de tentativas neste dispositivo. Tente mais tarde."

    if not admin.is_active or not verify_password(admin.password_hash, password):
        register_failure(session, admin, ip, "admin_login_failed")
        session.commit()
        return None, "Usuário ou senha inválidos."

    if _hasher.check_needs_rehash(admin.password_hash):
        admin.password_hash = _hasher.hash(password)
    if admin.totp_enabled:
        session.commit()
        return admin, None
    complete_login(session, admin, ip)
    return admin, None


def complete_login(session: Session, admin: AdminUser, ip: str | None) -> None:
    admin.failed_login_attempts = 0
    admin.locked_until = None
    admin.last_login_at = utcnow()
    log_event(session, "admin_login", admin_user_id=admin.id, ip_address=ip, two_factor=admin.totp_enabled)
    session.commit()


def login_session(request: Request, admin: AdminUser) -> None:
    request.session.clear()
    request.session["admin_id"] = admin.id
    request.session["csrf"] = secrets.token_urlsafe(32)


def get_csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def verify_csrf(request: Request, csrf_token: str = Form("")) -> None:
    expected = request.session.get("csrf")
    if not expected or not secrets.compare_digest(expected, csrf_token):
        raise HTTPException(status_code=403, detail="Token CSRF inválido. Recarregue a página.")


def current_admin(request: Request, db: Session = Depends(get_db)) -> AdminUser:
    admin_id = request.session.get("admin_id")
    if not admin_id:
        raise NotAuthenticated()
    admin = db.get(AdminUser, admin_id)
    if admin is None or not admin.is_active:
        request.session.clear()
        raise NotAuthenticated()
    return admin


def require_superuser(admin: AdminUser = Depends(current_admin)) -> AdminUser:
    if not admin.is_superuser:
        raise HTTPException(status_code=403, detail="Permissão insuficiente.")
    return admin
