"""TikTok Shop (API de afiliado criador) + melhorias da auditoria: pontuação, link marcado (sub_id),
prova social/hora no post e Mercado Livre só com link de afiliado."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from core import settings_service
from database import session_scope
from models import OfferBot, OfferPost, OfferStatus, PromoOffer
from offers import bots as bot_service
from offers import collector, post_template, posting, tiktok, tracking
from offers.config import parse
from tests.fakes import make_bot
from tests.test_offers_shopee import FakeShopee, configure_shopee

KEY, SECRET, TOKEN = "6appkey", "tt-secret", "ACCESS-1"
ITEMS = [
    {"id": "1729384756001", "title": "Fone Bluetooth TWS Pro", "main_image_url": "https://p16-oec-va.ibyteimg.com/a.jpg",
     "detail_link": "https://shop.tiktok.com/view/product/1729384756001", "has_inventory": True, "units_sold": 2500,
     "original_price": {"currency": "BRL", "minimum_amount": "199.90"},
     "sales_price": {"currency": "BRL", "minimum_amount": "89.90"},
     "commission": {"rate": 1500, "currency": "BRL", "amount": "13.48"}},
    {"id": "1729384756002", "title": "Produto sem link", "has_inventory": True,
     "sales_price": {"currency": "BRL", "minimum_amount": "50.00"},
     "original_price": {"currency": "BRL", "minimum_amount": "100.00"}, "commission": {"rate": 1000}},
    {"id": "1729384756003", "title": "Esgotado", "has_inventory": False,
     "sales_price": {"currency": "BRL", "minimum_amount": "10.00"}},
]


def _expected_sign(path: str, params: dict, body: str) -> str:
    """Implementação independente da assinatura (para conferir a do cliente)."""
    base = path + "".join(k + str(params[k]) for k in sorted(params) if k not in ("sign", "access_token")) + body
    return hmac.new(SECRET.encode(), (SECRET + base + SECRET).encode(), hashlib.sha256).hexdigest()


class FakeTikTok:
    def __init__(self, items=ITEMS, linked=("1729384756001",)):
        self.items, self.linked, self.calls = items, set(linked), []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = urlsplit(str(request.url))
        params = dict(parse_qsl(url.query))
        body = request.content.decode()
        if url.netloc == "auth.tiktok-shops.com":
            self.calls.append(("auth", url.path, params))
            if params.get("auth_code") == "ruim":
                return httpx.Response(200, json={"code": 36004004, "message": "invalid auth code"})
            return httpx.Response(200, json={"code": 0, "data": {
                "access_token": "ACCESS-NEW" if "refresh" in url.path else TOKEN, "access_token_expire_in": 4102444800,
                "refresh_token": "REFRESH-1", "refresh_token_expire_in": 4102444800, "user_name": "criador.teste"}})
        assert request.headers["x-tts-access-token"] in (TOKEN, "ACCESS-NEW")
        if params.get("sign") != _expected_sign(url.path, params, body):
            return httpx.Response(200, json={"code": 106001, "message": "Invalid sign"})
        payload = json.loads(body or "{}")
        self.calls.append(("api", url.path, payload))
        if url.path == tiktok.SEARCH_PATH:
            return httpx.Response(200, json={"code": 0, "data": {"products": self.items, "total_count": 3}})
        if url.path == tiktok.LINKS_PATH:
            ids = payload["material"]["ids"]
            return httpx.Response(200, json={"code": 0, "data": {"sharing_links": [
                {"material_id": i, "sharing_link": f"https://vt.tiktok.com/AFF{i[-3:]}"} for i in ids if i in self.linked]}})
        return httpx.Response(404, json={"code": 404})

    @property
    def transport(self):
        return httpx.MockTransport(self.handler)

    def factory(self):
        return lambda cfg: tiktok.TikTokClient(cfg.tiktok_app_key, cfg.tiktok_app_secret, TOKEN, transport=self.transport)


def configure_tiktok(connected=True):
    values = {"tiktok_app_key": KEY, "tiktok_app_secret": SECRET,
              "tiktok_auth_url": "https://services.tiktokshop.com/open/authorize?service_id=99"}
    if connected:
        values.update(tiktok_access_token=TOKEN, tiktok_refresh_token="REFRESH-1", tiktok_access_expires="4102444800")
    with session_scope() as s:
        settings_service.set_values(s, values)


# --------------------------------------------------------------------------- #
# Cliente TikTok
# --------------------------------------------------------------------------- #
def test_client_signs_searches_and_keeps_only_affiliate_links():
    fake = FakeTikTok()
    with tiktok.TikTokClient(KEY, SECRET, TOKEN, transport=fake.transport) as c:
        products = c.search_with_links(["fone"], sort="commission_rate", min_commission=10)
    assert [p.external_id for p in products] == ["1729384756001"]          # sem link e esgotado ficam de fora
    p = products[0]
    assert p.platform == "tiktok" and p.affiliate_url == "https://vt.tiktok.com/AFF001"
    assert p.price == Decimal("89.90") and p.original_price == Decimal("199.90") and p.discount_pct == 55
    assert p.commission_rate == Decimal("0.1500") and p.sold == 2500
    search = next(c for c in fake.calls if c[1] == tiktok.SEARCH_PATH)[2]
    assert search == {"title_keywords": ["fone"], "commission_rate_range": {"rate_ge": 1000}}


def test_wrong_secret_is_reported():
    with tiktok.TikTokClient(KEY, "outro-secret", TOKEN, transport=FakeTikTok().transport) as c:
        with pytest.raises(tiktok.TikTokError, match="Invalid sign"):
            c.search()


def test_exchange_and_refresh_tokens():
    fake = FakeTikTok()
    t = tiktok.exchange_code(KEY, SECRET, "CODE-1", transport=fake.transport)
    assert t == {"access_token": TOKEN, "refresh_token": "REFRESH-1", "expires": 4102444800, "name": "criador.teste"}
    with pytest.raises(tiktok.TikTokAuthError):
        tiktok.exchange_code(KEY, SECRET, "ruim", transport=fake.transport)

    configure_tiktok()
    with session_scope() as s:
        settings_service.set_values(s, {"tiktok_access_expires": "100"})         # vencido
    assert bot_service.tiktok_token(transport=fake.transport) == "ACCESS-NEW"
    with session_scope() as s:
        assert settings_service.get_all(s)["tiktok_access_token"] == "ACCESS-NEW"
    assert bot_service.tiktok_token(transport=fake.transport) == "ACCESS-NEW"   # ainda válido: não renova


def test_bot_collects_from_tiktok():
    configure_tiktok()
    bot = make_bot(config={"src_tiktok": "1", "src_mercadolivre": "0", "min_discount": "20"})
    result = collector.run_cycle(bot, tiktok_factory=FakeTikTok().factory())
    assert result.eligible == 1 and result.queued == 1, result.summary()
    with session_scope() as s:
        offer = s.scalar(select(PromoOffer).where(PromoOffer.platform == "tiktok"))
        assert offer.link == "https://vt.tiktok.com/AFF001" and offer.sold == 2500


def test_tiktok_not_connected_is_reported():
    configure_tiktok(connected=False)
    bot = make_bot(config={"src_tiktok": "1", "src_mercadolivre": "0"})
    result = collector.run_cycle(bot, tiktok_factory=FakeTikTok().factory())
    assert any("TikTok Shop" in e for e in result.errors)


# --------------------------------------------------------------------------- #
# Auditoria: pontuação, Mercado Livre sem afiliado, link marcado, prova social e hora
# --------------------------------------------------------------------------- #
def _offer(**kw) -> PromoOffer:
    base = dict(platform="shopee", external_id="x", title="X", product_url="https://x", price=Decimal("10"))
    return PromoOffer(**{**base, **kw})


def test_offer_score_prefers_commission_rating_and_sales():
    cheap = _offer(discount_pct=80)
    good = _offer(discount_pct=40, commission_rate=Decimal("0.12"), rating=Decimal("4.9"), sold=3000)
    assert collector.offer_score(good) > collector.offer_score(cheap)
    assert collector.offer_score(cheap) == 80.0


def test_rank_by_score_changes_order_only_when_enabled():
    configure_shopee()
    nodes = [
        {"itemId": 1, "shopId": 9, "productName": "Cabo USB", "priceMin": "15", "priceDiscountRate": 80,
         "offerLink": "https://s.shopee.com.br/A", "commissionRate": "0.01", "ratingStar": "3.9", "sales": 2},
        {"itemId": 2, "shopId": 9, "productName": "Air Fryer", "priceMin": "199", "priceDiscountRate": 40,
         "offerLink": "https://s.shopee.com.br/B", "commissionRate": "0.12", "ratingStar": "4.9", "sales": 5000},
    ]
    picked = {}
    for rank in ("discount", "score"):
        bot = make_bot(f"Bot {rank}", config={"src_shopee": "1", "src_mercadolivre": "0", "rank_by": rank,
                                               "max_posts_per_cycle": "1"})
        collector.run_cycle(bot, shopee_factory=FakeShopee(nodes).factory())
        with session_scope() as s:
            post = s.scalar(select(OfferPost).where(OfferPost.bot_id == bot, OfferPost.status == OfferStatus.QUEUED))
            picked[rank] = s.get(PromoOffer, post.offer_id).title
    assert picked == {"discount": "Cabo USB", "score": "Air Fryer"}


def test_mercadolivre_without_affiliate_is_never_auto_posted(fake_client):
    from tests.fakes import configure_ml, ml_factory

    configure_ml()
    bot = make_bot(config={"categories": "MLB1000", "min_discount": "10", "ml_require_affiliate": "1"})
    result = collector.run_cycle(bot, ml_factory())
    assert result.queued == 0 and result.no_affiliate >= 1 and "aguardando o seu link" in result.summary()
    with session_scope() as s:
        assert not s.scalars(select(OfferPost).where(OfferPost.status == OfferStatus.QUEUED)).all()


def test_tracked_shopee_link_with_sub_ids_and_fallback():
    configure_shopee()
    fake = FakeShopee()
    bot = make_bot(config={"src_shopee": "1", "src_mercadolivre": "0", "track_links": "1"})
    collector.run_cycle(bot, shopee_factory=fake.factory())
    tracking.shopee_factory = fake.factory()
    try:
        job = posting.claim_next_post(bot)
    finally:
        tracking.shopee_factory = None
    assert job.link == "https://s.shopee.com.br/GEN" and "s.shopee.com.br/GEN" in job.caption
    gen = next(q for q in fake.queries if "generateShortLink" in q)
    assert re.search(rf'subIds: \["bot{bot}", "tg", "h\d\d"\]', gen)

    # sem a opção (ou se a Shopee falhar) sai o link normal
    with session_scope() as s:
        offer = s.scalars(select(PromoOffer)).first()
        cfg = bot_service.config_for(s, s.get(OfferBot, bot))
        off = parse({}, shopee_app_id="1", shopee_secret="2")
        assert tracking.link_for(offer, off, bot, "telegram") == offer.link
        tracking.shopee_factory = lambda c: (_ for _ in ()).throw(RuntimeError("fora do ar"))
        try:
            assert tracking.link_for(offer, cfg, bot, "whatsapp") == offer.link
        finally:
            tracking.shopee_factory = None


def test_social_proof_and_price_time_placeholders():
    data = post_template.PostData(title="Air Fryer", price=Decimal("199.9"), link="https://x", rating=Decimal("4.75"),
                                  sold=1500, seen_at=datetime(2026, 11, 27, 23, 30, tzinfo=UTC))
    tpl = "{titulo}\n{prova}\nPreço das {hora}, pode mudar\n{link}"
    text = post_template.render_whatsapp(data, tpl)
    assert "⭐ 4,8 · +1,5 mil vendidos" in text and "Preço das 20h30" in text      # 23:30 UTC = 20:30 Brasília
    bare = post_template.PostData(title="Sem dados", price=Decimal("10"), link="https://x")
    assert post_template.render_whatsapp(bare, tpl) == "Sem dados\nhttps://x"       # linhas sem dado somem


def test_panel_tiktok_connect_flow_and_ml_publish_guard(fake_client):
    from admin.auth import hash_password
    from admin.views import offers as views
    from app import app
    from models import AdminUser

    with session_scope() as s:
        s.add(AdminUser(username="admin", password_hash=hash_password("SenhaForte123"), is_superuser=True))
    fake = FakeTikTok()
    views.tiktok_transport = fake.transport
    try:
        with TestClient(app) as c:
            tok = lambda url: re.search(r'name="csrf_token" value="([^"]+)"', c.get(url).text).group(1)  # noqa: E731
            c.post("/admin/login", data={"username": "admin", "password": "SenhaForte123", "csrf_token": tok("/admin/login")})
            page = "/admin/offers/settings"
            assert "TikTok Shop" in c.get(page).text
            r = c.post(page, data={"csrf_token": tok(page), "store": "tiktok", "tiktok_app_key": KEY,
                                   "tiktok_app_secret": SECRET, "tiktok_auth_url": "http://inseguro"})
            assert "precisa começar com https://" in r.text
            c.post(page, data={"csrf_token": tok(page), "store": "tiktok", "tiktok_app_key": KEY,
                               "tiktok_app_secret": SECRET,
                               "tiktok_auth_url": "https://services.tiktokshop.com/open/authorize?service_id=99"})
            go = c.get("/admin/offers/tiktok/connect", follow_redirects=False)
            state = re.search(r"state=([^&]+)", go.headers["location"]).group(1)
            assert go.headers["location"].startswith("https://services.tiktokshop.com/open/authorize?service_id=99&")
            bad = c.get("/admin/offers/tiktok/callback?code=X&state=errado")
            assert "inválida ou expirada" in bad.text
            # o state é de uso único: gera outro
            state = re.search(r"state=([^&]+)", c.get("/admin/offers/tiktok/connect",
                                                      follow_redirects=False).headers["location"]).group(1)
            ok = c.get(f"/admin/offers/tiktok/callback?code=CODE-1&state={state}")
            assert "Conta do TikTok Shop conectada (criador.teste)" in ok.text and "conectado" in ok.text
            with session_scope() as s:
                vals = settings_service.get_all(s)
                assert vals["tiktok_refresh_token"] == "REFRESH-1"

            # Mercado Livre sem meli.la não é publicado pelo painel
            bot = make_bot(telegram=("@canal",))
            with session_scope() as s:
                offer = PromoOffer(platform="mercadolivre", external_id="MLB1", title="TV", product_url="https://ml",
                                   price=Decimal("1000"))
                s.add(offer)
                s.flush()
                s.add(OfferPost(offer_id=offer.id, bot_id=bot, status=OfferStatus.NEW))
                oid = offer.id
            r = c.post(f"/admin/offers/bots/{bot}/offers/{oid}/queue",
                       data={"csrf_token": tok(f"/admin/offers?bot={bot}"), "target": "telegram"})
            assert "Cole o seu link de afiliado do Mercado Livre" in r.text
            r = c.post(f"/admin/offers/bots/{bot}/offers/{oid}/queue",
                       data={"csrf_token": tok(f"/admin/offers?bot={bot}"), "target": "telegram",
                             "affiliate_url": "https://meli.la/ABC"})
            assert "Oferta na fila" in r.text
    finally:
        views.tiktok_transport = None
