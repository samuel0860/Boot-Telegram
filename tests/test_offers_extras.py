"""Central dos bots, pausar/retomar todos, copiar configuração, estatísticas e detector de desconto falso."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select

from core.audit import utcnow
from database import session_scope
from models import OfferBot, OfferPost, OfferStatus, PriceHistory, PromoOffer
from offers import bots as bot_service
from offers import collector, price_history
from offers.phrases import category_for
from tests.fakes import make_bot
from tests.test_offers_shopee import FakeShopee, _tok, configure_shopee, panel  # noqa: F401

SHOPEE_ONLY = {"src_shopee": "1", "src_mercadolivre": "0", "min_discount": "20"}


def _air_fryer_post(bot_id: int) -> OfferPost:
    with session_scope() as s:
        post = s.scalar(select(OfferPost).join(PromoOffer, PromoOffer.id == OfferPost.offer_id)
                        .where(OfferPost.bot_id == bot_id, PromoOffer.title == "Air Fryer Mondial 4L"))
        s.expunge(post)
        return post


def test_price_history_one_point_per_day_or_change():
    with session_scope() as s:
        offer = PromoOffer(platform="shopee", external_id="9", title="X", product_url="https://x", price=Decimal("10"))
        s.add(offer)
        s.flush()
        now = utcnow()
        price_history.record(s, offer, now)
        price_history.record(s, offer, now + timedelta(hours=1))     # mesmo preço, mesmo dia: ignora
        offer.price = Decimal("9")
        price_history.record(s, offer, now + timedelta(hours=2))     # mudou: registra
        assert s.scalar(select(func.count()).select_from(PriceHistory)) == 2


def test_fake_discount_is_not_auto_posted():
    configure_shopee()
    first = make_bot("Primeiro", config=SHOPEE_ONLY)
    collector.run_cycle(first, shopee_factory=FakeShopee().factory())
    assert _air_fryer_post(first).status == OfferStatus.QUEUED       # sem histórico: não dá para julgar

    # a loja já cobrava R$199,90 há dias, mas agora diz "50% OFF" sobre R$399,80
    with session_scope() as s:
        offer = s.scalar(select(PromoOffer).where(PromoOffer.title == "Air Fryer Mondial 4L"))
        for d in (5, 10):
            s.add(PriceHistory(offer_id=offer.id, price=Decimal("199.90"), seen_at=utcnow() - timedelta(days=d)))
    second = make_bot("Segundo", config=SHOPEE_ONLY)
    result = collector.run_cycle(second, shopee_factory=FakeShopee().factory())
    assert result.suspicious == 1 and "desconto suspeito" in result.summary()
    assert _air_fryer_post(second).status == OfferStatus.NEW          # fica na lista, não vai para a fila
    with session_scope() as s:
        assert "Desconto suspeito" in s.scalar(select(PromoOffer.price_note)
                                               .where(PromoOffer.title == "Air Fryer Mondial 4L"))

    # com o detector desligado, publica normalmente
    third = make_bot("Terceiro", config={**SHOPEE_ONLY, "skip_fake": "0"})
    collector.run_cycle(third, shopee_factory=FakeShopee().factory())
    assert _air_fryer_post(third).status == OfferStatus.QUEUED


def test_real_discount_passes():
    with session_scope() as s:
        offer = PromoOffer(platform="shopee", external_id="7", title="Y", product_url="https://y",
                           price=Decimal("100"), original_price=Decimal("200"), discount_pct=50)
        s.add(offer)
        s.flush()
        for d in (4, 8, 12):
            s.add(PriceHistory(offer_id=offer.id, price=Decimal("180"), seen_at=utcnow() - timedelta(days=d)))
        s.flush()
        assert price_history.check(s, offer, utcnow()) is None       # caiu de verdade (180 -> 100)


def test_category_for_titles():
    assert category_for("Chinelo Slide Nike Victori One") == "Calçados"
    assert category_for("Air Fryer Mondial 4L") == "Cozinha"
    assert category_for("Coisa sem tipo") == "Outros"


def test_central_pause_and_resume_all(panel):  # noqa: F811
    c = panel
    on = make_bot("Ligado", telegram=("@canal1",))
    off = make_bot("Desligado", active=False, telegram=("@canal2",))
    page = c.get("/admin/offers/bots").text
    assert "Central dos bots de promoção" in page and "Token do Telegram cadastrado" in page
    assert "Configuração incompleta" in page and "Pausar todos" in page

    c.post("/admin/offers/bots/pause-all", data={"csrf_token": _tok(c, "/admin/offers/bots")})
    with session_scope() as s:
        assert not s.get(OfferBot, on).is_active and s.get(OfferBot, on).state.get("paused_by_all")
    assert "Retomar os que eu pausei" in c.get("/admin/offers/bots").text
    c.post("/admin/offers/bots/resume-all", data={"csrf_token": _tok(c, "/admin/offers/bots")})
    with session_scope() as s:
        assert s.get(OfferBot, on).is_active and not s.get(OfferBot, off).is_active   # só religa quem estava ligado
        assert "paused_by_all" not in s.get(OfferBot, on).state

    r = c.post(f"/admin/offers/bots/{off}/toggle", data={"csrf_token": _tok(c, "/admin/offers/bots"),
                                                         "back": "central"}, follow_redirects=False)
    assert r.headers["location"] == "/admin/offers/bots"


def test_copy_config_between_bots(panel):  # noqa: F811
    c = panel
    src = make_bot("Modelo", config={"min_discount": "33", "template": "🔥 {titulo}\n{link}", "wa_max_per_day": "9"})
    dst = make_bot("Novo")
    url = f"/admin/offers/bots/{dst}"
    assert "Copiar configuração de outro bot" in c.get(url).text
    r = c.post(f"{url}/copy", data={"csrf_token": _tok(c, url), "source_id": src, "groups": ["busca", "post"]})
    assert "Copiado de “Modelo”: Busca e filtros, Modelo do post" in r.text
    with session_scope() as s:
        cfg = bot_service.config_for(s, s.get(OfferBot, dst))
        bot = s.get(OfferBot, dst)
    assert cfg.min_discount == 33 and cfg.template.startswith("🔥") and cfg.wa_max_per_day == 20
    assert not bot.telegram_token


def test_stats_page(panel):  # noqa: F811
    c = panel
    configure_shopee()
    bot = make_bot("Stats", config=SHOPEE_ONLY)
    collector.run_cycle(bot, shopee_factory=FakeShopee().factory())
    with session_scope() as s:
        for post in s.scalars(select(OfferPost).where(OfferPost.bot_id == bot)):
            post.status, post.posted_at = OfferStatus.POSTED, utcnow()
    page = c.get("/admin/offers/stats?days=7").text
    assert "Posts por dia" in page and "s-shopee" in page and "Cozinha" in page
    assert "Air Fryer Mondial 4L" in page and "Ver em tabela" in page
    assert "Stats" in c.get(f"/admin/offers/stats?bot={bot}&days=90").text


def test_offer_card_has_phone_preview(panel):  # noqa: F811
    c = panel
    configure_shopee()
    bot = make_bot("Prévia", config=SHOPEE_ONLY)
    collector.run_cycle(bot, shopee_factory=FakeShopee().factory())
    page = c.get(f"/admin/offers?bot={bot}&status=all").text
    assert "Ver como fica no grupo" in page and 'class="phone-text"' in page and "<b>" in page
    assert "Chamada, cupom e link" in page
