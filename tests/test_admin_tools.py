from __future__ import annotations

import json
import re
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from database import session_scope
from models import OfferStatus, Order, OrderStatus, Plan, PromoOffer, User
from services import alert_service, converter_service, settings_service
from services.access_service import utcnow
from services.converter_service import ConvertError
from services.mercadolivre import MercadoLivreClient

from tests.test_promo import make_handler


# --------------------------------------------------------------------------- #
# Conversor de links
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("url,expected", [
    ("https://www.mercadolivre.com.br/fone-x/p/MLB100?pdp=1", ("PRODUCT", "MLB100")),
    ("https://produto.mercadolivre.com.br/MLB-1234567890-fone-_JM", ("ITEM", "MLB1234567890")),
    ("https://www.mercadolivre.com.br/ofertas", None),
])
def test_extract_id(url, expected):
    assert converter_service.extract_id(url) == expected


def test_find_link_only_mercadolivre():
    assert converter_service.find_link("olha https://meli.la/2AbCd essa") == "https://meli.la/2AbCd"
    assert converter_service.find_link("https://evil.com/mercadolivre.com.br") is None
    assert converter_service.find_link("sem link") is None


def test_resolve_short_link_follows_redirect_headers_only():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.host == "meli.la":
            return httpx.Response(301, headers={"location": "https://mercadolivre.com/sec/abc"})
        if request.url.path == "/sec/abc":
            return httpx.Response(302, headers={"location": "https://www.mercadolivre.com.br/fone/p/MLB100?matt_tool=1"})
        return httpx.Response(200)

    assert converter_service.resolve("https://meli.la/2AbCd", transport=httpx.MockTransport(handler)) == ("PRODUCT", "MLB100")
    assert len(calls) == 2  # parou ao encontrar o ID, sem abrir a página do produto


def test_resolve_unknown_link():
    with pytest.raises(ConvertError):
        converter_service.resolve("https://meli.la/x", transport=httpx.MockTransport(lambda r: httpx.Response(200)))


def _configure_ml():
    with session_scope() as s:
        settings_service.set_values(s, {"ml_app_id": "123456", "ml_secret_key": "segredo", "promo_footer": "afiliado"})


def _factory():
    return lambda cfg: MercadoLivreClient(cfg.app_id, cfg.secret_key, transport=httpx.MockTransport(make_handler([])))


def test_import_link_creates_offer_with_affiliate():
    _configure_ml()
    redirect = httpx.MockTransport(lambda r: httpx.Response(
        301, headers={"location": "https://www.mercadolivre.com.br/fone/p/MLB100"}))
    preview = converter_service.import_link("https://meli.la/2AbCd", client_factory=_factory(), transport=redirect)
    assert preview.link == "https://meli.la/2AbCd" and preview.discount_pct == 50
    assert "Fone Bluetooth X" in preview.caption
    with session_scope() as s:
        offer = s.get(PromoOffer, preview.offer_id)
        assert offer.status == OfferStatus.NEW and offer.source == "conversor"
        assert offer.affiliate_url == "https://meli.la/2AbCd"


def test_import_link_requires_credentials():
    with pytest.raises(ConvertError, match="App ID"):
        converter_service.import_link("https://www.mercadolivre.com.br/x/p/MLB100", client_factory=_factory())


# --------------------------------------------------------------------------- #
# Vínculo do Telegram e alertas
# --------------------------------------------------------------------------- #
def test_link_code_flow():
    code = alert_service.create_link_code(admin_user_id=None)
    assert not alert_service.redeem_link_code("ERRADO", 111, "x")
    assert alert_service.redeem_link_code(code.lower(), 111, "Ana")
    assert 111 in alert_service.admin_ids()
    assert not alert_service.redeem_link_code(code, 222, "x")  # código de uso único
    alert_service.unlink_admin(111, None)
    assert 111 not in alert_service.admin_ids()


def test_expired_link_code():
    alert_service.create_link_code(admin_user_id=None)
    with session_scope() as s:
        data = json.loads(settings_service.get_all(s)["tg_link_code"])
        data["expires"] = (utcnow() - timedelta(minutes=1)).isoformat()
        settings_service.set_values(s, {"tg_link_code": json.dumps(data)})
    assert not alert_service.redeem_link_code(data["code"], 111, "x")


def _paid_undelivered_order(catalog, user):
    with session_scope() as s:
        s.add(Order(user_id=user.id, plan_id=catalog["monthly"], amount=Decimal("15"), status=OrderStatus.PAID,
                    paid_at=utcnow() - timedelta(minutes=30)))


def test_alerts_detect_and_cooldown(catalog, user):
    _paid_undelivered_order(catalog, user)
    with session_scope() as s:
        settings_service.set_values(s, {"wa_enabled": "1"})
        values = settings_service.get_all(s)
    problems = alert_service.detect_problems(values, wa_state="close")
    keys = {k for k, _ in problems}
    assert keys == {"undelivered", "wa_disconnected"}
    assert len(alert_service.alerts_to_send(problems)) == 2
    assert alert_service.alerts_to_send(problems) == []  # cooldown: não repete
    # problema resolvido e voltou: alerta de novo
    alert_service.alerts_to_send([p for p in problems if p[0] == "undelivered"])
    assert len(alert_service.alerts_to_send(problems)) == 1


def test_alerts_disabled():
    with session_scope() as s:
        settings_service.set_values(s, {"alerts_enabled": "0"})
    assert alert_service.alerts_to_send([("x", "y")]) == []


def test_summary_and_heartbeat(catalog, user):
    text = alert_service.daily_summary()
    assert "Resumo de hoje" in text and "Novos usuários no bot: <b>1</b>" in text
    with session_scope() as s:
        assert not alert_service.bot_online(settings_service.get_all(s))
    alert_service.heartbeat()
    with session_scope() as s:
        values = settings_service.get_all(s)
    assert alert_service.bot_online(values)
    now = utcnow().astimezone(alert_service.TZ).replace(hour=22)
    with session_scope() as s:
        settings_service.set_values(s, {"daily_summary_hour": "21"})
        values = settings_service.get_all(s)
    assert alert_service.summary_due(values, now)
    alert_service.mark_summary_sent(now)
    with session_scope() as s:
        assert not alert_service.summary_due(settings_service.get_all(s), now)


# --------------------------------------------------------------------------- #
# Painel
# --------------------------------------------------------------------------- #
@pytest.fixture
def panel():
    from admin.auth import hash_password
    from app import app
    from models import AdminUser

    with session_scope() as s:
        s.add(AdminUser(username="admin", password_hash=hash_password("SenhaForte123"), is_superuser=True))
    with TestClient(app) as c:
        tok = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/admin/login").text).group(1)
        c.post("/admin/login", data={"username": "admin", "password": "SenhaForte123", "csrf_token": tok})
        yield c


def _tok(c, url):
    return re.search(r'name="csrf_token" value="([^"]+)"', c.get(url).text).group(1)


def test_dashboard_onboarding(panel, catalog):
    page = panel.get("/admin/").text
    assert "Primeiros passos" in page and "Vincular o seu Telegram" in page and "Bot do Telegram" in page


def test_account_link_code_page(panel):
    r = panel.post("/admin/account/telegram/code", data={"csrf_token": _tok(panel, "/admin/account")})
    m = re.search(r"/vincular ([A-Z0-9]{6})", r.text)
    assert m, "código deve aparecer na página"
    assert alert_service.redeem_link_code(m.group(1), 999, "admin")
    assert "Desvincular" in panel.get("/admin/account").text


def test_redesigned_pages_render(panel, catalog):
    for path in ["/admin/plans", "/admin/plans/new", f"/admin/plans/{catalog['monthly']}/edit",
                 "/admin/products", "/admin/products/new", "/admin/categories", "/admin/account"]:
        r = panel.get(path)
        assert r.status_code == 200, path
    assert "R$ 15,00" in panel.get("/admin/plans").text
