"""Simuladores usados SOMENTE nos testes: API do Mercado Livre, Evolution API (WhatsApp)
e criação rápida de bots de ofertas."""
from __future__ import annotations

import json
import re
from datetime import timedelta
from decimal import Decimal

import httpx

from core import settings_service
from core.audit import utcnow
from database import session_scope
from models import OfferBot, OfferPost, OfferStatus, OfferTarget, PromoOffer, TargetChannel, WaStatus
from offers.mercadolivre import MercadoLivreClient
from offers.whatsapp import EvolutionClient

# --------------------------------------------------------------------------- #
# Mercado Livre
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


def ml_factory(calls: list | None = None):
    calls = [] if calls is None else calls
    return lambda cfg: MercadoLivreClient(cfg.app_id, cfg.secret_key, transport=httpx.MockTransport(make_handler(calls)))


def configure_ml() -> None:
    with session_scope() as s:
        settings_service.set_values(s, {"ml_app_id": "123456", "ml_secret_key": "segredo"})


# --------------------------------------------------------------------------- #
# Bots de ofertas
# --------------------------------------------------------------------------- #
G1 = {"id": "120363000000000001@g.us", "name": "Ofertas VIP"}
G2 = {"id": "120363000000000002@g.us", "name": "Promo Casa"}

SEARCH_DEFAULTS = {"mode": "auto", "min_discount": "20", "categories": "MLB1000", "keywords": "air fryer",
                   "max_posts_per_cycle": "2", "spacing_seconds": "15", "blocked_words": "capinha",
                   # testes antigos do Mercado Livre postam sem meli.la (a trava tem teste próprio)
                   "ml_require_affiliate": "0",
                   "active_hours": ""}


def make_bot(name="Ofertas Tech", *, active=True, telegram=("@canalteste",), whatsapp=(), config=None,
             state=None) -> int:
    with session_scope() as s:
        bot = OfferBot(name=name, is_active=active, config={**SEARCH_DEFAULTS, **(config or {})}, state=state or {})
        s.add(bot)
        s.flush()
        bot.wa_instance = f"ofertas{bot.id}"
        for t in telegram:
            s.add(OfferTarget(bot_id=bot.id, channel=TargetChannel.TELEGRAM, target_id=t, name=t))
        for g in whatsapp:
            s.add(OfferTarget(bot_id=bot.id, channel=TargetChannel.WHATSAPP, target_id=g["id"], name=g["name"]))
        return bot.id


def make_post(bot_id: int, ext="MLB1", *, status=OfferStatus.QUEUED, wa=WaStatus.PENDING, queued_ago=timedelta(0),
              discount_pct=50, **post_fields) -> tuple[int, int]:
    """Cria oferta + registro do bot. Retorna (offer_id, post_id)."""
    with session_scope() as s:
        offer = PromoOffer(platform="mercadolivre", external_id=ext, title="Fone *Top* ~X~",
                           product_url="https://ml/p/1", image_url="https://http2.mlstatic.com/a.jpg",
                           price=Decimal("79.90"), original_price=Decimal("159.90"), discount_pct=discount_pct,
                           free_shipping=True)
        s.add(offer)
        s.flush()
        post = OfferPost(offer_id=offer.id, bot_id=bot_id, status=status, queued_at=utcnow(), wa_status=wa,
                         wa_queued_at=utcnow() - queued_ago, **post_fields)
        s.add(post)
        s.flush()
        return offer.id, post.id


# --------------------------------------------------------------------------- #
# Evolution API (WhatsApp)
# --------------------------------------------------------------------------- #
class FakeEvolution:
    def __init__(self, state="open", fail_media=False, fail_all=False):
        self.state = state
        self.fail_media = fail_media
        self.fail_all = fail_all
        self.sent: list[tuple[str, str, dict, str]] = []   # (tipo, destino, corpo, instância)
        self.created: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers.get("apikey") == "chave-teste"
        path = request.url.path
        if path.startswith("/instance/connectionState/"):
            if self.state == "missing":
                return httpx.Response(404, json={"response": {"message": ["The instance does not exist"]}})
            return httpx.Response(200, json={"instance": {"state": self.state}})
        if path == "/instance/create":
            self.created.append(json.loads(request.content)["instanceName"])
            self.state = "connecting"
            return httpx.Response(201, json={"instance": {"instanceName": "x"}})
        if path.startswith("/instance/connect/"):
            return httpx.Response(200, json={"base64": "data:image/png;base64,AAAA", "code": "x"})
        if path == "/instance/fetchInstances":
            return httpx.Response(200, json=[{"ownerJid": "5511999990000@s.whatsapp.net"}])
        if path.startswith("/group/fetchAllGroups/"):
            return httpx.Response(200, json=[{"id": G2["id"], "subject": G2["name"], "size": 40},
                                             {"id": G1["id"], "subject": G1["name"], "size": 120}])
        if path.startswith("/message/"):
            body = json.loads(request.content)
            if self.fail_all or (self.fail_media and "sendMedia" in path):
                return httpx.Response(400, json={"response": {"message": "falha simulada"}})
            self.sent.append((path.split("/")[2], body["number"], body, path.split("/")[3]))
            return httpx.Response(201, json={"key": {"id": "MSG"}})
        if path.startswith("/instance/logout/"):
            self.state = "close"
            return httpx.Response(200, json={"status": "SUCCESS"})
        return httpx.Response(404, json={})

    def client(self) -> EvolutionClient:
        return EvolutionClient("http://whatsapp:8080", "chave-teste", transport=httpx.MockTransport(self.handler))
