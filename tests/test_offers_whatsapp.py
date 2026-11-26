"""MÓDULO DE OFERTAS — WhatsApp: cada bot com o próprio número, grupos e ritmo de envio."""
from __future__ import annotations

import re
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from core.audit import utcnow
from database import session_scope
from models import OfferBot, OfferPost, OfferStatus, TargetChannel, WaStatus
from offers import bots as bot_service
from offers import collector, whatsapp
from offers.config import parse
from offers.posting import whatsapp_caption
from offers.whatsapp import EvolutionClient, WhatsAppError
from tests.fakes import G1, G2, FakeEvolution, make_bot, make_post

WA_ON = {"wa_enabled": "1", "wa_warmup_days": "0", "active_hours": ""}


def _wa(bot_id: int):
    with session_scope() as s:
        bot = s.get(OfferBot, bot_id)
        _ = list(bot.targets)
        return whatsapp.wa_config(bot, bot_service.config_for(s, bot))


# --------------------------------------------------------------------------- #
# Formatação e configuração
# --------------------------------------------------------------------------- #
def test_caption_whatsapp_format():
    from decimal import Decimal

    from models import PromoOffer

    o = PromoOffer(title="Fone *Top* ~X~", product_url="https://ml/p/1", affiliate_url="https://meli.la/abc",
                   price=Decimal("79.90"), original_price=Decimal("159.90"), discount_pct=50, free_shipping=True,
                   headline="QUEIMA *TOTAL*", coupon="FONE10")
    c = whatsapp_caption(o, parse({}))
    assert c.startswith("🆘 *QUEIMA TOTAL*")      # marcação removida dos valores
    assert "Fone Top X" in c and "Por: R$79,90 pix 👑" in c
    assert "⚠️ cupom: *FONE10*" in c and "Link: https://meli.la/abc" in c


def test_each_bot_has_its_own_number_and_groups():
    a = make_bot("A", whatsapp=[G1], config=WA_ON)
    b = make_bot("B", whatsapp=[G2], config=WA_ON)
    wa_a, wa_b = _wa(a), _wa(b)
    assert wa_a.instance != wa_b.instance and wa_a.ready and wa_b.ready
    assert wa_a.groups == [G1] and wa_b.groups == [G2]


def test_plan_phases():
    cfg = parse({"active_hours": "08:00-23:00"})
    now = utcnow()
    wa = whatsapp.WaConfig(enabled=True, instance="x", warmup_days=7, warmup_per_day=5,
                           started_at=now - timedelta(days=2))
    plan = whatsapp.current_plan(wa, cfg, now)
    assert plan.phase == "aquecimento" and plan.warmup_day == 3 and plan.daily_cap == 5 and plan.interval_minutes == 180
    wa = whatsapp.WaConfig(enabled=True, instance="x", warmup_days=7, started_at=now - timedelta(days=8),
                           max_per_window=5, window_hours=4, max_per_day=20)
    plan = whatsapp.current_plan(wa, cfg, now)
    assert plan.phase == "normal" and plan.window_cap == 5 and plan.interval_minutes == 48 and plan.daily_cap == 20


# --------------------------------------------------------------------------- #
# Fila e limites — por bot
# --------------------------------------------------------------------------- #
def test_claim_send_and_interval():
    bot = make_bot(whatsapp=[G1, G2], config=WA_ON)
    _, post_id = make_post(bot)
    job, reason = whatsapp.claim_next(bot)
    assert reason == "ok" and job.post_id == post_id and len(job.groups) == 2 and job.instance == f"ofertas{bot}"

    fake, sleeps = FakeEvolution(), []
    sent, errors = whatsapp.send_job(job, fake.client(), sleeps.append)
    assert len(sent) == 2 and not errors and sleeps == [10]  # pausa entre grupos
    assert fake.sent[0][0] == "sendMedia" and fake.sent[0][3] == f"ofertas{bot}"
    whatsapp.finish(job.post_id, sent, errors)

    make_post(bot, "MLB2")
    assert whatsapp.claim_next(bot) == (None, "intervalo entre ofertas")
    with session_scope() as s:  # o aquecimento começa no primeiro envio
        assert s.get(OfferBot, bot).state.get("wa_started_at")


def test_limits_are_per_bot():
    """O envio de um bot não consome o limite do outro (números diferentes)."""
    a = make_bot("A", whatsapp=[G1], config=WA_ON)
    b = make_bot("B", whatsapp=[G2], config=WA_ON)
    make_post(a, "A1")
    make_post(b, "B1")
    job_a, _ = whatsapp.claim_next(a)
    whatsapp.finish(job_a.post_id, [G1["id"]], [])
    assert whatsapp.claim_next(a) == (None, "intervalo entre ofertas")   # o número A está no intervalo...
    job_b, reason = whatsapp.claim_next(b)                                # ...mas o B não
    assert reason == "ok" and job_b.groups == [G2]


def test_window_limit_and_warmup_cap():
    bot = make_bot(whatsapp=[G1], config={**WA_ON, "wa_max_per_window": "2", "wa_window_hours": "4"})
    for i in range(2):
        make_post(bot, f"S{i}", wa=WaStatus.SENT, wa_sent_at=utcnow() - timedelta(hours=3 - i))
    make_post(bot, "NEW")
    assert whatsapp.claim_next(bot) == (None, "limite da janela")

    warm = make_bot("W", whatsapp=[G1], config={**WA_ON, "wa_warmup_days": "7", "wa_warmup_per_day": "5"},
                    state={"wa_started_at": utcnow().isoformat()})
    day_start = utcnow().astimezone(whatsapp.TZ).replace(hour=0, minute=1)
    for i in range(5):
        make_post(warm, f"W{i}", wa=WaStatus.SENT, wa_sent_at=max(day_start, utcnow() - timedelta(hours=5)))
    make_post(warm, "WNEW")
    assert whatsapp.claim_next(warm) == (None, "limite diário (aquecimento)")


def test_best_discount_first_and_stale_expire():
    bot = make_bot(whatsapp=[G1], config=WA_ON)
    make_post(bot, "LOW", discount_pct=None)
    _, best = make_post(bot, "HIGH", discount_pct=70)
    _, stale = make_post(bot, "OLD", discount_pct=90, queued_ago=timedelta(hours=7))
    job, _ = whatsapp.claim_next(bot)
    assert job.post_id == best
    with session_scope() as s:
        assert s.get(OfferPost, stale).wa_status == WaStatus.SKIPPED


def test_disabled_inactive_or_outside_hours():
    off = make_bot("off", whatsapp=[G1], config={**WA_ON, "wa_enabled": "0"})
    make_post(off)
    assert whatsapp.claim_next(off) == (None, "desligado")
    inactive = make_bot("inactive", active=False, whatsapp=[G1], config=WA_ON)
    assert whatsapp.claim_next(inactive) == (None, "desligado")
    now = utcnow().astimezone(whatsapp.TZ)
    hours = f"{(now + timedelta(hours=2)):%H}:00-{(now + timedelta(hours=3)):%H}:00"
    late = make_bot("late", whatsapp=[G1], config={**WA_ON, "active_hours": hours})
    make_post(late, "L1")
    assert whatsapp.claim_next(late) == (None, "fora do horário ativo")


def test_falls_back_to_text_and_records_failure():
    bot = make_bot(whatsapp=[G1, G2], config=WA_ON)
    make_post(bot)
    job, _ = whatsapp.claim_next(bot)
    fake = FakeEvolution(fail_media=True)
    sent, _ = whatsapp.send_job(job, fake.client(), lambda s: None)
    assert len(sent) == 2 and {k for k, *_ in fake.sent} == {"sendText"}
    sent2, errors2 = whatsapp.send_job(job, FakeEvolution(fail_all=True).client(), lambda s: None)
    whatsapp.finish(job.post_id, sent2, errors2)
    with session_scope() as s:
        p = s.get(OfferPost, job.post_id)
        assert p.wa_status == WaStatus.FAILED and "falha simulada" in p.wa_error


def test_queue_post_marks_whatsapp_only_when_ready():
    bot = make_bot(whatsapp=[G1], config=WA_ON)
    offer_id, post_id = make_post(bot, status=OfferStatus.NEW, wa=WaStatus.NONE)
    assert collector.queue_post(offer_id, bot, telegram=False, whatsapp_too=True)
    with session_scope() as s:
        p = s.get(OfferPost, post_id)
        assert p.wa_status == WaStatus.PENDING and p.status == OfferStatus.NEW   # só WhatsApp

    no_wa = make_bot("sem wa")
    offer2, post2 = make_post(no_wa, "X2", status=OfferStatus.NEW, wa=WaStatus.NONE)
    collector.queue_post(offer2, no_wa)
    with session_scope() as s:
        p = s.get(OfferPost, post2)
        assert p.wa_status == WaStatus.NONE and p.status == OfferStatus.QUEUED


def test_client_errors():
    assert FakeEvolution(state="missing").client().state("x") == "missing"
    with pytest.raises(WhatsAppError):
        EvolutionClient("http://whatsapp:8080", "")
    down = EvolutionClient("http://whatsapp:8080", "k", transport=httpx.MockTransport(
        lambda r: (_ for _ in ()).throw(httpx.ConnectError("x"))))
    with pytest.raises(WhatsAppError, match="indisponível"):
        down.state("x")


# --------------------------------------------------------------------------- #
# Painel (aba WhatsApp do bot)
# --------------------------------------------------------------------------- #
@pytest.fixture
def panel():
    from admin.auth import hash_password
    from admin.views import offers as views
    from app import app
    from models import AdminUser

    with session_scope() as s:
        s.add(AdminUser(username="admin", password_hash=hash_password("SenhaForte123"), is_superuser=True))
    fake = FakeEvolution(state="missing")
    old = views.wa_client_factory
    views.wa_client_factory = fake.client
    with TestClient(app) as c:
        tok = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/admin/login").text).group(1)
        c.post("/admin/login", data={"username": "admin", "password": "SenhaForte123", "csrf_token": tok})
        yield c, fake
    views.wa_client_factory = old


def _tok(c, url):
    return re.search(r'name="csrf_token" value="([^"]+)"', c.get(url).text).group(1)


def test_panel_connect_select_groups_and_enable(panel):
    c, fake = panel
    bot = make_bot(active=False, telegram=())
    url = f"/admin/offers/bots/{bot}"
    assert "Gerar QR Code" in c.get(f"{url}?tab=whatsapp").text
    r = c.post(f"{url}/whatsapp/connect", data={"csrf_token": _tok(c, url)})
    assert fake.created == [f"ofertas{bot}"]                       # instância própria do bot
    assert "data:image/png;base64,AAAA" in r.text and 'http-equiv="refresh"' in r.text

    fake.state = "open"
    page = c.get(f"{url}?tab=whatsapp").text
    assert "+5511999990000" in page and "Ofertas VIP" in page

    r = c.post(f"{url}/whatsapp/toggle", data={"csrf_token": _tok(c, url)})
    assert "Selecione pelo menos um grupo" in r.text
    c.post(f"{url}/whatsapp/groups", data={"csrf_token": _tok(c, url),
                                           "group_ids": [G1["id"], "5511999@s.whatsapp.net", "inexistente@g.us"]})
    assert _wa(bot).groups == [G1]

    c.post(f"{url}/whatsapp/settings", data={"csrf_token": _tok(c, url), "wa_warmup_days": "7",
                                             "wa_warmup_per_day": "5", "wa_max_per_window": "50",
                                             "wa_window_hours": "4", "wa_max_per_day": "20",
                                             "wa_group_delay_seconds": "20", "wa_max_age_hours": "3"})
    r = c.post(f"{url}/whatsapp/toggle", data={"csrf_token": _tok(c, url)})
    assert "LIGAR o bot" in r.text                                 # bot ainda desligado
    c.post(f"{url}/toggle", data={"csrf_token": _tok(c, url)})
    wa = _wa(bot)
    assert wa.ready and wa.max_per_window == 10 and wa.started_at is not None

    r = c.post(f"{url}/whatsapp/test", data={"csrf_token": _tok(c, url), "group_id": G1["id"]})
    assert "Mensagem de teste enviada" in r.text and fake.sent[-1][1] == G1["id"]

    with session_scope() as s:
        assert all(t.channel == TargetChannel.WHATSAPP for t in s.get(OfferBot, bot).targets)


def test_panel_handles_service_down(panel):
    from admin.views import offers as views

    c, _ = panel
    bot = make_bot()
    views.wa_client_factory = lambda: EvolutionClient("http://whatsapp:8080", "k", transport=httpx.MockTransport(
        lambda r: (_ for _ in ()).throw(httpx.ConnectError("x"))))
    r = c.get(f"/admin/offers/bots/{bot}?tab=whatsapp")
    assert r.status_code == 200 and "indisponível" in r.text
