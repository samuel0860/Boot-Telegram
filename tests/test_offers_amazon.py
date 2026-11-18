"""Loja AMAZON no módulo de ofertas: Creators API (OAuth2 + catalog/v1), busca por bot,
"colar link" e painel."""
from __future__ import annotations

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
from offers import amazon, collector, converter, posting
from offers.amazon import AmazonAuthError, AmazonClient
from tests.fakes import make_bot

CRED_ID, SECRET, TAG = "amzn1.application-oa2-client.abc", "segredo-amazon", "meusite-20"


def _item(asin, title, price, basis=None, pct=None, buybox=True):
    price_obj = {"money": {"amount": price, "currency": "BRL"}}
    if basis:
        price_obj["savingBasis"] = {"money": {"amount": basis, "currency": "BRL"}}
    if pct:
        price_obj["savings"] = {"percentage": pct}
    return {
        "asin": asin,
        "detailPageURL": f"https://www.amazon.com.br/dp/{asin}?tag={TAG}&linkCode=ogi",
        "itemInfo": {"title": {"displayValue": title}},
        "images": {"primary": {"large": {"url": f"https://m.media-amazon.com/images/I/{asin}.jpg"}}},
        "offersV2": {"listings": [
            {"isBuyBoxWinner": False, "price": {"money": {"amount": 1}}},
            {"isBuyBoxWinner": buybox, "price": price_obj},
        ]},
        "browseNodeInfo": {"browseNodes": [{"id": "16209062011"}]},
    }


ITEMS = [
    _item("B0ECHO0001", "Echo Dot 5ª geração", 299.0, basis=499.0),
    _item("B0CAPA0002", "Capinha para Echo Dot", 19.9, pct=50),
    _item("B0SEMDESC3", "Kindle sem desconto", 499.0),
]


class FakeAmazon:
    """Simula o token (Login with Amazon) e o catálogo, e confere cabeçalhos e corpo."""

    def __init__(self, items=ITEMS, secret=SECRET):
        self.items, self.secret = items, secret
        self.bodies: list[dict] = []
        self.tokens = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.amazon.com":
            data = json.loads(request.content)
            if (data.get("client_id"), data.get("client_secret"), data.get("scope")) != (
                    CRED_ID, self.secret, "creatorsapi::default"):
                return httpx.Response(401, json={"error": "invalid_client"})
            self.tokens += 1
            return httpx.Response(200, json={"access_token": "TOKEN", "expires_in": 3600})
        if request.headers.get("authorization") != "Bearer TOKEN":
            return httpx.Response(401, json={"errors": [{"code": "UnrecognizedClient", "message": "no"}]})
        assert request.headers.get("x-marketplace") == "www.amazon.com.br"
        body = json.loads(request.content)
        self.bodies.append(body)
        if body.get("partnerTag") != TAG:
            return httpx.Response(400, json={"errors": [{"code": "InvalidPartnerTag", "message": "bad tag"}]})
        if request.url.path.endswith("/getItems"):
            found = [i for i in self.items if i["asin"] in body["itemIds"]]
            return httpx.Response(200, json={"itemsResult": {"items": found}})
        return httpx.Response(200, json={"searchResult": {"items": self.items, "totalResultCount": len(self.items)}})

    def client(self, tag=TAG) -> AmazonClient:
        return AmazonClient(CRED_ID, SECRET, tag, transport=httpx.MockTransport(self.handler))

    def factory(self):
        return lambda cfg: AmazonClient(cfg.amazon_id, cfg.amazon_secret, cfg.amazon_tag, cfg.amazon_version,
                                        transport=httpx.MockTransport(self.handler))


def configure_amazon():
    with session_scope() as s:
        settings_service.set_values(s, {"amazon_credential_id": CRED_ID, "amazon_secret": SECRET,
                                        "amazon_partner_tag": TAG})


AMAZON_ONLY = {"src_mercadolivre": "0", "src_amazon": "1", "amazon_keywords": "echo dot", "min_discount": "20",
               "blocked_words": "capinha", "active_hours": ""}


# --------------------------------------------------------------------------- #
# Cliente
# --------------------------------------------------------------------------- #
def test_client_token_search_and_prices():
    fake = FakeAmazon()
    client = fake.client()
    products = client.search(keywords="echo dot", sort="price_asc", min_saving=20)
    client.search(keywords="kindle")
    assert fake.tokens == 1                                   # token reaproveitado
    body = fake.bodies[0]
    assert body["keywords"] == "echo dot" and body["sortBy"] == "Price:LowToHigh" and body["minSavingPercent"] == 20
    assert "offersV2.listings.price" in body["resources"]
    by_id = {p.external_id: p for p in products}
    echo = by_id["B0ECHO0001"]
    assert echo.platform == "amazon" and echo.price == Decimal("299.00") and echo.original_price == Decimal("499.00")
    assert echo.discount_pct == 40 and f"tag={TAG}" in echo.affiliate_url
    assert echo.image.startswith("https://m.media-amazon.com/") and echo.category_id == "16209062011"
    assert by_id["B0CAPA0002"].original_price == Decimal("39.80")      # pelo % de economia
    assert by_id["B0SEMDESC3"].original_price is None


def test_client_errors_and_test_connection():
    with pytest.raises(AmazonAuthError, match="credencial recusada"):
        AmazonClient(CRED_ID, "errado", TAG, transport=httpx.MockTransport(FakeAmazon().handler)).search(keywords="x")
    with pytest.raises(AmazonAuthError, match="Partner Tag"):
        FakeAmazon().client(tag="outro-20").search(keywords="x")
    steps = FakeAmazon().client().test_connection()
    assert all(ok for _, ok, _ in steps) and f"tag={TAG}" in steps[-1][2]


@pytest.mark.parametrize("url,expected", [
    ("https://www.amazon.com.br/Echo-Dot/dp/B0ECHO0001/ref=sr_1_1?keywords=x", "B0ECHO0001"),
    ("https://www.amazon.com.br/gp/product/b0echo0001", "B0ECHO0001"),
    ("https://www.amazon.com.br/gp/aw/d/B0ECHO0001?psc=1", "B0ECHO0001"),
    ("https://www.amazon.com.br/s?k=echo", None),
])
def test_parse_asin(url, expected):
    assert amazon.parse_asin(url) == expected


def test_with_tag_replaces_someone_elses_tag():
    url = amazon.with_tag("https://www.amazon.com.br/dp/B0ECHO0001?tag=outro-20&th=1", TAG)
    assert "tag=meusite-20" in url and "outro-20" not in url and "th=1" in url
    assert amazon.valid_tag("meusite-20") and not amazon.valid_tag("meusite") and not amazon.valid_tag("a b-20")


# --------------------------------------------------------------------------- #
# Busca por bot
# --------------------------------------------------------------------------- #
def test_bot_collects_from_amazon_with_affiliate_link():
    configure_amazon()
    bot = make_bot(config=AMAZON_ONLY)
    result = collector.run_cycle(bot, amazon_factory=FakeAmazon().factory())
    assert not result.errors, result.errors
    with session_scope() as s:
        offers = {o.external_id: o for o in s.scalars(select(PromoOffer)).all()}
    assert set(offers) == {"B0ECHO0001"} and offers["B0ECHO0001"].platform == "amazon"
    job = posting.claim_next_post(bot)
    assert f"tag={TAG}" in job.link and "Por R$299 pix" in job.caption


def test_missing_amazon_credentials():
    bot = make_bot(config=AMAZON_ONLY)
    result = collector.run_cycle(bot, amazon_factory=FakeAmazon().factory())
    assert any("Amazon" in e for e in result.errors)


# --------------------------------------------------------------------------- #
# "Colar link" no bot de ofertas
# --------------------------------------------------------------------------- #
def test_converter_amazon_long_and_short_links():
    configure_amazon()
    bot = make_bot()
    preview = converter.import_link(bot, "https://www.amazon.com.br/Echo/dp/B0ECHO0001?tag=outro-20\ncupom: ECHO10",
                                    amazon_factory=FakeAmazon().factory())
    assert f"tag={TAG}" in preview.link and "outro-20" not in preview.link and "ECHO10" in preview.caption
    with session_scope() as s:
        post = s.scalar(select(OfferPost).where(OfferPost.bot_id == bot))
        assert post.status == OfferStatus.NEW and s.get(PromoOffer, post.offer_id).platform == "amazon"

    redirect = httpx.MockTransport(lambda r: httpx.Response(
        301, headers={"location": "https://www.amazon.com.br/dp/B0ECHO0001?tag=meusite-20"}))
    preview = converter.import_link(bot, "https://amzn.to/3MEU", amazon_factory=FakeAmazon().factory(),
                                    transport=redirect)
    assert preview.link == "https://amzn.to/3MEU"


def test_converter_requires_amazon_credentials():
    bot = make_bot()
    with pytest.raises(converter.ConvertError, match="Amazon"):
        converter.import_link(bot, "https://www.amazon.com.br/dp/B0ECHO0001", amazon_factory=FakeAmazon().factory())


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
    old = views.amazon_client_factory
    views.amazon_client_factory = FakeAmazon().factory()
    with TestClient(app) as c:
        tok = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/admin/login").text).group(1)
        c.post("/admin/login", data={"username": "admin", "password": "SenhaForte123", "csrf_token": tok})
        yield c
    views.amazon_client_factory = old


def _tok(c, url):
    return re.search(r'name="csrf_token" value="([^"]+)"', c.get(url).text).group(1)


def test_panel_amazon_credentials_and_bot_sources(panel):
    c = panel
    r = c.post("/admin/offers/settings", data={"csrf_token": _tok(c, "/admin/offers/settings"), "store": "amazon",
                                               "amazon_credential_id": CRED_ID, "amazon_partner_tag": "sem-tag"})
    assert "Partner Tag inválido" in r.text
    r = c.post("/admin/offers/settings", data={"csrf_token": _tok(c, "/admin/offers/settings"), "store": "amazon",
                                               "amazon_credential_id": CRED_ID, "amazon_secret": SECRET,
                                               "amazon_partner_tag": TAG, "amazon_version": "3.1"})
    assert "Credenciais da Amazon salvas" in r.text and SECRET not in r.text
    with session_scope() as s:
        row = s.get(AppSetting, "amazon_secret")
        assert row.is_secret and SECRET not in row.value
    r = c.post("/admin/offers/settings/test", data={"csrf_token": _tok(c, "/admin/offers/settings"),
                                                    "store": "amazon"})
    assert "Amazon · Busca de ofertas" in r.text

    bot = make_bot(telegram=("@canal",))
    url = f"/admin/offers/bots/{bot}"
    assert "O que buscar na Amazon" in c.get(f"{url}?tab=busca").text
    c.post(f"{url}/search", data={"csrf_token": _tok(c, url), "src_amazon": "1", "amazon_keywords": "echo dot",
                                  "amazon_sort": "reviews", "amazon_nodes": "16209062011", "min_discount": "20"})
    with session_scope() as s:
        from offers import bots as bot_service
        cfg = bot_service.config_for(s, s.get(OfferBot, bot))
    assert cfg.use_amazon and not cfg.use_mercadolivre and cfg.amazon_sort == "reviews"
    assert cfg.amazon_nodes == ["16209062011"] and cfg.amazon_ready
    r = c.post(f"{url}/search", data={"csrf_token": _tok(c, url), "src_amazon": "1", "amazon_nodes": "abc"})
    assert "Categoria da Amazon inválida" in r.text

    r = c.post(f"{url}/run", data={"csrf_token": _tok(c, url)})
    assert "Busca concluída" in r.text
    page = c.get(f"/admin/offers?bot={bot}&status=all").text
    assert "Echo Dot 5ª geração" in page and "badge-store-amazon" in page
