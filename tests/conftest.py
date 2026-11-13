"""Fixtures de teste. Usa um banco PostgreSQL separado: <DATABASE_URL>_test.

O cliente de pagamento falso existe SOMENTE nos testes; o código de produção
sempre consulta a API real do Mercado Pago.
"""
from __future__ import annotations

import itertools
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env", override=False)

_base_url = os.environ["DATABASE_URL"]
_parts = urlsplit(_base_url)
TEST_DB = _parts.path.lstrip("/") + "_test"
TEST_URL = os.environ.get("TEST_DATABASE_URL") or urlunsplit(_parts._replace(path="/" + TEST_DB))

_admin = create_engine(_base_url, isolation_level="AUTOCOMMIT")
with _admin.connect() as conn:
    if not conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": TEST_DB}):
        conn.execute(text(f'CREATE DATABASE "{TEST_DB}"'))
_admin.dispose()

os.environ.update(
    DATABASE_URL=TEST_URL,
    APP_ENV="development",
    SECRET_KEY="t" * 64,
    MP_ACCESS_TOKEN="TEST-TOKEN",
    MP_WEBHOOK_SECRET="test-webhook-secret",
    TELEGRAM_BOT_USERNAME="TesteVendasBot",
    TELEGRAM_START_PARAM="tiktok",
    PUBLIC_BASE_URL="https://exemplo.com.br",
)

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

from models import PaymentStatus  # noqa: E402
from payments import PaymentNotFound, PixCharge, ProviderPayment  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def migrated_db():
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield


@pytest.fixture(autouse=True)
def clean_db(migrated_db):
    from database import engine

    with engine.begin() as conn:
        tables = conn.scalars(
            text("SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename <> 'alembic_version'")
        ).all()
        conn.execute(text("TRUNCATE " + ", ".join(f'"{t}"' for t in tables) + " RESTART IDENTITY CASCADE"))
    yield


class FakePaymentClient:
    """Simula o provedor apenas para testes automatizados."""

    provider_name = "mercadopago"

    def __init__(self):
        self._ids = itertools.count(1000)
        self.payments: dict[str, dict] = {}
        self.configured = True

    def create_pix_payment(self, *, idempotency_key, amount, description, external_reference, payer_email,
                           payer_first_name, expires_at, notification_url) -> PixCharge:
        pid = str(next(self._ids))
        self.payments[pid] = {"status": PaymentStatus.PENDING, "amount": amount, "currency": "BRL",
                              "external_reference": external_reference, "method": "pix"}
        return PixCharge(provider="mercadopago", provider_payment_id=pid, status=PaymentStatus.PENDING,
                         status_detail="pending_waiting_transfer", amount=amount, currency="BRL",
                         qr_code=f"00020126PIXCODE{pid}", qr_code_base64="iVBORw0KGgo=", ticket_url=None,
                         expires_at=expires_at, raw={"id": pid})

    def set_status(self, pid: str, status: PaymentStatus, amount: Decimal | None = None):
        self.payments[pid]["status"] = status
        if amount is not None:
            self.payments[pid]["amount"] = amount

    def get_payment(self, payment_id: str) -> ProviderPayment:
        p = self.payments.get(str(payment_id))
        if p is None:
            raise PaymentNotFound(payment_id)
        return ProviderPayment(
            provider="mercadopago", provider_payment_id=str(payment_id), status=p["status"],
            status_raw=p["status"].value, status_detail=None, amount=p["amount"], currency=p["currency"],
            external_reference=p["external_reference"], payment_method_id=p["method"],
            approved_at=datetime.now(timezone.utc) if p["status"] == PaymentStatus.APPROVED else None,
            raw={"id": payment_id, "status": p["status"].value},
        )


@pytest.fixture
def fake_client():
    return FakePaymentClient()


@pytest.fixture
def catalog():
    """Cria um plano mensal com um conteúdo de texto e um grupo do Telegram."""
    from database import session_scope
    from models import AccessType, Category, ContentType, Plan, Product

    with session_scope() as s:
        cat = Category(name="VIP", slug="vip")
        text_prod = Product(name="Texto", slug="texto", category=cat, content_type=ContentType.TEXT,
                            content_value="segredo")
        chat_prod = Product(name="Grupo", slug="grupo", category=cat, content_type=ContentType.TELEGRAM_CHAT,
                            content_value="-1001234567890")
        monthly = Plan(name="Mensal", slug="mensal", price=Decimal("15.00"), access_type=AccessType.SUBSCRIPTION,
                       duration_days=30, products=[text_prod, chat_prod])
        pack = Plan(name="Pack", slug="pack", price=Decimal("2.50"), access_type=AccessType.ONE_TIME,
                    duration_days=None, products=[text_prod])
        s.add_all([monthly, pack])
        s.flush()
        return {"monthly": monthly.id, "pack": pack.id, "chat_id": -1001234567890}


@pytest.fixture
def user():
    from services.user_service import register_telegram_user

    return register_telegram_user(telegram_id=555000111, username="cliente", first_name="Ana",
                                  last_name=None, language_code="pt-br", source="tiktok")
