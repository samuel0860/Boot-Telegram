"""Processo do MÓDULO DE OFERTAS: roda todos os bots de ofertas ao mesmo tempo.

A cada TICK segundos:
- liga/desliga o Telegram de cada bot conforme o painel (token novo, removido ou trocado);
- para cada bot ligado: dispara a busca no Mercado Livre quando estiver na hora,
  publica a próxima oferta da fila nos destinos do Telegram e envia a próxima
  para os grupos do WhatsApp (respeitando o ritmo do número daquele bot);
- grava o sinal de vida, manda alertas e o resumo diário de cada bot.

Bots e destinos são lidos do banco a cada ciclo: nada precisa ser reiniciado
quando você cria, liga, desliga ou altera um bot no painel.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field

from sqlalchemy import select
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import Forbidden, RetryAfter, TelegramError
from telegram.ext import Application

from core import alert_service, error_reporter, settings_service, tenancy
from core.alert_service import OFFERS_HEARTBEAT
from database import session_scope
from models import Account, AccountStatus, OfferBot, TargetChannel
from offers import alerts as offer_alerts
from offers import bots as bot_service
from offers import collector, posting, telegram_bot, whatsapp

logger = logging.getLogger("offers")
TICK = 10
ALERT_EVERY = 300
HEARTBEAT_EVERY = 60


async def run_db(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


# --------------------------------------------------------------------------- #
# Publicação no Telegram
# --------------------------------------------------------------------------- #
async def send_to_target(tg: Bot, target_id: str, job: posting.PostJob) -> int:
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Ver oferta", url=job.link)]])
    chat = posting.channel_target(target_id)
    for attempt in range(2):
        try:
            if job.image_url:
                try:
                    msg = await tg.send_photo(chat, photo=job.image_url, caption=job.caption,
                                              parse_mode=ParseMode.HTML, reply_markup=markup)
                    return msg.message_id
                except TelegramError as exc:
                    text = str(exc).lower()
                    if "chat not found" in text or "rights" in text or isinstance(exc, Forbidden | RetryAfter):
                        raise
            msg = await tg.send_message(chat, job.caption, parse_mode=ParseMode.HTML, reply_markup=markup)
            return msg.message_id
        except RetryAfter as exc:
            if attempt:
                raise
            await asyncio.sleep(float(exc.retry_after) + 1)
    raise TelegramError("falha ao publicar")


async def publish_next(bot_id: int, tg: Bot) -> bool:
    """Publica a próxima oferta da fila do bot em todos os destinos do Telegram."""
    job = await run_db(posting.claim_next_post, bot_id)
    if job is None:
        return False
    sent, errors = [], []
    for target_id, name in job.targets:
        try:
            message_id = await send_to_target(tg, target_id, job)
            sent.append({"target": target_id, "message_id": message_id})
        except TelegramError as exc:
            errors.append(f"{name}: {exc}")
            logger.warning("Bot %s: falha ao publicar a oferta %s em %s: %s", bot_id, job.offer_id, target_id, exc)
    await run_db(posting.finish_post, job.post_id, sent, errors)
    return True


# --------------------------------------------------------------------------- #
# Estado de cada bot no processo
# --------------------------------------------------------------------------- #
@dataclass
class BotRuntime:
    bot_id: int
    account_id: int | None = None
    app: Application | None = None
    token_hash: str = ""
    cycle_running: bool = False
    wa_running: bool = False
    last_alert_check: float = 0.0
    tasks: set = field(default_factory=set)


class OffersRuntime:
    def __init__(self, app_factory=telegram_bot.build_application, wa_client_factory=whatsapp.default_client):
        self.app_factory = app_factory
        self.wa_client_factory = wa_client_factory
        self.bots: dict[int, BotRuntime] = {}
        self.last_heartbeat = 0.0
        self.last_error_flush = 0.0

    # ---- Telegram: um Application (polling) por token
    async def _start_app(self, rt: BotRuntime, token: str) -> None:
        app = self.app_factory(token, rt.bot_id)
        app.bot_data["account_id"] = rt.account_id   # tudo o que este bot fizer vale para a conta dona dele
        await app.initialize()
        await app.start()
        await app.updater.start_polling(allowed_updates=["message", "callback_query", "my_chat_member",
                                                         "channel_post"], drop_pending_updates=True)
        rt.app = app
        logger.info("Bot de ofertas %s conectado ao Telegram como @%s", rt.bot_id, app.bot.username)

    async def _stop_app(self, rt: BotRuntime) -> None:
        app, rt.app = rt.app, None
        if app is None:
            return
        try:
            if app.updater and app.updater.running:
                await app.updater.stop()
            if app.running:
                await app.stop()
            await app.shutdown()
        except Exception:  # noqa: BLE001
            logger.warning("Erro ao desligar o Telegram do bot %s", rt.bot_id, exc_info=True)

    async def sync(self) -> list[tuple[OfferBot, str]]:
        """Lê os bots do banco e liga/desliga o Telegram de cada um."""
        def _load():
            with session_scope() as session:
                # bots de contas suspensas/canceladas param (o Telegram deles também é desligado)
                active = set(session.scalars(select(Account.id).where(Account.status == AccountStatus.ACTIVE)))
                bots = [b for b in bot_service.list_bots(session) if b.account_id in active]
                for b in bots:
                    _ = list(b.targets)  # carrega os destinos antes de fechar a sessão
                return [(b, bot_service.token_of(b)) for b in bots]
        loaded = await run_db(_load)
        seen = set()
        for bot, token in loaded:
            seen.add(bot.id)
            rt = self.bots.setdefault(bot.id, BotRuntime(bot.id, bot.account_id))
            token_hash = hashlib.sha256(token.encode()).hexdigest() if token else ""
            if token_hash != rt.token_hash:
                await self._stop_app(rt)
                rt.token_hash = token_hash
                if token:
                    try:
                        await self._start_app(rt, token)
                    except TelegramError as exc:
                        logger.error("Bot de ofertas %s: token recusado pelo Telegram (%s)", bot.id, exc)
                        rt.token_hash = token_hash  # não tenta de novo até o token mudar
        for bot_id in list(self.bots):
            if bot_id not in seen:  # bot excluído no painel
                await self._stop_app(self.bots.pop(bot_id))
        return loaded

    # ---- tarefas por bot
    def _spawn(self, rt: BotRuntime, coro, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        rt.tasks.add(task)
        task.add_done_callback(rt.tasks.discard)

    async def _cycle(self, rt: BotRuntime) -> None:
        try:
            await run_db(collector.run_cycle, rt.bot_id, triggered_by="bot")
        except Exception:  # noqa: BLE001
            logger.exception("Erro no ciclo de ofertas do bot %s", rt.bot_id)
        finally:
            rt.cycle_running = False

    async def _whatsapp(self, rt: BotRuntime, job: whatsapp.WaJob) -> None:
        try:
            def _work():
                with self.wa_client_factory() as client:
                    return whatsapp.send_job(job, client, time.sleep)
            try:
                sent, errors = await run_db(_work)
            except whatsapp.WhatsAppError as exc:
                sent, errors = [], [str(exc)]
            await run_db(whatsapp.finish, job.post_id, sent, errors)
            if errors:
                logger.warning("Bot %s — WhatsApp: oferta enviada a %d grupo(s), erros: %s",
                               rt.bot_id, len(sent), errors[:3])
        except Exception:  # noqa: BLE001
            logger.exception("Erro no envio para o WhatsApp do bot %s", rt.bot_id)
        finally:
            rt.wa_running = False

    async def _alerts(self, rt: BotRuntime, bot: OfferBot, has_token: bool) -> None:
        def _check():
            with session_scope() as session:
                cfg = bot_service.config_for(session, bot)
                values = settings_service.get_all(session)
            wa_state = None
            if cfg.wa_enabled and bot.wa_instance:
                try:
                    with self.wa_client_factory() as c:
                        wa_state = c.state(bot.wa_instance)
                except whatsapp.WhatsAppError:
                    wa_state = "unavailable"
            scope = offer_alerts.scope(bot.id)
            msgs = alert_service.alerts_to_send(offer_alerts.detect_problems(bot, wa_state, has_token), scope=scope)
            summary = offer_alerts.daily_summary(bot) if alert_service.summary_due(values, scope=scope) else None
            return msgs, summary, alert_service.admin_ids(values)

        msgs, summary, admins = await run_db(_check)
        if rt.app is None or not admins:
            return  # sem token não há por onde avisar (o painel mostra os problemas)
        for admin_id in admins:
            for text in msgs + ([summary] if summary else []):
                try:
                    await rt.app.bot.send_message(admin_id, text, parse_mode=ParseMode.HTML)
                except TelegramError:
                    pass  # o admin ainda não abriu este bot (/start)
        if summary:
            await run_db(alert_service.mark_summary_sent, scope=offer_alerts.scope(bot.id))

    async def tick_bot(self, bot: OfferBot, has_token: bool) -> None:
        rt = self.bots[bot.id]
        if bot.is_active:
            def _cfg():
                with session_scope() as session:
                    return bot_service.config_for(session, bot)
            cfg = await run_db(_cfg)
            if not rt.cycle_running and collector.is_cycle_due(bot, cfg):
                rt.cycle_running = True
                self._spawn(rt, self._cycle(rt), f"ciclo-{bot.id}")
            if rt.app is not None and bot_service.active_targets(bot, TargetChannel.TELEGRAM):
                await publish_next(bot.id, rt.app.bot)
            if not rt.wa_running:
                job, _reason = await run_db(whatsapp.claim_next, bot.id)
                if job is not None:
                    rt.wa_running = True
                    self._spawn(rt, self._whatsapp(rt, job), f"whatsapp-{bot.id}")
        if time.monotonic() - rt.last_alert_check >= ALERT_EVERY:
            rt.last_alert_check = time.monotonic()
            await self._alerts(rt, bot, has_token)

    async def tick(self) -> None:
        loaded = await self.sync()
        for bot, token in loaded:
            try:
                # cada bot trabalha DENTRO da conta dona dele (as tarefas criadas aqui herdam a conta)
                with tenancy.use_account(bot.account_id):
                    await self.tick_bot(bot, bool(token))
            except Exception:  # noqa: BLE001
                logger.exception("Erro no bot de ofertas %s", bot.id)
        now = time.monotonic()
        if now - self.last_heartbeat >= HEARTBEAT_EVERY:
            self.last_heartbeat = now
            await run_db(alert_service.heartbeat, OFFERS_HEARTBEAT)
        if now - self.last_error_flush >= 60:
            self.last_error_flush = now
            any_app = next((rt.app for rt in self.bots.values() if rt.app is not None), None)
            if any_app is not None:
                await error_reporter.flush_async(any_app.bot)

    async def run_forever(self) -> None:
        logger.info("Módulo de ofertas iniciado")
        try:
            while True:
                started = time.monotonic()
                try:
                    await self.tick()
                except Exception:  # noqa: BLE001
                    logger.exception("Erro no ciclo do módulo de ofertas")
                await asyncio.sleep(max(1.0, TICK - (time.monotonic() - started)))
        finally:
            for rt in list(self.bots.values()):
                await self._stop_app(rt)
