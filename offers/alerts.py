"""Alertas e resumo diário do MÓDULO DE OFERTAS — cada bot de ofertas avisa sobre si mesmo."""
from __future__ import annotations

from datetime import datetime
from html import escape

from sqlalchemy import func, select

from core.alert_service import TZ, day_start
from core.audit import utcnow
from database import session_scope
from models import OfferBot, OfferPost
from offers import bots as bot_service
from offers import collector


def scope(bot_id: int) -> str:
    return f"offers:{bot_id}"


def detect_problems(bot: OfferBot, wa_state: str | None, has_token: bool) -> list[tuple[str, str]]:
    """Problemas atuais do bot. wa_state=None se o WhatsApp não foi verificado."""
    name = escape(bot.name)
    problems: list[tuple[str, str]] = []
    with session_scope() as session:
        cfg = bot_service.config_for(session, bot)
    missing = cfg.missing_credentials()
    if missing:
        problems.append(("ml_credentials", f"🔑 <b>{name}</b>: faltam as credenciais de {' e '.join(missing)} "
                                           "(painel → Ofertas → Lojas e APIs)."))
    last = collector.last_result(bot)
    if last and not last.get("ok", True):
        problems.append(("search_failed", f"⚠️ <b>{name}</b>: falha na busca de ofertas.\n"
                                          f"{escape(str(last.get('summary', '')))[:300]}"))
    if cfg.wa_enabled and wa_state is not None and wa_state != "open":
        problems.append(("wa_disconnected", f"📵 <b>{name}</b>: WhatsApp desconectado — os envios estão parados. "
                                            "Abra o bot no painel e leia o QR Code de novo."))
    if not has_token and any(t.channel.value == "telegram" and t.is_active for t in bot.targets):
        problems.append(("no_token", f"🤖 <b>{name}</b>: tem canais do Telegram, mas nenhum token. "
                                     "Cadastre o token do @BotFather no painel."))
    return problems


def daily_summary(bot: OfferBot, now: datetime | None = None) -> str:
    now = now or utcnow()
    start = day_start(now)
    with session_scope() as session:
        posted = session.scalar(select(func.count()).select_from(OfferPost).where(
            OfferPost.bot_id == bot.id, OfferPost.posted_at >= start))
        wa_sent = session.scalar(select(func.count()).select_from(OfferPost).where(
            OfferPost.bot_id == bot.id, OfferPost.wa_sent_at >= start))
    return (
        f"📊 <b>{escape(bot.name)}</b> — hoje ({now.astimezone(TZ):%d/%m})\n\n"
        f"📢 Ofertas no Telegram: <b>{posted}</b>\n"
        f"💬 Ofertas no WhatsApp: <b>{wa_sent}</b>"
    )
