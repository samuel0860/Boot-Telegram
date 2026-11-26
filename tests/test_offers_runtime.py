"""Processo do módulo de ofertas: liga/desliga o Telegram de cada bot e publica as filas."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from telegram.error import BadRequest

from core import alert_service, settings_service
from core.settings_service import encrypt
from database import session_scope
from models import OfferBot, OfferPost, OfferStatus, WaStatus
from offers.runtime import OffersRuntime
from tests.fakes import G1, FakeEvolution, make_bot, make_post

TOKEN_A = "111111111:AA" + "a" * 32
TOKEN_B = "222222222:BB" + "b" * 32


class FakeTgBot:
    def __init__(self, token, fail_targets=()):
        self.username = f"bot{token[:3]}"
        self.sent: list[tuple[str, object, str]] = []
        self.fail_targets = set(fail_targets)

    async def send_photo(self, chat, photo, caption, parse_mode, reply_markup):
        if str(chat) in self.fail_targets:
            raise BadRequest("Chat not found")
        self.sent.append(("photo", chat, caption))
        return SimpleNamespace(message_id=len(self.sent))

    async def send_message(self, chat, text, parse_mode=None, reply_markup=None, **kw):
        if str(chat) in self.fail_targets:
            raise BadRequest("Chat not found")
        self.sent.append(("text", chat, text))
        return SimpleNamespace(message_id=len(self.sent))


class FakeApp:
    instances: list = []

    def __init__(self, token, bot_id, fail_targets=()):
        self.token, self.bot_id = token, bot_id
        self.bot_data: dict = {}
        self.bot = FakeTgBot(token, fail_targets)
        self.running = False
        self.updater = SimpleNamespace(running=False, start_polling=self._poll, stop=self._stop_poll)
        FakeApp.instances.append(self)

    async def initialize(self):
        pass

    async def start(self):
        self.running = True

    async def _poll(self, **kw):
        self.updater.running = True

    async def _stop_poll(self):
        self.updater.running = False

    async def stop(self):
        self.running = False

    async def shutdown(self):
        pass


def _set_token(bot_id, token):
    with session_scope() as s:
        b = s.get(OfferBot, bot_id)
        b.telegram_token = encrypt(token) if token else None


def _runtime(fake_wa=None, fail_targets=()):
    FakeApp.instances = []
    wa = fake_wa or FakeEvolution()
    return OffersRuntime(app_factory=lambda t, b: FakeApp(t, b, fail_targets), wa_client_factory=wa.client), wa


def test_sync_starts_and_stops_telegram_per_bot():
    a, b = make_bot("A"), make_bot("B")
    _set_token(a, TOKEN_A)
    rt, _ = _runtime()

    async def scenario():
        await rt.sync()
        assert rt.bots[a].app is not None and rt.bots[b].app is None
        app_a = rt.bots[a].app
        _set_token(b, TOKEN_B)
        _set_token(a, None)                       # token removido no painel
        await rt.sync()
        assert rt.bots[a].app is None and not app_a.running and rt.bots[b].app.token == TOKEN_B
        with session_scope() as s:
            s.delete(s.get(OfferBot, b))          # bot excluído no painel
        await rt.sync()
        assert b not in rt.bots
    asyncio.run(scenario())


def test_tick_publishes_to_all_telegram_targets_and_whatsapp():
    bot = make_bot(telegram=("@canal1", "-1001234567890"), whatsapp=[G1],
                   config={"wa_enabled": "1", "wa_warmup_days": "0", "active_hours": ""})
    _set_token(bot, TOKEN_A)
    _, post_id = make_post(bot)
    rt, wa = _runtime()

    async def scenario():
        await rt.tick()
        await asyncio.gather(*list(rt.bots[bot].tasks))   # espera o envio ao WhatsApp
    asyncio.run(scenario())

    sent = FakeApp.instances[0].bot.sent
    assert [c for _, c, _ in sent] == ["@canal1", -1001234567890]       # ID numérico vira int
    assert all("🆘" in caption for *_, caption in sent)
    assert wa.sent and wa.sent[0][1] == G1["id"] and wa.sent[0][3] == f"ofertas{bot}"
    with session_scope() as s:
        post = s.get(OfferPost, post_id)
        assert post.status == OfferStatus.POSTED and len(post.tg_messages) == 2
        assert post.wa_status == WaStatus.SENT
        assert alert_service.bot_online(settings_service.get_all(s), key=alert_service.OFFERS_HEARTBEAT)


def test_partial_failure_keeps_post_and_records_error():
    bot = make_bot(telegram=("@ok", "@quebrado"))
    _set_token(bot, TOKEN_A)
    _, post_id = make_post(bot, wa=WaStatus.NONE)
    rt, _ = _runtime(fail_targets=("@quebrado",))
    asyncio.run(rt.tick())
    with session_scope() as s:
        post = s.get(OfferPost, post_id)
        assert post.status == OfferStatus.POSTED and "Chat not found" in post.error


def test_inactive_bot_does_not_publish():
    bot = make_bot(active=False)
    _set_token(bot, TOKEN_A)
    _, post_id = make_post(bot, wa=WaStatus.NONE)
    rt, _ = _runtime()
    asyncio.run(rt.tick())
    assert FakeApp.instances[0].bot.sent == []
    with session_scope() as s:
        assert s.get(OfferPost, post_id).status == OfferStatus.QUEUED
