from __future__ import annotations

import threading
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from database import session_scope
from models import (
    AccessLog,
    Order,
    OrderStatus,
    Payment,
    PaymentStatus,
    Subscription,
    SubscriptionStatus,
    User,
)
from sales import access_service, payment_service
from sales.access_service import utcnow
from sales.user_service import register_telegram_user


def _count(model, *where):
    with session_scope() as s:
        return s.scalar(select(func.count()).select_from(model).where(*where))


def test_user_registration_is_unique():
    a = register_telegram_user(telegram_id=1, username="x", first_name="X", last_name=None, language_code=None,
                               source="tiktok")
    b = register_telegram_user(telegram_id=1, username="y", first_name="Y", last_name=None, language_code=None,
                               source="outro")
    assert a.is_new and not b.is_new and a.id == b.id
    assert _count(User) == 1
    with session_scope() as s:
        u = s.get(User, a.id)
        assert u.username == "y" and u.source == "tiktok"  # origem do primeiro acesso é mantida


def test_checkout_creates_order_and_reuses_pending(catalog, user, fake_client):
    r1 = payment_service.create_pix_checkout(user.telegram_id, catalog["monthly"], fake_client)
    assert r1.amount == Decimal("15.00") and r1.qr_code and not r1.reused
    r2 = payment_service.create_pix_checkout(user.telegram_id, catalog["monthly"], fake_client)
    assert r2.reused and r2.order_public_id == r1.order_public_id
    assert _count(Order) == 1 and _count(Payment) == 1


def test_clicking_check_without_payment_does_not_grant(catalog, user, fake_client):
    r = payment_service.create_pix_checkout(user.telegram_id, catalog["monthly"], fake_client)
    status = payment_service.reconcile_order(r.order_public_id, fake_client, user.telegram_id)
    assert status == OrderStatus.PENDING
    assert _count(Subscription) == 0


def test_approved_payment_grants_once(catalog, user, fake_client):
    r = payment_service.create_pix_checkout(user.telegram_id, catalog["monthly"], fake_client)
    pid = next(iter(fake_client.payments))
    fake_client.set_status(pid, PaymentStatus.APPROVED)

    for _ in range(3):
        assert payment_service.reconcile_order(r.order_public_id, fake_client) == OrderStatus.PAID

    assert _count(Subscription) == 1
    assert _count(AccessLog, AccessLog.action == "payment_approved") == 1
    with session_scope() as s:
        sub = s.scalar(select(Subscription))
        assert sub.status == SubscriptionStatus.ACTIVE
        assert timedelta(days=29, hours=23) < sub.expires_at - sub.starts_at <= timedelta(days=30)


def test_other_user_cannot_check_order(catalog, user, fake_client):
    r = payment_service.create_pix_checkout(user.telegram_id, catalog["monthly"], fake_client)
    assert payment_service.reconcile_order(r.order_public_id, fake_client, telegram_id=999) is None


def test_amount_mismatch_blocks_release(catalog, user, fake_client):
    r = payment_service.create_pix_checkout(user.telegram_id, catalog["monthly"], fake_client)
    pid = next(iter(fake_client.payments))
    fake_client.set_status(pid, PaymentStatus.APPROVED, amount=Decimal("0.01"))
    assert payment_service.reconcile_order(r.order_public_id, fake_client) == OrderStatus.PENDING
    assert _count(Subscription) == 0
    with session_scope() as s:
        assert "valor" in s.scalar(select(Payment.validation_error))


def test_concurrent_processing_grants_only_once(catalog, user, fake_client):
    payment_service.create_pix_checkout(user.telegram_id, catalog["monthly"], fake_client)
    pid = next(iter(fake_client.payments))
    fake_client.set_status(pid, PaymentStatus.APPROVED)
    pp = fake_client.get_payment(pid)
    results, errors = [], []

    def worker():
        try:
            with session_scope() as s:
                results.append(payment_service.apply_provider_payment(s, pp))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert results.count(payment_service.ApplyResult.GRANTED) == 1
    assert _count(Subscription) == 1


def test_late_payment_on_cancelled_order_is_honored(catalog, user, fake_client):
    r = payment_service.create_pix_checkout(user.telegram_id, catalog["pack"], fake_client)
    assert payment_service.cancel_pending_order(user.telegram_id, r.order_public_id)
    pid = next(iter(fake_client.payments))
    fake_client.set_status(pid, PaymentStatus.APPROVED)
    assert payment_service.reconcile_order(r.order_public_id, fake_client) == OrderStatus.PAID
    with session_scope() as s:
        assert s.scalar(select(Subscription.expires_at)) is None  # pack vitalício


def test_refund_revokes_access(catalog, user, fake_client):
    r = payment_service.create_pix_checkout(user.telegram_id, catalog["monthly"], fake_client)
    pid = next(iter(fake_client.payments))
    fake_client.set_status(pid, PaymentStatus.APPROVED)
    payment_service.reconcile_order(r.order_public_id, fake_client)
    fake_client.set_status(pid, PaymentStatus.REFUNDED)
    assert payment_service.reconcile_order(r.order_public_id, fake_client) == OrderStatus.REFUNDED
    with session_scope() as s:
        assert s.scalar(select(Subscription.status)) == SubscriptionStatus.REVOKED
        tasks = access_service.pending_removals(s)
    assert tasks and tasks[0].chat_ids == [catalog["chat_id"]]


def test_renewal_stacks_and_expiration_flow(catalog, user, fake_client):
    for _ in range(2):
        r = payment_service.create_pix_checkout(user.telegram_id, catalog["monthly"], fake_client)
        pid = max(fake_client.payments, key=int)
        fake_client.set_status(pid, PaymentStatus.APPROVED)
        payment_service.reconcile_order(r.order_public_id, fake_client)

    with session_scope() as s:
        first, second = s.scalars(select(Subscription).order_by(Subscription.id)).all()
        assert second.starts_at == first.expires_at
        assert access_service.is_authorized_for_chat(s, user.telegram_id, catalog["chat_id"])
        # Simula o fim do primeiro período
        first.starts_at = utcnow() - timedelta(days=31)
        first.expires_at = utcnow() - timedelta(minutes=1)
        second.starts_at = utcnow() - timedelta(minutes=1)

    with session_scope() as s:
        expired = access_service.expire_due_subscriptions(s)
    assert len(expired) == 1
    with session_scope() as s:
        tasks = access_service.pending_removals(s)
        # Ainda tem o segundo período válido -> não remove do grupo
        assert tasks and tasks[0].chat_ids == []
        assert access_service.is_authorized_for_chat(s, user.telegram_id, catalog["chat_id"])


def test_unpaid_user_not_authorized_for_chat(catalog, user):
    with session_scope() as s:
        assert not access_service.is_authorized_for_chat(s, user.telegram_id, catalog["chat_id"])
        assert catalog["chat_id"] in access_service.managed_chat_ids(s)


def test_delivery_claim_is_exclusive(catalog, user, fake_client):
    r = payment_service.create_pix_checkout(user.telegram_id, catalog["monthly"], fake_client)
    fake_client.set_status(next(iter(fake_client.payments)), PaymentStatus.APPROVED)
    payment_service.reconcile_order(r.order_public_id, fake_client)
    with session_scope() as s:
        packages = access_service.claim_orders_for_delivery(s)
    assert len(packages) == 1 and {i.name for i in packages[0].items} == {"Texto", "Grupo"}
    with session_scope() as s:
        assert access_service.claim_orders_for_delivery(s) == []
        access_service.mark_delivered(s, packages[0].order_id, [])
    with session_scope() as s:
        assert s.get(Order, packages[0].order_id).delivered_at is not None


def test_blocked_user_cannot_buy(catalog, user, fake_client):
    with session_scope() as s:
        s.get(User, user.id).is_blocked = True
    with pytest.raises(payment_service.CheckoutError):
        payment_service.create_pix_checkout(user.telegram_id, catalog["monthly"], fake_client)


def test_money_is_numeric():
    from models import Order as O
    from models import Plan

    assert str(Plan.__table__.c.price.type) == "NUMERIC(12, 2)"
    assert str(O.__table__.c.amount.type) == "NUMERIC(12, 2)"
