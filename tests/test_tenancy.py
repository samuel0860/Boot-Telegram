"""Vários clientes: cada conta só vê e só altera os próprios dados."""
from __future__ import annotations

import re
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from admin.auth import hash_password
from core import settings_service, tenancy
from database import session_scope
from models import AccessType, Account, AccountStatus, AdminUser, Coupon, OfferBot, Plan, PromoOffer, User
from offers import collector
from sales import user_service
from tests.fakes import make_bot
from tests.test_offers_shopee import APP_ID, SECRET, FakeShopee

PASSWORD = "SenhaForte123"


def _tok(c, url):
    return re.search(r'name="csrf_token" value="([^"]+)"', c.get(url).text).group(1)


def _login(c: TestClient, username: str, password: str = PASSWORD):
    tok = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/admin/login").text).group(1)
    return c.post("/admin/login", data={"username": username, "password": password, "csrf_token": tok})


@pytest.fixture
def two_accounts():
    """Conta 1 (plataforma, com dados) e conta 2 (cliente, vazia)."""
    with session_scope() as s:
        s.add(AdminUser(account_id=1, username="dono", password_hash=hash_password(PASSWORD), is_superuser=True))
        s.add(Plan(name="Plano VIP", slug="vip", price=Decimal("19.90"), access_type=AccessType.ONE_TIME))
        s.add(Coupon(code="SEGREDO10", percent_off=10))
        settings_service.set_values(s, {"shopee_app_id": "APP-DA-CONTA-1"})
        acc = Account(name="Loja da Maria", email="maria@cliente.com", plan="pro")
        s.add(acc)
        s.flush()
        s.add(AdminUser(account_id=acc.id, username="maria@cliente.com", password_hash=hash_password(PASSWORD),
                        is_superuser=True))
        other = acc.id
    bot1 = make_bot("Bot Secreto da Conta 1", telegram=("@canal1",))
    return {"other": other, "bot1": bot1}


def test_client_sees_only_own_data(two_accounts):
    bot1, other = two_accounts["bot1"], two_accounts["other"]
    with TestClient(__import__("app").app) as c:
        _login(c, "maria@cliente.com")
        for url in ("/admin/offers/bots", "/admin/plans", "/admin/coupons", "/admin/offers", "/admin/",
                    "/admin/offers/stats", "/admin/logs"):
            page = c.get(url).text
            assert "Bot Secreto da Conta 1" not in page and "Plano VIP" not in page and "SEGREDO10" not in page, url
        assert "APP-DA-CONTA-1" not in c.get("/admin/offers/settings").text

        # tentar abrir / alterar o bot da outra conta pelo número: não existe para ela
        assert c.get(f"/admin/offers/bots/{bot1}").status_code == 404
        tok = _tok(c, "/admin/offers/bots")
        assert c.post(f"/admin/offers/bots/{bot1}/toggle", data={"csrf_token": tok}).status_code == 404
        assert c.post(f"/admin/offers/bots/{bot1}/delete", data={"csrf_token": tok,
                                                                 "confirm_name": "x"}).status_code == 404

        # o que a cliente cria fica na conta dela; as configurações também
        c.post("/admin/offers/bots/create", data={"csrf_token": tok, "name": "Bot da Maria"})
        c.post("/admin/offers/settings", data={"csrf_token": _tok(c, "/admin/offers/settings"), "store": "shopee",
                                               "shopee_app_id": "18300009999", "shopee_secret": "s3"})
        assert c.get("/admin/platform").status_code == 403          # cliente não vê a plataforma

    with tenancy.unscoped(), session_scope() as s:
        mine = s.scalar(select(OfferBot).where(OfferBot.name == "Bot da Maria"))
        assert mine.account_id == other
        assert s.get(OfferBot, bot1).is_active                        # o bot da conta 1 não foi mexido
        assert settings_service.get_all(s, 1)["shopee_app_id"] == "APP-DA-CONTA-1"
        assert settings_service.get_all(s, other)["shopee_app_id"] == "18300009999"


def test_plan_limits(two_accounts):
    with session_scope() as s:
        s.get(Account, two_accounts["other"]).plan = "start"     # 1 bot, sem WhatsApp
    with TestClient(__import__("app").app) as c:
        _login(c, "maria@cliente.com")
        tok = _tok(c, "/admin/offers/bots")
        c.post("/admin/offers/bots/create", data={"csrf_token": tok, "name": "Primeiro"})
        r = c.post("/admin/offers/bots/create", data={"csrf_token": tok, "name": "Segundo"})
        assert "permite até 1 bot" in r.text
    with tenancy.unscoped(), session_scope() as s:
        assert s.scalar(select(OfferBot).where(OfferBot.name == "Segundo")) is None


def test_platform_creates_suspends_and_supports(two_accounts):
    other = two_accounts["other"]
    with TestClient(__import__("app").app) as c:
        _login(c, "dono")
        page = c.get("/admin/platform").text
        assert "Loja da Maria" in page and "Clientes da plataforma" in page
        r = c.post("/admin/platform/accounts/create", data={"csrf_token": _tok(c, "/admin/platform"),
                                                            "name": "Loja do João", "email": "joao@cliente.com",
                                                            "plan": "agencia"})
        password = re.search(r"Senha provisória</dt><dd><code>([^<]+)</code>", r.text).group(1)

        # suporte: entra na conta da Maria, vê os dados dela, volta
        tok = _tok(c, "/admin/platform")
        c.post(f"/admin/platform/accounts/{other}/enter", data={"csrf_token": tok})
        page = c.get("/admin/offers/bots").text
        assert "Você está na conta" in page and "Bot Secreto da Conta 1" not in page
        c.post("/admin/platform/leave", data={"csrf_token": _tok(c, "/admin/")})
        assert "Bot Secreto da Conta 1" in c.get("/admin/offers/bots").text

        c.post(f"/admin/platform/accounts/{other}/status", data={"csrf_token": _tok(c, "/admin/platform"),
                                                                 "status": "suspended"})
    with TestClient(__import__("app").app) as c2:
        assert "suspensa" in _login(c2, "maria@cliente.com").text
        assert _login(c2, "joao@cliente.com", password).status_code == 200
        assert c2.get("/admin/").status_code == 200
    with tenancy.unscoped(), session_scope() as s:
        assert s.get(Account, other).status == AccountStatus.SUSPENDED
        joao = s.scalar(select(Account).where(Account.email == "joao@cliente.com"))
        assert joao.plan == "agencia"


def test_suspended_client_cannot_keep_old_session(two_accounts):
    with TestClient(__import__("app").app) as c:
        _login(c, "maria@cliente.com")
        assert c.get("/admin/").status_code == 200
        with tenancy.unscoped(), session_scope() as s:
            s.get(Account, two_accounts["other"]).status = AccountStatus.SUSPENDED
        assert c.get("/admin/", follow_redirects=False).headers["location"] == "/admin/login"


def test_bot_cycle_uses_its_own_account_without_context(two_accounts):
    """O processo dos bots roda sem conta definida: o ciclo usa a conta DONA do bot."""
    other = two_accounts["other"]
    with tenancy.use_account(other), session_scope() as s:
        settings_service.set_values(s, {"shopee_app_id": APP_ID, "shopee_secret": SECRET})
    with tenancy.use_account(other):
        bot2 = make_bot("Bot da Maria", config={"src_shopee": "1", "src_mercadolivre": "0"})
    with tenancy.unscoped():
        result = collector.run_cycle(bot2, shopee_factory=FakeShopee().factory())
        assert result.eligible > 0, result.summary()
        with session_scope() as s:
            offers = s.scalars(select(PromoOffer)).all()
        assert offers and {o.account_id for o in offers} == {other}


def test_same_telegram_user_in_two_accounts(two_accounts):
    for account in (1, two_accounts["other"]):
        with tenancy.use_account(account):
            user_service.register_telegram_user(telegram_id=777, username="ana", first_name="Ana", last_name=None,
                                     language_code="pt-br")
    with tenancy.unscoped(), session_scope() as s:
        assert sorted(u.account_id for u in s.scalars(select(User).where(User.telegram_id == 777))) == \
            [1, two_accounts["other"]]


def test_new_rows_never_go_without_account():
    with tenancy.unscoped(), pytest.raises(IntegrityError), session_scope() as s:
        s.add(Coupon(code="SEMDONO", percent_off=5))
