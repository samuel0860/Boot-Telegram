from __future__ import annotations

import re
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from database import session_scope
from models import AppSetting, OfferStatus, PromoOffer
from services import mercadolivre, promo_service, settings_service
from services.mercadolivre import MercadoLivreClient, MLAuthError


# --------------------------------------------------------------------------- #
# API do Mercado Livre simulada (somente testes)
# --------------------------------------------------------------------------- #
PRODUCTS = {
    "MLB100": {"id": "MLB100", "name": "Fone Bluetooth X", "permalink": "https://www.mercadolivre.com.br/p/MLB100",
               "pictures": [{"url": "http://http2.mlstatic.com/fone.jpg"}],
               "buy_box_winner": {"item_id": "MLB9001", "price": 79.9, "original_price": 159.9,
                                  "category_id": "MLB1000", "shipping": {"free_shipping": True}}},
    "MLB200": {"id": "MLB200", "name": "Capinha celular", "pictures": [],
               "buy_box_winner": {"item_id": "MLB9002", "price": 10, "original_price": 30}},
    "MLB300": {"id": "MLB300", "name": "Notebook sem desconto", "pictures": [],
               "buy_box_winner": {"item_id": "MLB9003", "price": 3000}},
    "MLB400": {"id": "MLB400", "name": "Air Fryer", "pictures": [],
               "buy_box_winner": {"item_id": "MLB9004", "price": 300}},  # desconto vem do sale_price
}


def make_handler(calls: list, *, bad_credentials=False, expire_first=False):
    state = {"tokens": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path == "/oauth/token":
            body = dict(x.split("=") for x in request.content.decode().split("&"))
            if bad_credentials or body.get("client_secret") != "segredo":
                return httpx.Response(400, json={"error": "invalid_client", "message": "invalid client_id or client_secret"})
            state["tokens"] += 1
            return httpx.Response(200, json={"access_token": f"TOKEN{state['tokens']}", "expires_in": 21600})
        auth = request.headers.get("authorization", "")
        if expire_first and auth == "Bearer TOKEN1":
            return httpx.Response(401, json={"message": "invalid_token"})
        if not auth.startswith("Bearer TOKEN"):
            return httpx.Response(401, json={"message": "unauthorized"})
        if path.startswith("/highlights/MLB/category/"):
            return httpx.Response(200, json={"content": [
                {"id": "MLB100", "type": "PRODUCT"}, {"id": "MLB200", "type": "PRODUCT"},
                {"id": "MLB300", "type": "PRODUCT"}, {"id": "MLBU1", "type": "USER_PRODUCT"}]})
        if path == "/products/search":
            return httpx.Response(200, json={"results": [{"id": "MLB400"}, {"id": "MLB100"}]})
        m = re.fullmatch(r"/products/(MLB\d+)", path)
        if m:
            return httpx.Response(200, json=PRODUCTS[m.group(1)]) if m.group(1) in PRODUCTS else httpx.Response(404, json={})
        if path == "/items/MLB9004/sale_price":
            return httpx.Response(200, json={"amount": 199.9, "regular_amount": 299.9})
        if path.endswith("/sale_price"):
            return httpx.Response(200, json={"amount": None, "regular_amount": None})
        return httpx.Response(404, json={"message": "not found"})

    return handler


@pytest.fixture(autouse=True)
def clear_token_cache():
    mercadolivre._token_cache.clear()
    yield
    mercadolivre._token_cache.clear()


def ml_client(calls, **kw) -> MercadoLivreClient:
    return MercadoLivreClient("123456", "segredo", transport=httpx.MockTransport(make_handler(calls, **kw)))


def configure(**overrides):
    values = {"ml_app_id": "123456", "ml_secret_key": "segredo", "promo_enabled": "1",
              "promo_channel_id": "@canalteste", "promo_mode": "auto", "promo_min_discount": "20",
              "promo_categories": "MLB1000", "promo_keywords": "air fryer", "promo_max_posts_per_cycle": "2",
              "promo_spacing_seconds": "15", "promo_blocked_words": "capinha", "promo_active_hours": ""}
    values.update(overrides)
    with session_scope() as s:
        settings_service.set_values(s, values)


def factory(calls):
    return lambda cfg: MercadoLivreClient(cfg.app_id, cfg.secret_key,
                                          transport=httpx.MockTransport(make_handler(calls)))


# --------------------------------------------------------------------------- #
# Cliente da API
# --------------------------------------------------------------------------- #
def test_client_parses_product_and_discount():
    calls = []
    p = ml_client(calls).product("MLB100")
    assert p.price == Decimal("79.90") and p.original_price == Decimal("159.90")
    assert p.discount_pct == 50 and p.free_shipping
    assert p.image == "https://http2.mlstatic.com/fone.jpg"  # força https
    assert calls[0] == "/oauth/token"


def test_client_uses_sale_price_when_no_original():
    p = ml_client([]).product("MLB400")
    assert p.price == Decimal("199.90") and p.original_price == Decimal("299.90") and p.discount_pct == 33
    # sem promoção na API de preços: mantém o preço do produto, sem desconto
    q = ml_client([]).product("MLB300")
    assert q.price == Decimal("3000.00") and q.original_price is None and q.discount_pct is None


def test_client_bad_credentials():
    with pytest.raises(MLAuthError):
        ml_client([], bad_credentials=True).highlights("MLB1000")


def test_client_refreshes_expired_token():
    calls = []
    assert len(ml_client(calls, expire_first=True).highlights("MLB1000")) == 4
    assert calls.count("/oauth/token") == 2


def test_connection_diagnostics():
    steps = ml_client([]).test_connection("MLB1000")
    assert all(ok for _, ok, _ in steps) and len(steps) == 4


# --------------------------------------------------------------------------- #
# Configurações
# --------------------------------------------------------------------------- #
def test_secret_is_encrypted_at_rest():
    configure()
    with session_scope() as s:
        row = s.get(AppSetting, "ml_secret_key")
        assert row.is_secret and "segredo" not in row.value
        assert settings_service.get_all(s)["ml_secret_key"] == "segredo"


def test_active_hours_cross_midnight():
    from datetime import time
    cfg = settings_service.promo_config({**settings_service.DEFAULTS, "promo_active_hours": "22:00-02:00"})
    assert cfg.is_active_time(time(23, 0)) and cfg.is_active_time(time(1, 0)) and not cfg.is_active_time(time(12, 0))


# --------------------------------------------------------------------------- #
# Ciclo, filtros, fila e publicação
# --------------------------------------------------------------------------- #
def test_cycle_filters_and_queues_best():
    configure()
    result = promo_service.run_cycle(factory([]))
    assert not result.errors, result.errors
    with session_scope() as s:
        offers = {o.external_id: o for o in s.scalars(select(PromoOffer)).all()}
    # MLB200 bloqueado (capinha), MLB300 sem desconto, USER_PRODUCT ignorado
    assert set(offers) == {"MLB100", "MLB400"}
    assert offers["MLB100"].discount_pct == 50 and offers["MLB400"].discount_pct == 33
    assert {o.status for o in offers.values()} == {OfferStatus.QUEUED}
    assert result.queued == 2


def test_manual_mode_does_not_queue():
    configure(promo_mode="manual")
    promo_service.run_cycle(factory([]))
    with session_scope() as s:
        assert s.scalar(select(func.count()).where(PromoOffer.status == OfferStatus.QUEUED)) == 0
        assert s.scalar(select(func.count()).where(PromoOffer.status == OfferStatus.NEW)) == 2


def test_disabled_bot_collects_but_does_not_queue():
    configure(promo_enabled="0")
    promo_service.run_cycle(factory([]))
    with session_scope() as s:
        assert s.scalar(select(func.count()).where(PromoOffer.status == OfferStatus.QUEUED)) == 0


def test_no_repeat_and_spacing():
    configure()
    promo_service.run_cycle(factory([]))
    job = promo_service.claim_next_post()
    assert job and job.channel_id == "@canalteste" and "🔥" in job.caption and "De:" in job.caption
    promo_service.finish_post(job.offer_id, 777, None)
    assert promo_service.claim_next_post() is None  # espaçamento entre posts
    # novo ciclo não recoloca o mesmo produto na fila (não repetir por N dias)
    promo_service.run_cycle(factory([]))
    with session_scope() as s:
        assert s.get(PromoOffer, job.offer_id).status == OfferStatus.POSTED


def test_failed_post_is_marked():
    configure()
    promo_service.run_cycle(factory([]))
    job = promo_service.claim_next_post()
    promo_service.finish_post(job.offer_id, None, "Telegram: chat not found")
    with session_scope() as s:
        o = s.get(PromoOffer, job.offer_id)
        assert o.status == OfferStatus.FAILED and "chat not found" in o.error


def test_affiliate_link_used_in_post():
    configure(promo_mode="manual")
    promo_service.run_cycle(factory([]))
    with session_scope() as s:
        offer_id = s.scalar(select(PromoOffer.id).where(PromoOffer.external_id == "MLB100"))
    assert promo_service.queue_offer(offer_id, "https://meli.la/abc123", None, None)
    job = promo_service.claim_next_post()
    assert job.link == "https://meli.la/abc123"


def test_missing_credentials_reported():
    result = promo_service.run_cycle(factory([]))
    assert result.errors and "App ID" in result.errors[0]


# --------------------------------------------------------------------------- #
# Painel
# --------------------------------------------------------------------------- #
@pytest.fixture
def admin_client(fake_client):
    from admin import routes
    from admin.auth import hash_password
    from app import app
    from models import AdminUser

    with session_scope() as s:
        s.add(AdminUser(username="admin", password_hash=hash_password("SenhaForte123"), is_superuser=True))
    old = routes.promo_client_factory
    routes.promo_client_factory = factory([])
    with TestClient(app) as c:
        token = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/admin/login").text).group(1)
        c.post("/admin/login", data={"username": "admin", "password": "SenhaForte123", "csrf_token": token})
        yield c
    routes.promo_client_factory = old


def _token(c, url="/admin/promo"):
    return re.search(r'name="csrf_token" value="([^"]+)"', c.get(url).text).group(1)


def test_panel_full_flow(admin_client):
    c = admin_client
    assert c.get("/admin/promo").status_code == 200
    r = c.post("/admin/promo/settings", data={
        "csrf_token": _token(c), "ml_app_id": "123456", "ml_secret_key": "segredo",
        "promo_channel_id": "@canalteste", "promo_mode": "auto", "promo_interval_minutes": "30",
        "promo_max_posts_per_cycle": "3", "promo_spacing_seconds": "60", "promo_min_discount": "25",
        "promo_min_price": "0", "promo_max_price": "0", "promo_blocked_words": "capinha",
        "categories": ["MLB1000"], "extra_categories": "", "promo_keywords": "air fryer",
        "promo_no_repeat_days": "7", "promo_active_hours": "08:00-23:00", "promo_footer": "afiliado",
    }, follow_redirects=False)
    assert r.status_code == 303
    page = c.get("/admin/promo").text
    assert "segredo" not in page  # secret nunca volta para o HTML
    assert "Salvas" in page or "salvas" in page

    # salvar de novo sem secret mantém a chave
    c.post("/admin/promo/settings", data={"csrf_token": _token(c), "ml_app_id": "123456", "ml_secret_key": "",
                                          "promo_channel_id": "@canalteste", "categories": ["MLB1000"]})
    with session_scope() as s:
        assert settings_service.get_all(s)["ml_secret_key"] == "segredo"

    r = c.post("/admin/promo/test", data={"csrf_token": _token(c)})  # segue o redirect
    assert "token obtido" in r.text and "Busca no catálogo" in r.text

    c.post("/admin/promo/toggle", data={"csrf_token": _token(c)})
    r = c.post("/admin/promo/run", data={"csrf_token": _token(c)}, follow_redirects=False)
    assert r.headers["location"] == "/admin/promo/offers"
    offers_page = c.get("/admin/promo/offers?status=queued").text
    assert "Fone Bluetooth X" in offers_page and "-50%" in offers_page

    with session_scope() as s:
        offer_id = s.scalar(select(PromoOffer.id).where(PromoOffer.external_id == "MLB100"))
    c.post(f"/admin/promo/offers/{offer_id}/discard", data={"csrf_token": _token(c)})
    with session_scope() as s:
        assert s.get(PromoOffer, offer_id).status == OfferStatus.DISCARDED
    r = c.post(f"/admin/promo/offers/{offer_id}/queue",
               data={"csrf_token": _token(c), "affiliate_url": "javascript:alert(1)"})
    with session_scope() as s:
        assert s.get(PromoOffer, offer_id).status == OfferStatus.DISCARDED  # link inválido recusado


def test_panel_rejects_invalid_channel(admin_client):
    c = admin_client
    c.post("/admin/promo/settings", data={"csrf_token": _token(c), "promo_channel_id": "canal sem arroba"})
    with session_scope() as s:
        assert settings_service.get_all(s)["promo_channel_id"] == ""
