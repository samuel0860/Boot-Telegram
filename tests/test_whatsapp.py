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
from models import OfferStatus, PromoOffer, WaStatus
from services import promo_service, settings_service, whatsapp_service
from services.access_service import utcnow
from services.whatsapp_service import EvolutionClient, WhatsAppError

G1 = {"id": "120363000000000001@g.us", "name": "Ofertas VIP"}
G2 = {"id": "120363000000000002@g.us", "name": "Promo Casa"}


# --------------------------------------------------------------------------- #
# Evolution API simulada (somente testes)
# --------------------------------------------------------------------------- #
class FakeEvolution:
    def __init__(self, state="open", fail_media=False, fail_all=False):
        self.state = state
        self.fail_media = fail_media
        self.fail_all = fail_all
        self.sent: list[tuple[str, str, dict]] = []
        self.created = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers.get("apikey") == "chave-teste"
        path = request.url.path
        if path.startswith("/instance/connectionState/"):
            if self.state == "missing":
                return httpx.Response(404, json={"response": {"message": ["The instance does not exist"]}})
            return httpx.Response(200, json={"instance": {"state": self.state}})
        if path == "/instance/create":
            self.created = True
            self.state = "connecting"
            return httpx.Response(201, json={"instance": {"instanceName": "promocoes"}})
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
            self.sent.append((path.split("/")[2], body["number"], body))
            return httpx.Response(201, json={"key": {"id": "MSG"}})
        if path.startswith("/instance/logout/"):
            self.state = "close"
            return httpx.Response(200, json={"status": "SUCCESS"})
        return httpx.Response(404, json={})

    def client(self) -> EvolutionClient:
        return EvolutionClient("http://whatsapp:8080", "chave-teste", transport=httpx.MockTransport(self.handler))


def configure(**over):
    values = {"wa_enabled": "1", "wa_groups": json.dumps([G1, G2]), "wa_warmup_days": "0",
              "wa_warmup_per_day": "5", "wa_max_per_window": "5", "wa_window_hours": "4",
              "wa_max_per_day": "20", "wa_group_delay_seconds": "10", "wa_started_at": "",
              "wa_max_age_hours": "6", "promo_active_hours": "", "promo_footer": "Link de *afiliado*"}
    values.update(over)
    with session_scope() as s:
        settings_service.set_values(s, values)


def make_offer(ext="MLB1", wa=WaStatus.PENDING, queued_ago=timedelta(0), **kw) -> int:
    with session_scope() as s:
        fields = dict(platform="mercadolivre", external_id=ext, title="Fone *Top* ~X~", product_url="https://ml/p/1",
                      image_url="https://http2.mlstatic.com/a.jpg", price=Decimal("79.90"),
                      original_price=Decimal("159.90"), discount_pct=50, free_shipping=True,
                      status=OfferStatus.QUEUED, wa_status=wa, wa_queued_at=utcnow() - queued_ago)
        fields.update(kw)
        o = PromoOffer(**fields)
        s.add(o)
        s.flush()
        return o.id


# --------------------------------------------------------------------------- #
# Formatação e configuração
# --------------------------------------------------------------------------- #
def test_caption_whatsapp_format():
    o = PromoOffer(title="Fone *Top* ~X~", product_url="https://ml/p/1", affiliate_url="https://meli.la/abc",
                   price=Decimal("79.90"), original_price=Decimal("159.90"), discount_pct=50, free_shipping=True)
    c = whatsapp_service.caption_for(o, "Link de *afiliado*")
    assert "*Fone Top X*" in c                      # remove marcação do título
    assert "~R$ 159,90~" in c and "*R$ 79,90*" in c and "-50%" in c
    assert "https://meli.la/abc" in c               # link de afiliado tem prioridade
    assert "_Link de afiliado_" in c


def test_limits_are_clamped():
    cfg = whatsapp_service.wa_config({"wa_max_per_window": "999", "wa_window_hours": "0",
                                      "wa_max_per_day": "999", "wa_group_delay_seconds": "0",
                                      "wa_warmup_per_day": "50",
                                      "wa_groups": json.dumps([G1, {"id": "5511999@s.whatsapp.net", "name": "x"}])})
    assert cfg.max_per_window == 10 and cfg.window_hours == 1 and cfg.max_per_day == 60
    assert cfg.group_delay_seconds == 5 and cfg.warmup_per_day == 10
    assert cfg.groups == [G1]  # contatos individuais são rejeitados: só grupos


def test_plan_phases():
    from datetime import time
    promo = settings_service.promo_config({**settings_service.DEFAULTS, "promo_active_hours": "08:00-23:00"})
    now = utcnow()
    cfg = whatsapp_service.wa_config({"wa_warmup_days": "7", "wa_warmup_per_day": "5",
                                      "wa_started_at": (now - timedelta(days=2)).isoformat()})
    plan = whatsapp_service.current_plan(cfg, promo, now)
    # aquecimento: dia 3, 5 por dia espalhadas em 15h -> 1 a cada 3h
    assert plan.phase == "aquecimento" and plan.warmup_day == 3 and plan.daily_cap == 5
    assert plan.interval_minutes == 180 and plan.window_cap is None

    cfg = whatsapp_service.wa_config({"wa_warmup_days": "7", "wa_started_at": (now - timedelta(days=8)).isoformat(),
                                      "wa_max_per_window": "5", "wa_window_hours": "4", "wa_max_per_day": "20"})
    plan = whatsapp_service.current_plan(cfg, promo, now)
    # normal: 5 a cada 4h -> 1 a cada 48 min
    assert plan.phase == "normal" and plan.window_cap == 5 and plan.window_hours == 4
    assert plan.interval_minutes == 48 and plan.daily_cap == 20


# --------------------------------------------------------------------------- #
# Fila e limites anti-spam
# --------------------------------------------------------------------------- #
def test_claim_send_and_interval():
    configure()
    oid = make_offer()
    job, reason = whatsapp_service.claim_next()
    assert reason == "ok" and job.offer_id == oid and len(job.groups) == 2

    fake, sleeps = FakeEvolution(), []
    sent, errors = whatsapp_service.send_job(job, fake.client(), sleeps.append)
    assert len(sent) == 2 and not errors and sleeps == [10]  # pausa entre grupos
    assert fake.sent[0][0] == "sendMedia" and fake.sent[0][2]["media"].startswith("https://")
    whatsapp_service.finish(job.offer_id, sent, errors)

    make_offer("MLB2")
    job2, reason2 = whatsapp_service.claim_next()
    assert job2 is None and reason2 == "intervalo entre ofertas"


def test_window_limit():
    configure(wa_max_per_window="2", wa_window_hours="4")
    for i in range(2):  # 2 envios nas últimas 4h, espaçados além do intervalo
        make_offer(f"S{i}", wa=WaStatus.SENT, wa_sent_at=utcnow() - timedelta(hours=3 - i))
    make_offer("NEW")
    assert whatsapp_service.claim_next() == (None, "limite da janela")


def test_warmup_daily_cap():
    configure(wa_warmup_days="7", wa_warmup_per_day="5", wa_started_at=utcnow().isoformat(),
              promo_active_hours="")
    day_start = utcnow().astimezone(whatsapp_service.TZ).replace(hour=0, minute=1)
    for i in range(5):
        make_offer(f"W{i}", wa=WaStatus.SENT, wa_sent_at=max(day_start, utcnow() - timedelta(hours=5)))
    make_offer("NEW")
    assert whatsapp_service.claim_next() == (None, "limite diário (aquecimento)")


def test_best_discount_goes_first():
    configure()
    make_offer("LOW", discount_pct=None)
    best = make_offer("HIGH")
    with session_scope() as s:
        s.get(PromoOffer, best).discount_pct = 70
    job, _ = whatsapp_service.claim_next()
    assert job.offer_id == best


def test_stale_offers_expire():
    configure()
    oid = make_offer(queued_ago=timedelta(hours=7))
    job, reason = whatsapp_service.claim_next()
    assert job is None and reason == "fila vazia"
    with session_scope() as s:
        assert s.get(PromoOffer, oid).wa_status == WaStatus.SKIPPED


def test_disabled_or_outside_hours():
    configure(wa_enabled="0")
    make_offer()
    assert whatsapp_service.claim_next() == (None, "desligado")
    now = utcnow().astimezone(whatsapp_service.TZ)
    start = (now + timedelta(hours=2)).strftime("%H:00")
    end = (now + timedelta(hours=3)).strftime("%H:00")
    configure(promo_active_hours=f"{start}-{end}")
    assert whatsapp_service.claim_next() == (None, "fora do horário ativo")


def test_falls_back_to_text_and_records_failure():
    configure()
    make_offer()
    job, _ = whatsapp_service.claim_next()
    fake = FakeEvolution(fail_media=True)
    sent, errors = whatsapp_service.send_job(job, fake.client(), lambda s: None)
    assert len(sent) == 2 and {k for k, _, _ in fake.sent} == {"sendText"}

    make_offer("MLB9", wa=WaStatus.PENDING)
    with session_scope() as s:  # libera o intervalo (48 min) para o teste
        for o in s.scalars(select(PromoOffer).where(PromoOffer.wa_status == WaStatus.SENT)):
            o.wa_sent_at = utcnow() - timedelta(minutes=60)
    job2, _ = whatsapp_service.claim_next()
    sent2, errors2 = whatsapp_service.send_job(job2, FakeEvolution(fail_all=True).client(), lambda s: None)
    whatsapp_service.finish(job2.offer_id, sent2, errors2)
    with session_scope() as s:
        o = s.get(PromoOffer, job2.offer_id)
        assert o.wa_status == WaStatus.FAILED and "falha simulada" in o.wa_error


def test_queueing_offer_also_queues_whatsapp():
    configure()
    with session_scope() as s:
        o = PromoOffer(platform="mercadolivre", external_id="Q1", title="X", product_url="https://ml/p/q",
                       price=Decimal("10"), status=OfferStatus.NEW)
        s.add(o)
        s.flush()
        oid = o.id
    promo_service.queue_offer(oid, None, None, None)
    with session_scope() as s:
        assert s.get(PromoOffer, oid).wa_status == WaStatus.PENDING


def test_client_errors():
    fake = FakeEvolution(state="missing")
    assert fake.client().state("promocoes") == "missing"
    with pytest.raises(WhatsAppError):
        EvolutionClient("http://whatsapp:8080", "")
    down = EvolutionClient("http://whatsapp:8080", "k", transport=httpx.MockTransport(
        lambda r: (_ for _ in ()).throw(httpx.ConnectError("x"))))
    with pytest.raises(WhatsAppError, match="indisponível"):
        down.state("promocoes")


# --------------------------------------------------------------------------- #
# Painel
# --------------------------------------------------------------------------- #
@pytest.fixture
def panel():
    from admin import routes
    from admin.auth import hash_password
    from app import app
    from models import AdminUser

    with session_scope() as s:
        s.add(AdminUser(username="admin", password_hash=hash_password("SenhaForte123"), is_superuser=True))
    fake = FakeEvolution(state="missing")
    old = routes.wa_client_factory
    routes.wa_client_factory = fake.client
    with TestClient(app) as c:
        tok = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/admin/login").text).group(1)
        c.post("/admin/login", data={"username": "admin", "password": "SenhaForte123", "csrf_token": tok})
        yield c, fake
    routes.wa_client_factory = old


def _tok(c, url="/admin/whatsapp"):
    return re.search(r'name="csrf_token" value="([^"]+)"', c.get(url).text).group(1)


def test_panel_connect_select_groups_and_enable(panel):
    c, fake = panel
    assert "Gerar QR Code" in c.get("/admin/whatsapp").text
    r = c.post("/admin/whatsapp/connect", data={"csrf_token": _tok(c)})
    assert fake.created and "data:image/png;base64,AAAA" in r.text and 'http-equiv="refresh"' in r.text

    fake.state = "open"
    page = c.get("/admin/whatsapp").text
    assert "Conectado" in page and "+5511999990000" in page and "Ofertas VIP" in page

    # ativar sem grupos é bloqueado
    r = c.post("/admin/whatsapp/toggle", data={"csrf_token": _tok(c)})
    assert "Selecione pelo menos um grupo" in r.text

    c.post("/admin/whatsapp/groups", data={"csrf_token": _tok(c),
                                           "group_ids": [G1["id"], "5511999@s.whatsapp.net", "inexistente@g.us"]})
    with session_scope() as s:
        assert json.loads(settings_service.get_all(s)["wa_groups"]) == [G1]

    c.post("/admin/whatsapp/settings", data={"csrf_token": _tok(c), "wa_warmup_days": "7", "wa_warmup_per_day": "5",
                                             "wa_max_per_window": "50", "wa_window_hours": "4", "wa_max_per_day": "20",
                                             "wa_group_delay_seconds": "20", "wa_max_age_hours": "3"})
    r = c.post("/admin/whatsapp/toggle", data={"csrf_token": _tok(c)})
    with session_scope() as s:
        cfg = whatsapp_service.wa_config(settings_service.get_all(s))
    assert cfg.enabled and cfg.max_per_window == 10 and cfg.window_hours == 4 and cfg.started_at is not None
    assert "Aquecimento · dia 1 de 7" in r.text  # o aquecimento começa ao ativar

    r = c.post("/admin/whatsapp/test", data={"csrf_token": _tok(c), "group_id": G1["id"]})
    assert "Mensagem de teste enviada" in r.text and fake.sent[-1][1] == G1["id"]


def test_panel_handles_service_down(panel):
    from admin import routes

    c, _ = panel
    routes.wa_client_factory = lambda: EvolutionClient("http://whatsapp:8080", "k", transport=httpx.MockTransport(
        lambda r: (_ for _ in ()).throw(httpx.ConnectError("x"))))
    r = c.get("/admin/whatsapp")
    assert r.status_code == 200 and "indisponível" in r.text
