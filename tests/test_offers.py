"""MÓDULO DE OFERTAS: API do Mercado Livre, coleta por bot, fila/publicação e painel."""
from __future__ import annotations

import re
from datetime import time
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from core import settings_service
from database import session_scope
from models import AppSetting, OfferBot, OfferPost, OfferStatus, OfferTarget, PromoOffer
from offers import bots as bot_service
from offers import collector, mercadolivre, posting
from offers import config as offer_config
from offers.mercadolivre import MercadoLivreClient, MLAuthError
from tests.fakes import configure_ml, make_bot, make_handler, ml_factory


@pytest.fixture(autouse=True)
def clear_token_cache():
    mercadolivre._token_cache.clear()
    yield
    mercadolivre._token_cache.clear()


def ml_client(calls, **kw) -> MercadoLivreClient:
    return MercadoLivreClient("123456", "segredo", transport=httpx.MockTransport(make_handler(calls, **kw)))


def _posts(bot_id: int) -> dict[str, OfferPost]:
    with session_scope() as s:
        rows = s.execute(select(PromoOffer.external_id, OfferPost).join(OfferPost, OfferPost.offer_id == PromoOffer.id)
                         .where(OfferPost.bot_id == bot_id)).all()
        return {ext: post for ext, post in rows}


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
    q = ml_client([]).product("MLB300")
    assert q.price == Decimal("3000.00") and q.original_price is None and q.discount_pct is None


def test_client_bad_credentials_and_token_refresh():
    with pytest.raises(MLAuthError):
        ml_client([], bad_credentials=True).highlights("MLB1000")
    calls = []
    assert len(ml_client(calls, expire_first=True).highlights("MLB1000")) == 4
    assert calls.count("/oauth/token") == 2


def test_connection_diagnostics():
    steps = ml_client([]).test_connection("MLB1000")
    assert all(ok for _, ok, _ in steps) and len(steps) == 4


# --------------------------------------------------------------------------- #
# Configuração
# --------------------------------------------------------------------------- #
def test_ml_secret_is_encrypted_at_rest():
    configure_ml()
    with session_scope() as s:
        row = s.get(AppSetting, "ml_secret_key")
        assert row.is_secret and "segredo" not in row.value
        assert settings_service.get_all(s)["ml_secret_key"] == "segredo"


def test_bot_config_parsing_and_limits():
    cfg = offer_config.parse({"active_hours": "22:00-02:00", "wa_max_per_window": "999", "min_discount": "abc",
                              "categories": "MLB1, xx, mlb2", "unknown": "1"})
    assert cfg.is_active_time(time(23, 0)) and cfg.is_active_time(time(1, 0)) and not cfg.is_active_time(time(12, 0))
    assert cfg.wa_max_per_window == 10 and cfg.min_discount == 20 and cfg.categories == ["MLB1", "MLB2"]
    assert cfg.template.startswith("🆘") and not cfg.credentials_ok


# --------------------------------------------------------------------------- #
# Ciclo, filtros e fila — por bot
# --------------------------------------------------------------------------- #
def test_cycle_filters_and_queues_best():
    configure_ml()
    bot = make_bot()
    result = collector.run_cycle(bot, ml_factory())
    assert not result.errors, result.errors
    posts = _posts(bot)
    # MLB200 bloqueado (capinha), MLB300 sem desconto, USER_PRODUCT ignorado
    assert set(posts) == {"MLB100", "MLB400"}
    assert {p.status for p in posts.values()} == {OfferStatus.QUEUED} and result.queued == 2 and result.new == 2
    with session_scope() as s:
        state = s.get(OfferBot, bot).state
    assert state["last_result"]["ok"] and state["last_run_at"]


def test_manual_mode_and_disabled_bot_do_not_queue():
    configure_ml()
    manual = make_bot("Manual", config={"mode": "manual"})
    off = make_bot("Desligado", active=False)
    collector.run_cycle(manual, ml_factory())
    collector.run_cycle(off, ml_factory())
    assert {p.status for p in _posts(manual).values()} == {OfferStatus.NEW}
    assert {p.status for p in _posts(off).values()} == {OfferStatus.NEW}


def test_two_bots_are_independent():
    """Cada bot tem filtros e fila próprios; o mesmo produto pode sair nos dois."""
    configure_ml()
    tech = make_bot("Tech", telegram=("@tech",))
    casa = make_bot("Casa", telegram=("@casa",), config={"categories": "", "keywords": "air fryer",
                                                         "min_discount": "40"})
    collector.run_cycle(tech, ml_factory())
    collector.run_cycle(casa, ml_factory())
    assert set(_posts(tech)) == {"MLB100", "MLB400"}
    assert set(_posts(casa)) == {"MLB100"}            # desconto mínimo 40%: só o fone (50%)
    with session_scope() as s:
        assert s.scalar(select(func.count(PromoOffer.id))) == 2   # produto guardado uma vez só

    job_tech = posting.claim_next_post(tech)
    job_casa = posting.claim_next_post(casa)
    assert job_tech.targets == [("@tech", "@tech")] and job_casa.targets == [("@casa", "@casa")]
    posting.finish_post(job_tech.post_id, [{"target": "@tech", "message_id": 1}], [])
    # publicar no bot Tech não afeta a fila do bot Casa
    assert _posts(casa)["MLB100"].status == OfferStatus.POSTED  # já reservado pelo claim acima
    assert posting.claim_next_post(tech) is None       # espaçamento entre posts do bot Tech


def test_post_caption_spacing_and_no_repeat():
    configure_ml()
    bot = make_bot()
    collector.run_cycle(bot, ml_factory())
    job = posting.claim_next_post(bot)
    assert re.search(r"🆘 <b>[^<]*\d+% OFF[^<]*</b>", job.caption)
    assert "De: R$159,90 | Por: R$79,90 pix" in job.caption and "<i>Som de qualidade" in job.caption
    posting.finish_post(job.post_id, [{"target": "@canalteste", "message_id": 777}], [])
    assert posting.claim_next_post(bot) is None  # espaçamento entre posts
    collector.run_cycle(bot, ml_factory())       # não recoloca o mesmo produto (não repetir por N dias)
    with session_scope() as s:
        post = s.get(OfferPost, job.post_id)
        assert post.status == OfferStatus.POSTED and post.tg_messages == [{"target": "@canalteste", "message_id": 777}]


def test_failed_post_is_marked_and_bot_template_is_used():
    configure_ml()
    bot = make_bot(config={"template": "OFERTA {titulo} {link}"})
    collector.run_cycle(bot, ml_factory())
    job = posting.claim_next_post(bot)
    assert job.caption.startswith("OFERTA ")
    posting.finish_post(job.post_id, [], ["@canalteste: Chat not found"])
    with session_scope() as s:
        post = s.get(OfferPost, job.post_id)
        assert post.status == OfferStatus.FAILED and "Chat not found" in post.error


def test_affiliate_link_and_manual_queue():
    configure_ml()
    bot = make_bot(config={"mode": "manual"})
    collector.run_cycle(bot, ml_factory())
    with session_scope() as s:
        offer = s.scalar(select(PromoOffer).where(PromoOffer.external_id == "MLB100"))
        collector.set_offer_details(s, offer, "https://meli.la/abc123", "PRECIN", "CUPOM10")
        offer_id = offer.id
    assert collector.queue_post(offer_id, bot)
    job = posting.claim_next_post(bot)
    assert job.link == "https://meli.la/abc123" and "PRECIN" in job.caption and "CUPOM10" in job.caption


def test_bot_without_targets_or_credentials():
    bot = make_bot(telegram=())
    result = collector.run_cycle(bot, ml_factory())
    assert result.errors and "App ID" in result.errors[0]
    configure_ml()
    with session_scope() as s:
        b = s.get(OfferBot, bot)
        _ = list(b.targets)
        cfg = bot_service.config_for(s, b)
    assert not collector.is_cycle_due(b, cfg)  # sem destino, não busca sozinho
    assert posting.claim_next_post(bot) is None


# --------------------------------------------------------------------------- #
# Token do Telegram
# --------------------------------------------------------------------------- #
def _getme(token_id=987654321, ok=True):
    def handler(request):
        if not ok:
            return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
        return httpx.Response(200, json={"ok": True, "result": {"id": token_id, "username": "OfertasTechBot",
                                                                "first_name": "Ofertas Tech"}})
    return httpx.MockTransport(handler)


TOKEN = "987654321:AAH" + "x" * 32


def test_token_validation():
    with pytest.raises(bot_service.BotError, match="inválido"):
        bot_service.telegram_get_me("abc")
    with pytest.raises(bot_service.BotError, match="recusou"):
        bot_service.telegram_get_me(TOKEN, transport=_getme(ok=False))
    me = bot_service.telegram_get_me(TOKEN, transport=_getme())
    assert me.username == "OfertasTechBot"


def test_token_cannot_be_sales_bot_or_duplicated():
    a, b = make_bot("A"), make_bot("B")
    me = bot_service.telegram_get_me(TOKEN, transport=_getme())
    with session_scope() as s:
        bot_service.set_token(s, s.get(OfferBot, a), TOKEN, me)
    with session_scope() as s, pytest.raises(bot_service.BotError, match="já está"):
        bot_service.set_token(s, s.get(OfferBot, b), TOKEN, me)
    object.__setattr__(bot_service.settings_service.settings, "telegram_bot_token", TOKEN)
    try:
        with session_scope() as s, pytest.raises(bot_service.BotError, match="VENDAS"):
            bot_service.set_token(s, s.get(OfferBot, b), TOKEN, me)
    finally:
        object.__setattr__(bot_service.settings_service.settings, "telegram_bot_token", "")
    with session_scope() as s:
        stored = s.get(OfferBot, a)
        assert stored.telegram_username == "OfertasTechBot" and TOKEN not in stored.telegram_token  # criptografado
        assert bot_service.token_of(stored) == TOKEN


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
    old = views.ml_client_factory, views.telegram_get_me
    views.ml_client_factory = ml_factory()
    views.telegram_get_me = lambda token: bot_service.telegram_get_me(token, transport=_getme())
    with TestClient(app) as c:
        tok = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/admin/login").text).group(1)
        c.post("/admin/login", data={"username": "admin", "password": "SenhaForte123", "csrf_token": tok})
        yield c
    views.ml_client_factory, views.telegram_get_me = old


def _tok(c, url):
    return re.search(r'name="csrf_token" value="([^"]+)"', c.get(url).text).group(1)


def test_panel_full_flow(panel):
    c = panel
    # conta do Mercado Livre (módulo)
    r = c.post("/admin/offers/settings", data={"csrf_token": _tok(c, "/admin/offers/settings"),
                                               "ml_app_id": "123456", "ml_secret_key": "segredo"})
    assert "segredo" not in r.text and "Credenciais do Mercado Livre salvas" in r.text
    c.post("/admin/offers/settings", data={"csrf_token": _tok(c, "/admin/offers/settings"), "ml_app_id": "123456"})
    with session_scope() as s:
        assert settings_service.get_all(s)["ml_secret_key"] == "segredo"   # vazio mantém a chave
    r = c.post("/admin/offers/settings/test", data={"csrf_token": _tok(c, "/admin/offers/settings")})
    assert "token obtido" in r.text

    # cria o bot
    r = c.post("/admin/offers/bots/create", data={"csrf_token": _tok(c, "/admin/offers/bots"), "name": "Ofertas Tech"},
               follow_redirects=False)
    bot_url = r.headers["location"].split("?")[0]
    bot_id = int(bot_url.rsplit("/", 1)[1])
    for tab in ["geral", "telegram", "whatsapp", "busca", "post"]:
        assert c.get(f"{bot_url}?tab={tab}").status_code == 200, tab

    # ligar sem destino é bloqueado
    r = c.post(f"{bot_url}/toggle", data={"csrf_token": _tok(c, bot_url)})
    assert "cadastre pelo menos um destino" in r.text

    # token e destinos do Telegram
    r = c.post(f"{bot_url}/token", data={"csrf_token": _tok(c, bot_url), "token": TOKEN})
    assert "@OfertasTechBot" in r.text
    r = c.post(f"{bot_url}/targets/add", data={"csrf_token": _tok(c, bot_url), "target_id": "canal sem arroba"})
    assert "Destino do Telegram inválido" in r.text
    c.post(f"{bot_url}/targets/add", data={"csrf_token": _tok(c, bot_url), "target_id": "@canalteste",
                                           "name": "Canal Teste"})

    # busca, modelo e ligar
    r = c.post(f"{bot_url}/search", data={"csrf_token": _tok(c, bot_url), "min_discount": "25"})
    assert "Marque pelo menos uma loja" in r.text
    c.post(f"{bot_url}/search", data={"csrf_token": _tok(c, bot_url), "mode": "auto", "min_discount": "25",
                                      "src_mercadolivre": "1",
                                      "categories": ["MLB1000"], "keywords": "air fryer", "blocked_words": "capinha",
                                      "active_hours": "08:00-23:00", "max_posts_per_cycle": "3"})
    r = c.post(f"{bot_url}/template", data={"csrf_token": _tok(c, bot_url), "template": "sem link {titulo}"})
    assert "precisa ter o marcador {link}" in r.text
    c.post(f"{bot_url}/template", data={"csrf_token": _tok(c, bot_url), "template": "🔥 *{chamada}*\n{titulo}\n{link}",
                                        "default_headline": "OFERTA", "footer": ""})
    c.post(f"{bot_url}/toggle", data={"csrf_token": _tok(c, bot_url)})
    with session_scope() as s:
        bot = s.get(OfferBot, bot_id)
        cfg = bot_service.config_for(s, bot)
    assert bot.is_active and cfg.min_discount == 25 and cfg.default_headline == "OFERTA"
    assert cfg.template.startswith("🔥") and cfg.categories == ["MLB1000"]

    # buscar agora e lista de ofertas
    r = c.post(f"{bot_url}/run", data={"csrf_token": _tok(c, bot_url)}, follow_redirects=False)
    assert r.headers["location"] == f"/admin/offers?bot={bot_id}"
    page = c.get(f"/admin/offers?bot={bot_id}&status=queued").text
    assert "Fone Bluetooth X" in page and "-50%" in page and "🔥 *OFERTA*" in page

    with session_scope() as s:
        offer_id = s.scalar(select(PromoOffer.id).where(PromoOffer.external_id == "MLB100"))
    base = f"/admin/offers/bots/{bot_id}/offers/{offer_id}"
    c.post(f"{base}/discard", data={"csrf_token": _tok(c, "/admin/offers")})
    assert _posts(bot_id)["MLB100"].status == OfferStatus.DISCARDED
    c.post(f"{base}/queue", data={"csrf_token": _tok(c, "/admin/offers"), "affiliate_url": "javascript:alert(1)"})
    assert _posts(bot_id)["MLB100"].status == OfferStatus.DISCARDED  # link inválido recusado
    c.post(f"{base}/queue", data={"csrf_token": _tok(c, "/admin/offers"), "target": "telegram",
                                  "affiliate_url": "https://meli.la/x", "headline": "PRECIN", "coupon": "DIA10"})
    assert _posts(bot_id)["MLB100"].status == OfferStatus.QUEUED
    with session_scope() as s:
        o = s.get(PromoOffer, offer_id)
        assert (o.affiliate_url, o.headline, o.coupon) == ("https://meli.la/x", "PRECIN", "DIA10")

    # lista e exclusão
    assert "Ofertas Tech" in c.get("/admin/offers/bots").text
    r = c.post(f"{bot_url}/delete", data={"csrf_token": _tok(c, bot_url), "confirm_name": "errado"})
    assert "digite o nome" in r.text
    c.post(f"{bot_url}/delete", data={"csrf_token": _tok(c, bot_url), "confirm_name": "Ofertas Tech"})
    with session_scope() as s:
        assert s.get(OfferBot, bot_id) is None
        assert s.scalar(select(func.count(OfferTarget.id))) == 0 and s.scalar(select(func.count(OfferPost.id))) == 0


def test_seen_chats_can_be_added_with_one_click(panel):
    bot = make_bot(telegram=())
    with session_scope() as s:
        bot_service.record_seen_chat(s, bot, -1001234567890, "Canal Ofertas", "channel", True)
    url = f"/admin/offers/bots/{bot}"
    c = panel
    page = c.get(f"{url}?tab=telegram").text
    assert "Canal Ofertas" in page and "Encontrados pelo bot" in page
    c.post(f"{url}/targets/add", data={"csrf_token": _tok(c, url), "target_id": "-1001234567890",
                                       "name": "Canal Ofertas"})
    page = c.get(f"{url}?tab=telegram").text
    assert "Encontrados pelo bot" not in page and "-1001234567890" in page


def test_offers_page_without_bots_redirects(panel):
    r = panel.get("/admin/offers", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/offers/bots"
