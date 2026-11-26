from __future__ import annotations

import hashlib
import hmac
import re
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from database import session_scope
from models import AdminUser, OrderStatus, PaymentStatus, Plan, Subscription, WebhookEvent
from sales import payment_service
from sales.payments import get_payment_client, verify_mercadopago_signature

SECRET = "test-webhook-secret"


@pytest.fixture
def client(fake_client):
    from app import app

    app.dependency_overrides[get_payment_client] = lambda: fake_client
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _sign(data_id: str, request_id: str, ts: str = "1704908010") -> str:
    manifest = f"id:{data_id};request-id:{request_id};ts:{ts};"
    return f"ts={ts},v1=" + hmac.new(SECRET.encode(), manifest.encode(), hashlib.sha256).hexdigest()


def test_signature_helper():
    sig = _sign("123", "req-1")
    assert verify_mercadopago_signature(x_signature=sig, x_request_id="req-1", data_id="123", secret=SECRET)
    assert not verify_mercadopago_signature(x_signature=sig, x_request_id="req-2", data_id="123", secret=SECRET)
    assert not verify_mercadopago_signature(x_signature=None, x_request_id="req-1", data_id="123", secret=SECRET)


def test_home_opens_panel_and_keeps_campaign_links(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/admin/"
    r = client.get("/?start=tiktok-v12", follow_redirects=False)
    assert r.headers["location"] == "https://t.me/TesteVendasBot?start=tiktok-v12"
    assert client.get("/?start=<script>", follow_redirects=False).headers["location"] == "/admin/"

def test_privacy_page(client):
    r = client.get("/privacidade")
    assert r.status_code == 200 and "Lei Geral de Proteção de Dados" in r.text


def test_health(client):
    assert client.get("/health").json()["database"] is True


def test_webhook_flow(client, catalog, user, fake_client):
    payment_service.create_pix_checkout(user.telegram_id, catalog["monthly"], fake_client)
    pid = next(iter(fake_client.payments))
    fake_client.set_status(pid, PaymentStatus.APPROVED)
    url = f"/webhooks/mercadopago?data.id={pid}&type=payment"
    body = {"action": "payment.updated", "type": "payment", "data": {"id": pid}}

    # assinatura inválida -> recusado e nada liberado
    bad = client.post(url, json=body, headers={"x-signature": "ts=1,v1=deadbeef", "x-request-id": "r-bad"})
    assert bad.status_code == 401
    with session_scope() as s:
        assert s.scalar(select(func.count(Subscription.id))) == 0

    ok = client.post(url, json=body, headers={"x-signature": _sign(pid, "r-1"), "x-request-id": "r-1"})
    assert ok.status_code == 200 and ok.text == "granted"

    dup = client.post(url, json=body, headers={"x-signature": _sign(pid, "r-1"), "x-request-id": "r-1"})
    assert dup.status_code == 200 and dup.text == "duplicate"

    again = client.post(url, json=body, headers={"x-signature": _sign(pid, "r-2"), "x-request-id": "r-2"})
    assert again.text == "already_processed"

    with session_scope() as s:
        assert s.scalar(select(func.count(Subscription.id))) == 1
        # a requisição com assinatura inválida não é gravada
        assert s.scalar(select(func.count(WebhookEvent.id))) == 2
        from models import Order
        assert s.scalar(select(Order.status)) == OrderStatus.PAID


def test_webhook_unknown_payment_is_ignored(client):
    url = "/webhooks/mercadopago?data.id=987654&type=payment"
    r = client.post(url, json={}, headers={"x-signature": _sign("987654", "r-x"), "x-request-id": "r-x"})
    assert r.status_code == 200 and r.text == "not found"


def _csrf(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def _create_admin(superuser=True):
    from admin.auth import hash_password

    with session_scope() as s:
        s.add(AdminUser(username="admin", password_hash=hash_password("SenhaForte123"), is_superuser=superuser))


def _login(client, password="SenhaForte123"):
    token = _csrf(client.get("/admin/login").text)
    return client.post("/admin/login", data={"username": "admin", "password": password, "csrf_token": token},
                       follow_redirects=False)


def test_admin_requires_login(client):
    r = client.get("/admin/users", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"


def test_admin_login_and_edit_price(client, catalog):
    _create_admin()
    assert "inválidos" in _login(client, "errada").text
    r = _login(client)
    assert r.status_code == 303
    page = client.get("/admin/")
    assert page.status_code == 200 and "Dashboard" in page.text

    for path in ["/admin/users", "/admin/orders", "/admin/payments", "/admin/subscriptions?status=expired",
                 "/admin/plans", "/admin/products", "/admin/categories", "/admin/logs", "/admin/webhooks",
                 "/admin/admins", "/admin/account", f"/admin/plans/{catalog['monthly']}/edit", "/admin/products/new"]:
        assert client.get(path).status_code == 200, path

    # sem CSRF -> 403
    r = client.post(f"/admin/plans/{catalog['monthly']}/toggle")
    assert r.status_code == 403

    token = _csrf(client.get(f"/admin/plans/{catalog['monthly']}/edit").text)
    r = client.post("/admin/plans/save", data={
        "csrf_token": token, "plan_id": str(catalog["monthly"]), "name": "Mensal", "slug": "mensal",
        "description": "", "price": "19,90", "access_type": "subscription", "duration_days": "30",
        "sort_order": "1", "is_active": "1", "product_ids": [],
    }, follow_redirects=False)
    assert r.status_code == 303
    with session_scope() as s:
        assert s.get(Plan, catalog["monthly"]).price == Decimal("19.90")


def test_operator_cannot_edit_plans(client, catalog):
    _create_admin(superuser=False)
    _login(client)
    assert client.get("/admin/plans").status_code == 200
    assert client.get("/admin/plans/new").status_code == 403


def test_account_lockout(client):
    _create_admin()
    for _ in range(5):
        _login(client, "errada")
    assert "bloqueada" in _login(client).text
