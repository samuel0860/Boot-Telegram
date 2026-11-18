"""Operação: aviso de erros no Telegram, healthcheck do bot e telas novas do painel."""
from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from admin.auth import hash_password
from core import alert_service, error_reporter, settings_service
from database import session_scope
from models import AdminUser, Coupon
from sales.payments import get_payment_client

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Aviso de erros
# --------------------------------------------------------------------------- #
def _raise_and_log(log: logging.Logger) -> None:
    try:
        raise ValueError("falhou <x>")
    except ValueError:
        log.exception("Erro no job de entregas")


def test_error_buffer_groups_and_cools_down():
    buf = error_reporter.ErrorBuffer("bot")
    log = logging.getLogger("services.teste")
    log.addHandler(buf)
    try:
        for _ in range(3):
            _raise_and_log(log)
        log.warning("só aviso")                          # abaixo de ERROR: ignorado
        messages = buf.drain()
        assert len(messages) == 1
        assert "(×3)" in messages[0] and "ValueError: falhou &lt;x&gt;" in messages[0]
        _raise_and_log(log)                              # mesmo erro dentro do cooldown: não reenvia
        assert buf.drain() == []
        log.error("outro problema")
        assert len(buf.drain()) == 1
    finally:
        log.removeHandler(buf)


def test_error_reporter_redacts_secrets():
    object.__setattr__(error_reporter.settings, "telegram_bot_token", "123:SECRET")
    try:
        assert "SECRET" not in error_reporter._redact("url https://api.telegram.org/bot123:SECRET/x")
    finally:
        object.__setattr__(error_reporter.settings, "telegram_bot_token", "")


def test_flush_http_sends_to_linked_admins():
    with session_scope() as s:
        settings_service.set_values(s, {"tg_admin_ids": json.dumps([42]), "alerts_enabled": "1"})
    buf = error_reporter.ErrorBuffer("painel")
    buf.emit(logging.LogRecord("app", logging.ERROR, __file__, 1, "quebrou", None, None))
    sent = []
    transport = httpx.MockTransport(lambda req: sent.append(json.loads(req.content)) or httpx.Response(200, json={}))
    old = error_reporter._buffer
    error_reporter._buffer = buf
    object.__setattr__(error_reporter.settings, "telegram_bot_token", "TOKEN")
    try:
        assert error_reporter.flush_http(httpx.Client(transport=transport)) == 1
    finally:
        error_reporter._buffer = old
        object.__setattr__(error_reporter.settings, "telegram_bot_token", "")
    assert sent and sent[0]["chat_id"] == 42 and "quebrou" in sent[0]["text"]


# --------------------------------------------------------------------------- #
# Healthcheck do bot
# --------------------------------------------------------------------------- #
def _bot_health(module: str = "sales") -> int:
    return subprocess.run([sys.executable, str(ROOT / "manage.py"), "bot-health", module], cwd=ROOT,
                          capture_output=True, text=True).returncode


def test_bot_health_command():
    assert _bot_health() != 0                     # nunca deu sinal de vida
    alert_service.heartbeat()
    assert _bot_health() == 0
    assert _bot_health("offers") != 0             # cada processo tem o próprio sinal de vida
    alert_service.heartbeat(alert_service.OFFERS_HEARTBEAT)
    assert _bot_health("offers") == 0


# --------------------------------------------------------------------------- #
# Telas novas do painel
# --------------------------------------------------------------------------- #
@pytest.fixture
def admin_client(fake_client):
    from app import app

    with session_scope() as s:
        s.add(AdminUser(username="admin", password_hash=hash_password("SenhaForte123"), is_superuser=True))
    app.dependency_overrides[get_payment_client] = lambda: fake_client
    with TestClient(app) as c:
        token = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/admin/login").text).group(1)
        c.post("/admin/login", data={"username": "admin", "password": "SenhaForte123", "csrf_token": token})
        yield c
    app.dependency_overrides.clear()


def _token(c, url):
    return re.search(r'name="csrf_token" value="([^"]+)"', c.get(url).text).group(1)


def test_coupons_page(admin_client, catalog):
    c = admin_client
    r = c.post("/admin/coupons/create", data={"csrf_token": _token(c, "/admin/coupons"), "code": "tiktok10",
                                              "percent_off": "10", "plan_id": str(catalog["monthly"]),
                                              "max_uses": "50", "valid_until": "2099-12-31"})
    assert "Cupom TIKTOK10 criado" in r.text and "Mensal" in r.text
    r = c.post("/admin/coupons/create", data={"csrf_token": _token(c, "/admin/coupons"), "code": "TIKTOK10",
                                              "percent_off": "95"})
    assert "1% a 90%" in r.text and "Já existe" in r.text
    with session_scope() as s:
        coupon_id = s.query(Coupon.id).scalar()
    c.post(f"/admin/coupons/{coupon_id}/toggle", data={"csrf_token": _token(c, "/admin/coupons")})
    with session_scope() as s:
        assert not s.get(Coupon, coupon_id).is_active


def test_funnel_page(admin_client, user):
    r = admin_client.get("/admin/funnel?period=7")
    assert r.status_code == 200 and "tiktok" in r.text and "Total" in r.text


def test_privacy_link_on_landing(admin_client):
    assert 'href="/privacidade"' in admin_client.get("/").text
