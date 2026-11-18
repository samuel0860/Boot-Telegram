"""Loja SHOPEE no módulo de ofertas: API de afiliados (GraphQL assinado), busca por bot,
"colar link" e painel."""
from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from core import settings_service
from database import session_scope
from models import AppSetting, OfferBot, OfferPost, OfferStatus, PromoOffer
from offers import collector, converter, posting, shopee
from offers.shopee import ShopeeAuthError, ShopeeClient
from tests.fakes import make_bot

APP_ID, SECRET = "18300001234", "segredo-shopee"
NODES = [
    {"itemId": 2222, "shopId": 1111, "productName": "Air Fryer Mondial 4L", "priceMin": "199.90",
     "priceMax": "249.90", "priceDiscountRate": 50, "imageUrl": "https://down-br.img.susercontent.com/file/a",
     "productLink": "https://shopee.com.br/product/1111/2222", "offerLink": "https://s.shopee.com.br/AFF1",
     "commissionRate": "0.08", "sales": 1500, "ratingStar": "4.8", "shopName": "Loja X", "productCatIds": [100, 200]},
    {"itemId": 3333, "shopId": 1111, "productName": "Capinha de celular", "priceMin": "9.90",
     "priceDiscountRate": 60, "imageUrl": None, "productLink": "https://shopee.com.br/product/1111/3333",
     "offerLink": "https://s.shopee.com.br/AFF2"},
    {"itemId": 4444, "shopId": 1111, "productName": "Sem desconto", "priceMin": "50", "priceDiscountRate": 0,
     "productLink": "https://shopee.com.br/product/1111/4444", "offerLink": "https://s.shopee.com.br/AFF3"},
]


class FakeShopee:
    """Simula a Open API de afiliados e CONFERE a assinatura de cada requisição."""

    def __init__(self, nodes=NODES, secret=SECRET):
        self.nodes, self.secret = nodes, secret
        self.queries: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        m = re.fullmatch(r"SHA256 Credential=(\d+), Timestamp=(\d+), Signature=([0-9a-f]{64})",
                         request.headers.get("authorization", ""))
        expected = hashlib.sha256(f"{APP_ID}{m.group(2) if m else ''}{body}{self.secret}".encode()).hexdigest()
        if not m or m.group(1) != APP_ID or m.group(3) != expected:
            return httpx.Response(200, json={"errors": [{"message": "Invalid Signature",
                                                         "extensions": {"code": 10020}}]})
        query = json.loads(body)["query"]
        self.queries.append(query)
        if "generateShortLink" in query:
            return httpx.Response(200, json={"data": {"generateShortLink": {"shortLink": "https://s.shopee.com.br/GEN"}}})
        item = re.search(r"itemId: (\d+)", query)
        nodes = [n for n in self.nodes if str(n["itemId"]) == item.group(1)] if item else self.nodes
        return httpx.Response(200, json={"data": {"productOfferV2": {"nodes": nodes, "pageInfo": {"page": 1}}}})

    def client(self) -> ShopeeClient:
        return ShopeeClient(APP_ID, SECRET, transport=httpx.MockTransport(self.handler))

    def factory(self):
        return lambda cfg: ShopeeClient(cfg.shopee_app_id, cfg.shopee_secret,
                                        transport=httpx.MockTransport(self.handler))


def configure_shopee():
    with session_scope() as s:
        settings_service.set_values(s, {"shopee_app_id": APP_ID, "shopee_secret": SECRET})


SHOPEE_ONLY = {"src_mercadolivre": "0", "src_shopee": "1", "shopee_keywords": "air fryer", "min_discount": "20",
               "blocked_words": "capinha", "active_hours": ""}


# --------------------------------------------------------------------------- #
# Cliente
# --------------------------------------------------------------------------- #
def test_client_signs_and_parses_offers():
    fake = FakeShopee()
    products = fake.client().offers(keyword='air "fryer"', sort="commission", limit=5)
    assert len(products) == 3
    p = products[0]
    assert p.platform == "shopee" and p.external_id == "1111.2222" and p.title == "Air Fryer Mondial 4L"
    assert p.price == Decimal("199.90") and p.original_price == Decimal("399.80") and p.discount_pct == 50
    assert p.affiliate_url == "https://s.shopee.com.br/AFF1" and p.category_id == "200"
    q = fake.queries[0]
    assert 'keyword: "air \\"fryer\\""' in q and "sortType: 5" in q and "limit: 5" in q   # escape seguro


def test_client_wrong_secret_and_short_link():
    bad = ShopeeClient(APP_ID, "errado", transport=httpx.MockTransport(FakeShopee().handler))
    with pytest.raises(ShopeeAuthError, match="assinatura"):
        bad.offers(keyword="x")
    assert FakeShopee().client().short_link("https://shopee.com.br/product/1/2") == "https://s.shopee.com.br/GEN"
    steps = FakeShopee().client().test_connection()
    assert all(ok for _, ok, _ in steps) and "s.shopee.com.br" in steps[-1][2]


@pytest.mark.parametrize("url,expected", [
    ("https://shopee.com.br/Air-Fryer-Mondial-i.1111.2222?sp_atk=x", (1111, 2222)),
    ("https://shopee.com.br/product/1111/2222", (1111, 2222)),
    ("https://shopee.com.br/lojax", None),
])
def test_parse_item_ids(url, expected):
    assert shopee.parse_item_ids(url) == expected


# --------------------------------------------------------------------------- #
# Busca por bot
# --------------------------------------------------------------------------- #
def test_bot_collects_from_shopee_with_affiliate_link():
    configure_shopee()
    bot = make_bot(config=SHOPEE_ONLY)
    result = collector.run_cycle(bot, shopee_factory=FakeShopee().factory())
    assert not result.errors, result.errors
    with session_scope() as s:
        offers = {o.external_id: o for o in s.scalars(select(PromoOffer)).all()}
    # capinha bloqueada, item sem desconto fora do filtro
    assert set(offers) == {"1111.2222"} and offers["1111.2222"].platform == "shopee"
    assert offers["1111.2222"].affiliate_url == "https://s.shopee.com.br/AFF1"   # comissão já no link
    job = posting.claim_next_post(bot)
    assert job.link == "https://s.shopee.com.br/AFF1" and "Por R$199,90 pix" in job.caption


def test_store_placeholder_and_missing_credentials():
    bot = make_bot(config={**SHOPEE_ONLY, "template": "{titulo} na {loja}: {link}"})
    result = collector.run_cycle(bot, shopee_factory=FakeShopee().factory())
    assert any("Shopee" in e for e in result.errors)
    configure_shopee()
    collector.run_cycle(bot, shopee_factory=FakeShopee().factory())
    job = posting.claim_next_post(bot)
    assert job.caption == "Air Fryer Mondial 4L na Shopee: https://s.shopee.com.br/AFF1"


def test_both_stores_in_one_bot():
    from tests.fakes import configure_ml, ml_factory
    configure_ml()
    configure_shopee()
    bot = make_bot(config={**SHOPEE_ONLY, "src_mercadolivre": "1", "categories": "MLB1000", "keywords": ""})
    result = collector.run_cycle(bot, ml_factory(), shopee_factory=FakeShopee().factory())
    assert not result.errors, result.errors
    with session_scope() as s:
        platforms = set(s.scalars(select(PromoOffer.platform)).all())
    assert platforms == {"mercadolivre", "shopee"}


# --------------------------------------------------------------------------- #
# "Colar link" no bot de ofertas
# --------------------------------------------------------------------------- #
def _redirect_to(url):
    return httpx.MockTransport(lambda r: httpx.Response(302, headers={"location": url}))


def test_converter_long_link_generates_affiliate():
    configure_shopee()
    bot = make_bot()
    nodes = [dict(NODES[0], offerLink=None)]   # sem offerLink -> gera o link curto
    preview = converter.import_link(bot, "olha https://shopee.com.br/Air-Fryer-i.1111.2222\ncupom: SHOPEE10",
                                    shopee_factory=FakeShopee(nodes).factory())
    assert preview.link == "https://s.shopee.com.br/GEN" and "SHOPEE10" in preview.caption
    with session_scope() as s:
        post = s.scalar(select(OfferPost).where(OfferPost.bot_id == bot))
        assert post.status == OfferStatus.NEW and s.get(PromoOffer, post.offer_id).platform == "shopee"


def test_converter_short_link_keeps_users_affiliate():
    configure_shopee()
    bot = make_bot()
    preview = converter.import_link(bot, "https://s.shopee.com.br/MEU123", shopee_factory=FakeShopee().factory(),
                                    transport=_redirect_to("https://shopee.com.br/product/1111/2222?smtt=1"))
    assert preview.link == "https://s.shopee.com.br/MEU123"


def test_converter_requires_shopee_credentials():
    bot = make_bot()
    with pytest.raises(converter.ConvertError, match="Shopee"):
        converter.import_link(bot, "https://shopee.com.br/product/1111/2222", shopee_factory=FakeShopee().factory())


# --------------------------------------------------------------------------- #
# Painel
# --------------------------------------------------------------------------- #
@pytest.fixture
def panel(fake_client):
    from admin.auth import hash_password
    from admin.views import offers as views
    from app import app
    from models import AdminUser

    with session_scope() as s:
        s.add(AdminUser(username="admin", password_hash=hash_password("SenhaForte123"), is_superuser=True))
    old = views.shopee_client_factory
    views.shopee_client_factory = FakeShopee().factory()
    with TestClient(app) as c:
        tok = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/admin/login").text).group(1)
        c.post("/admin/login", data={"username": "admin", "password": "SenhaForte123", "csrf_token": tok})
        yield c
    views.shopee_client_factory = old


def _tok(c, url):
    return re.search(r'name="csrf_token" value="([^"]+)"', c.get(url).text).group(1)


def test_panel_shopee_credentials_and_bot_sources(panel):
    c = panel
    r = c.post("/admin/offers/settings", data={"csrf_token": _tok(c, "/admin/offers/settings"), "store": "shopee",
                                               "shopee_app_id": APP_ID, "shopee_secret": SECRET})
    assert "Credenciais da Shopee salvas" in r.text and SECRET not in r.text
    with session_scope() as s:
        row = s.get(AppSetting, "shopee_secret")
        assert row.is_secret and SECRET not in row.value
    r = c.post("/admin/offers/settings/test", data={"csrf_token": _tok(c, "/admin/offers/settings"),
                                                    "store": "shopee"})
    assert "Shopee · Busca de ofertas" in r.text

    bot = make_bot(telegram=("@canal",))
    url = f"/admin/offers/bots/{bot}"
    assert "O que buscar na Shopee" in c.get(f"{url}?tab=busca").text
    c.post(f"{url}/search", data={"csrf_token": _tok(c, url), "src_shopee": "1", "shopee_keywords": "air fryer",
                                  "shopee_sort": "commission", "shopee_categories": "100, 200",
                                  "min_discount": "20"})
    with session_scope() as s:
        from offers import bots as bot_service
        cfg = bot_service.config_for(s, s.get(OfferBot, bot))
    assert cfg.use_shopee and not cfg.use_mercadolivre and cfg.shopee_sort == "commission"
    assert cfg.shopee_categories == [100, 200] and cfg.shopee_ready
    r = c.post(f"{url}/search", data={"csrf_token": _tok(c, url), "src_shopee": "1", "shopee_categories": "abc"})
    assert "Categoria da Shopee inválida" in r.text

    r = c.post(f"{url}/run", data={"csrf_token": _tok(c, url)})
    assert "Busca concluída" in r.text
    page = c.get(f"/admin/offers?bot={bot}&status=all").text
    assert "Air Fryer Mondial 4L" in page and "badge-store-shopee" in page and "s.shopee.com.br/AFF1" in page
