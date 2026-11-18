"""Funil de vendas do bot: cupons, recuperação de PIX expirado, upsell, relatório e LGPD."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from core import retention_service
from database import session_scope
from models import AccessLog, Coupon, Order, OrderStatus, PaymentStatus, Subscription, User, WebhookEvent
from sales import payment_service, sales_service
from sales.access_service import utcnow
from sales.user_service import register_telegram_user


def _coupon(code="PROMO10", pct=10, **kw) -> int:
    with session_scope() as s:
        c = Coupon(code=code, percent_off=pct, **kw)
        s.add(c)
        s.flush()
        return c.id


def _pay(fake_client, result) -> None:
    pid = next(p for p, d in fake_client.payments.items() if d["external_reference"] == result.order_public_id)
    fake_client.set_status(pid, PaymentStatus.APPROVED)
    payment_service.reconcile_order(result.order_public_id, fake_client)


# --------------------------------------------------------------------------- #
# Cupons
# --------------------------------------------------------------------------- #
def test_coupon_discounts_checkout_and_payment_validates(catalog, user, fake_client):
    _coupon()
    r = payment_service.create_pix_checkout(user.telegram_id, catalog["monthly"], fake_client, "promo10")
    assert r.amount == Decimal("13.50") and r.list_price == Decimal("15.00") and r.coupon_code == "PROMO10"
    # mesma compra com o mesmo cupom reaproveita o PIX; sem cupom gera outro
    assert payment_service.create_pix_checkout(user.telegram_id, catalog["monthly"], fake_client, "PROMO10").reused
    assert not payment_service.create_pix_checkout(user.telegram_id, catalog["monthly"], fake_client).reused
    _pay(fake_client, r)
    with session_scope() as s:
        order = s.scalar(select(Order).where(Order.amount == Decimal("13.50")))
        assert order.status == OrderStatus.PAID and order.coupon_id is not None


@pytest.mark.parametrize("kw, msg", [
    ({"is_active": False}, "não está mais ativo"),
    ({"valid_until": utcnow() - timedelta(days=1)}, "expirou"),
])
def test_coupon_rejections(catalog, user, fake_client, kw, msg):
    _coupon(**kw)
    with pytest.raises(payment_service.CouponRejected, match=msg):
        payment_service.create_pix_checkout(user.telegram_id, catalog["monthly"], fake_client, "PROMO10")


def test_coupon_plan_restriction_and_max_uses(catalog, user, fake_client):
    _coupon(plan_id=catalog["pack"], max_uses=1)
    with pytest.raises(payment_service.CouponRejected, match="vale só para o plano Pack"):
        payment_service.create_pix_checkout(user.telegram_id, catalog["monthly"], fake_client, "PROMO10")
    r = payment_service.create_pix_checkout(user.telegram_id, catalog["pack"], fake_client, "PROMO10")
    assert r.amount == Decimal("2.25")
    _pay(fake_client, r)  # 1 uso pago -> esgotado
    with pytest.raises(payment_service.CouponRejected, match="número máximo"):
        payment_service.create_pix_checkout(user.telegram_id, catalog["pack"], fake_client, "PROMO10")


def test_coupon_helpers():
    assert sales_service.discounted(Decimal("9.90"), 90) == Decimal("0.99")
    assert sales_service.discounted(Decimal("0.01"), 50) == Decimal("0.01")   # nunca zera
    assert sales_service.split_start_param("tiktok__promo10") == ("tiktok", "PROMO10")
    assert sales_service.split_start_param("tiktok") == ("tiktok", None)
    assert sales_service.split_start_param(None) == (None, None)


# --------------------------------------------------------------------------- #
# Recuperação de PIX expirado
# --------------------------------------------------------------------------- #
def _expire(order_public_id: str, minutes_ago: int = 30) -> None:
    with session_scope() as s:
        o = s.scalar(select(Order).where(Order.public_id == order_public_id))
        o.status = OrderStatus.EXPIRED
        o.expires_at = utcnow() - timedelta(minutes=minutes_ago)


def test_recovery_sent_once(catalog, user, fake_client):
    r = payment_service.create_pix_checkout(user.telegram_id, catalog["monthly"], fake_client)
    _expire(r.order_public_id, minutes_ago=2)
    with session_scope() as s:
        assert sales_service.due_recoveries(s) == []          # espera RECOVERY_DELAY_MINUTES
    _expire(r.order_public_id, minutes_ago=30)
    with session_scope() as s:
        tasks = sales_service.due_recoveries(s)
    assert len(tasks) == 1 and tasks[0].telegram_id == user.telegram_id and tasks[0].plan_name == "Mensal"
    with session_scope() as s:
        assert sales_service.due_recoveries(s) == []          # nunca repete


def test_no_recovery_if_user_bought_or_blocked(catalog, user, fake_client):
    r1 = payment_service.create_pix_checkout(user.telegram_id, catalog["monthly"], fake_client)
    _expire(r1.order_public_id)
    r2 = payment_service.create_pix_checkout(user.telegram_id, catalog["monthly"], fake_client)
    _pay(fake_client, r2)
    with session_scope() as s:
        assert sales_service.due_recoveries(s) == []

    other = register_telegram_user(telegram_id=777, username=None, first_name="B", last_name=None,
                                   language_code=None, source="tiktok")
    r3 = payment_service.create_pix_checkout(other.telegram_id, catalog["pack"], fake_client)
    _expire(r3.order_public_id)
    with session_scope() as s:
        s.get(User, other.id).is_blocked = True
    with session_scope() as s:
        assert sales_service.due_recoveries(s) == []


# --------------------------------------------------------------------------- #
# Upsell
# --------------------------------------------------------------------------- #
def test_upsell_after_pack_offers_subscription_once(catalog, user, fake_client):
    r = payment_service.create_pix_checkout(user.telegram_id, catalog["pack"], fake_client)
    _pay(fake_client, r)
    with session_scope() as s:
        order_id = s.scalar(select(Order.id).where(Order.status == OrderStatus.PAID))
    with session_scope() as s:
        task = sales_service.upsell_for_order(s, order_id)
    assert task and task.plan_name == "Mensal" and task.price == Decimal("15.00")
    with session_scope() as s:
        assert sales_service.upsell_for_order(s, order_id) is None


def test_no_upsell_when_user_already_has_bigger_plan(catalog, user, fake_client):
    _pay(fake_client, payment_service.create_pix_checkout(user.telegram_id, catalog["monthly"], fake_client))
    r = payment_service.create_pix_checkout(user.telegram_id, catalog["pack"], fake_client)
    _pay(fake_client, r)
    with session_scope() as s:
        pack_order = s.scalar(select(Order.id).where(Order.amount == Decimal("2.50")))
    with session_scope() as s:
        assert sales_service.upsell_for_order(s, pack_order) is None


# --------------------------------------------------------------------------- #
# Funil por origem
# --------------------------------------------------------------------------- #
def test_funnel_by_source(catalog, user, fake_client):
    for tid, src in [(1001, "tiktok"), (1002, "insta"), (1003, None)]:
        register_telegram_user(telegram_id=tid, username=None, first_name=None, last_name=None,
                               language_code=None, source=src)
    payment_service.create_pix_checkout(1001, catalog["pack"], fake_client)          # gerou PIX, não pagou
    _pay(fake_client, payment_service.create_pix_checkout(user.telegram_id, catalog["monthly"], fake_client))
    with session_scope() as s:
        rows = {r.source: r for r in sales_service.funnel_by_source(s, None)}
    assert rows["tiktok"].users == 2 and rows["tiktok"].checkout_users == 2 and rows["tiktok"].buyers == 1
    assert rows["tiktok"].revenue == Decimal("15.00") and rows["tiktok"].conversion == 50.0
    assert rows["insta"].users == 1 and rows["insta"].buyers == 0
    assert rows["(direto)"].users == 1


# --------------------------------------------------------------------------- #
# LGPD
# --------------------------------------------------------------------------- #
def test_retention_purges_old_records(user):
    with session_scope() as s:
        s.add(AccessLog(action="velho", created_at=utcnow() - timedelta(days=400)))
        s.add(AccessLog(action="novo"))
        s.add(WebhookEvent(provider="mercadopago", event_key="old", created_at=utcnow() - timedelta(days=200)))
    result = retention_service.purge_old_records()
    assert result.logs == 1 and result.webhooks == 1
    with session_scope() as s:
        actions = set(s.scalars(select(AccessLog.action)).all())
    assert "velho" not in actions and "novo" in actions and "retention_purge" in actions


def test_anonymize_user_keeps_orders(catalog, user, fake_client):
    _pay(fake_client, payment_service.create_pix_checkout(user.telegram_id, catalog["monthly"], fake_client))
    assert retention_service.anonymize_user(user.id, None, None)
    with session_scope() as s:
        u = s.get(User, user.id)
        assert u.first_name is None and u.username is None
        assert s.scalar(select(func.count(Order.id))) == 1
        assert s.scalar(select(func.count(Subscription.id))) == 1
        assert s.scalar(select(func.count(AccessLog.id)).where(AccessLog.user_id == user.id,
                                                                AccessLog.action != "user_anonymized")) == 0
