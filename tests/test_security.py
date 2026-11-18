"""Login do painel: limites por IP/usuário, verificação em duas etapas (TOTP) e webhook."""
from __future__ import annotations

import re
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from admin import totp
from admin.auth import ACCOUNT_MAX_FAILURES, IP_MAX_FAILURES, hash_password
from database import session_scope
from models import AccessLog, AdminUser, WebhookEvent
from sales.payments import get_payment_client


@pytest.fixture
def client(fake_client):
    from app import app

    app.dependency_overrides[get_payment_client] = lambda: fake_client
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _csrf(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def _admin(username="admin", secret: str | None = None) -> int:
    with session_scope() as s:
        a = AdminUser(username=username, password_hash=hash_password("SenhaForte123"), is_superuser=True)
        if secret:
            a.totp_secret = totp.store(secret)
            a.totp_enabled = True
        s.add(a)
        s.flush()
        return a.id


def _login(c, username="admin", password="SenhaForte123", follow=False):
    token = _csrf(c.get("/admin/login").text)
    return c.post("/admin/login", data={"username": username, "password": password, "csrf_token": token},
                  follow_redirects=follow)


# --------------------------------------------------------------------------- #
# TOTP
# --------------------------------------------------------------------------- #
def test_totp_rfc6238_vector():
    # Vetor do RFC 6238 (SHA1, segredo "12345678901234567890"), T=59 s -> 94287082 (8 dígitos) -> 287082
    import base64
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert totp._code(secret, 59 // 30) == "287082"


def test_totp_verify_window_and_replay():
    secret = totp.new_secret()
    now = 1_700_000_000
    code = totp._code(secret, totp.current_step(now))
    step = totp.verify(secret, code, now=now)
    assert step == totp.current_step(now)
    assert totp.verify(secret, code, last_step=step, now=now) is None      # não reaproveita o mesmo código
    assert totp.verify(secret, code, now=now + 30) is not None             # tolera 30 s de relógio
    assert totp.verify(secret, code, now=now + 120) is None
    assert totp.verify(secret, "12345", now=now) is None


def test_qr_is_inline_svg():
    svg = totp.qr_svg(totp.provisioning_uri(totp.new_secret(), "admin"))
    assert svg.startswith("<svg") and "http" not in svg.split(">", 1)[1][:200]


# --------------------------------------------------------------------------- #
# Login em duas etapas
# --------------------------------------------------------------------------- #
def test_login_with_2fa(client):
    secret = totp.new_secret()
    _admin(secret=secret)
    r = _login(client)
    assert r.status_code == 303 and r.headers["location"] == "/admin/login/2fa"
    # ainda não está logado
    assert client.get("/admin/users", follow_redirects=False).headers["location"] == "/admin/login"

    page = client.get("/admin/login/2fa").text
    bad = client.post("/admin/login/2fa", data={"code": "000000", "csrf_token": _csrf(page)})
    assert "Código inválido" in bad.text

    code = totp._code(secret, totp.current_step())
    ok = client.post("/admin/login/2fa", data={"code": code, "csrf_token": _csrf(bad.text)}, follow_redirects=False)
    assert ok.status_code == 303 and ok.headers["location"] == "/admin/"
    assert client.get("/admin/users").status_code == 200
    with session_scope() as s:
        assert s.scalar(select(func.count(AccessLog.id)).where(AccessLog.action == "admin_2fa_failed")) == 1


def test_2fa_page_requires_password_step(client):
    assert client.get("/admin/login/2fa", follow_redirects=False).headers["location"] == "/admin/login"


def test_enable_and_disable_2fa_from_account(client):
    _admin()
    _login(client)
    page = client.get("/admin/account").text
    client.post("/admin/account/2fa/start", data={"csrf_token": _csrf(page)})
    page = client.get("/admin/account").text
    assert "<svg" in page and "Ativar" in page
    secret = re.search(r"Digite a chave: <code>([A-Z2-7 ]+)</code>", page).group(1).replace(" ", "")

    r = client.post("/admin/account/2fa/confirm", data={"code": "111111", "csrf_token": _csrf(page)})
    assert "Código inválido" in r.text
    code = totp._code(secret, totp.current_step())
    r = client.post("/admin/account/2fa/confirm", data={"code": code, "csrf_token": _csrf(r.text)})
    assert "Ativada" in r.text
    with session_scope() as s:
        a = s.scalar(select(AdminUser))
        assert a.totp_enabled and a.totp_secret and secret not in a.totp_secret   # guardado criptografado

    # desativar exige senha + código novo (o já usado não vale)
    r = client.post("/admin/account/2fa/disable", data={"password": "SenhaForte123", "code": code,
                                                        "csrf_token": _csrf(r.text)})
    assert "incorretos" in r.text
    next_code = totp._code(secret, totp.current_step() + 1)
    r = client.post("/admin/account/2fa/disable", data={"password": "SenhaForte123", "code": next_code,
                                                        "csrf_token": _csrf(r.text)})
    with session_scope() as s:
        assert not s.scalar(select(AdminUser)).totp_enabled


# --------------------------------------------------------------------------- #
# Limites de tentativas
# --------------------------------------------------------------------------- #
def test_wrong_password_from_one_ip_does_not_lock_account_for_others(client):
    _admin()
    for _ in range(5):
        _login(client, password="errada")
    assert "bloqueada" in _login(client).text            # este IP está bloqueado para este usuário
    with session_scope() as s:
        admin = s.scalar(select(AdminUser))
        assert admin.locked_until is None                 # a conta em si continua liberada
        # simula as falhas vindo de outro IP: o administrador legítimo entra normalmente
        s.execute(AccessLog.__table__.update().values(ip_address="203.0.113.9"))
    assert _login(client).headers["location"] == "/admin/"


def test_ip_limit_across_usernames(client):
    _admin()
    with session_scope() as s:
        for i in range(IP_MAX_FAILURES):
            s.add(AccessLog(action="admin_login_failed", ip_address="testclient", details={"username": f"u{i}"}))
    assert "Muitas tentativas" in _login(client).text


def test_distributed_attack_locks_account():
    from admin.auth import authenticate

    _admin()
    with session_scope() as s:
        for i in range(ACCOUNT_MAX_FAILURES):
            authenticate(s, "admin", "errada", f"198.51.100.{i % 250}")
    with session_scope() as s:
        assert s.scalar(select(AdminUser)).locked_until is not None


# --------------------------------------------------------------------------- #
# Webhook: requisição sem assinatura válida não grava nada
# --------------------------------------------------------------------------- #
def test_invalid_webhook_is_not_stored(client):
    for i in range(5):
        r = client.post(f"/webhooks/mercadopago?data.id={i}&type=payment", json={},
                        headers={"x-signature": f"ts={int(time.time())},v1=00", "x-request-id": f"spam-{i}"})
        assert r.status_code == 401
    with session_scope() as s:
        assert s.scalar(select(func.count(WebhookEvent.id))) == 0
