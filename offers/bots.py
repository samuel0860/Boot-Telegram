"""Cadastro dos bots de ofertas: token do Telegram, número do WhatsApp, destinos e estado."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from core import settings_service
from core.audit import utcnow
from core.settings_service import decrypt, encrypt
from models import OfferBot, OfferTarget, TargetChannel
from offers import config as offer_config

TOKEN_RE = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,64}$")
TG_TARGET_RE = re.compile(r"^(@[A-Za-z][A-Za-z0-9_]{4,31}|-100\d{6,}|-\d{6,})$")
WA_GROUP_RE = re.compile(r"^[0-9-]{10,40}@g\.us$")
MAX_SEEN_CHATS = 30


class BotError(Exception):
    """Mensagem amigável para o painel."""


# --------------------------------------------------------------------------- #
# Leitura
# --------------------------------------------------------------------------- #
def module_credentials(session: Session) -> tuple[str, str]:
    """(App ID, Secret Key) do Mercado Livre."""
    values = settings_service.get_all(session)
    return values.get("ml_app_id", ""), values.get("ml_secret_key", "")


def shopee_credentials(session: Session) -> tuple[str, str]:
    """(AppID, Senha) da API de afiliados da Shopee."""
    values = settings_service.get_all(session)
    return values.get("shopee_app_id", ""), values.get("shopee_secret", "")


def config_for(session: Session, bot: OfferBot) -> offer_config.BotConfig:
    values = settings_service.get_all(session)
    return offer_config.parse(
        bot.config, enabled=bot.is_active,
        **store_kwargs(values))


def store_kwargs(values: dict[str, str]) -> dict[str, str]:
    """Credenciais das três lojas (módulo de ofertas) no formato de offer_config.parse."""
    return {
        "app_id": values.get("ml_app_id", ""), "secret_key": values.get("ml_secret_key", ""),
        "shopee_app_id": values.get("shopee_app_id", ""), "shopee_secret": values.get("shopee_secret", ""),
        "amazon_id": values.get("amazon_credential_id", ""), "amazon_secret": values.get("amazon_secret", ""),
        "amazon_tag": values.get("amazon_partner_tag", ""), "amazon_version": values.get("amazon_version", "3.1"),
    }


def token_of(bot: OfferBot) -> str:
    return decrypt(bot.telegram_token) if bot.telegram_token else ""


def active_targets(bot: OfferBot, channel: TargetChannel) -> list[OfferTarget]:
    return [t for t in bot.targets if t.channel == channel and t.is_active]


def list_bots(session: Session) -> list[OfferBot]:
    return list(session.scalars(select(OfferBot).order_by(OfferBot.id)).all())


# --------------------------------------------------------------------------- #
# Criação e token
# --------------------------------------------------------------------------- #
def create_bot(session: Session, name: str) -> OfferBot:
    name = (name or "").strip()[:80]
    if not name:
        raise BotError("Dê um nome ao bot (ex.: Ofertas Tech).")
    bot = OfferBot(name=name, is_active=False, config={}, state={})
    session.add(bot)
    session.flush()
    bot.wa_instance = f"ofertas{bot.id}"  # cada bot conecta o próprio número
    return bot


@dataclass(frozen=True)
class TelegramIdentity:
    id: int
    username: str
    name: str


def telegram_get_me(token: str, transport: httpx.BaseTransport | None = None) -> TelegramIdentity:
    """Confere o token direto na API do Telegram (getMe)."""
    if not TOKEN_RE.match(token):
        raise BotError("Token inválido. Copie o token inteiro que o @BotFather enviou (formato 123456:ABC...).")
    try:
        with httpx.Client(timeout=10, transport=transport) as client:
            r = client.get(f"https://api.telegram.org/bot{token}/getMe")
    except httpx.HTTPError as exc:
        raise BotError("Não consegui falar com o Telegram agora. Tente de novo em instantes.") from exc
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or not data.get("ok"):
        raise BotError("O Telegram recusou esse token. Confira no @BotFather (/mybots → API Token).")
    me = data["result"]
    return TelegramIdentity(int(me["id"]), me.get("username") or "", me.get("first_name") or "")


def set_token(session: Session, bot: OfferBot, token: str, me: TelegramIdentity) -> None:
    from core import alert_service

    if alert_service.sales_bot_id() == me.id:
        raise BotError("Esse é o token do bot de VENDAS. Crie um bot novo no @BotFather só para ofertas.")
    other = session.scalar(select(OfferBot).where(OfferBot.telegram_id == me.id, OfferBot.id != bot.id))
    if other:
        raise BotError(f"Esse token já está no bot de ofertas “{other.name}”.")
    bot.telegram_token = encrypt(token)
    bot.telegram_id = me.id
    bot.telegram_username = me.username


def clear_token(bot: OfferBot) -> None:
    bot.telegram_token = None
    bot.telegram_id = None
    bot.telegram_username = None


# --------------------------------------------------------------------------- #
# Destinos
# --------------------------------------------------------------------------- #
def add_target(session: Session, bot: OfferBot, channel: TargetChannel, target_id: str, name: str) -> OfferTarget:
    target_id = (target_id or "").strip()
    if channel == TargetChannel.TELEGRAM and not TG_TARGET_RE.match(target_id):
        raise BotError("Destino do Telegram inválido: use @nomedocanal ou o ID numérico (ex.: -1001234567890).")
    if channel == TargetChannel.WHATSAPP and not WA_GROUP_RE.match(target_id):
        raise BotError("Grupo do WhatsApp inválido.")
    existing = next((t for t in bot.targets if t.channel == channel and t.target_id == target_id), None)
    if existing:
        existing.is_active = True
        existing.name = (name or existing.name)[:120]
        return existing
    target = OfferTarget(channel=channel, target_id=target_id, name=(name or target_id).strip()[:120])
    bot.targets.append(target)
    session.flush()
    return target


def set_whatsapp_groups(session: Session, bot: OfferBot, groups: list[dict]) -> int:
    """Substitui os grupos de WhatsApp do bot pelos selecionados."""
    wanted = {g["id"]: g.get("name") or g["id"] for g in groups if WA_GROUP_RE.match(str(g.get("id", "")))}
    for t in list(bot.targets):
        if t.channel == TargetChannel.WHATSAPP and t.target_id not in wanted:
            bot.targets.remove(t)
    for gid, gname in wanted.items():
        add_target(session, bot, TargetChannel.WHATSAPP, gid, gname)
    return len(wanted)


# --------------------------------------------------------------------------- #
# Estado (JSON) — sempre com a linha bloqueada para não perder atualizações
# --------------------------------------------------------------------------- #
def update_state(session: Session, bot_id: int, **values) -> dict:
    bot = session.get(OfferBot, bot_id, with_for_update=True)
    if bot is None:
        return {}
    state = dict(bot.state or {})
    state.update(values)
    bot.state = state
    return state


def update_config(bot: OfferBot, values: dict[str, str]) -> None:
    cfg = dict(bot.config or {})
    for key, value in values.items():
        if key in offer_config.DEFAULT_CONFIG:
            cfg[key] = str(value)
    bot.config = cfg


def record_seen_chat(session: Session, bot_id: int, chat_id: int, title: str, kind: str, can_post: bool) -> None:
    """Guarda canais/grupos em que o bot foi adicionado (o painel oferece adicionar com 1 clique)."""
    bot = session.get(OfferBot, bot_id, with_for_update=True)
    if bot is None:
        return
    state = dict(bot.state or {})
    seen = [c for c in state.get("seen_chats", []) if c.get("id") != chat_id]
    seen.insert(0, {"id": chat_id, "title": (title or str(chat_id))[:120], "type": kind, "can_post": can_post,
                    "at": utcnow().isoformat()})
    state["seen_chats"] = seen[:MAX_SEEN_CHATS]
    bot.state = state


def last_run(bot: OfferBot) -> datetime | None:
    try:
        raw = (bot.state or {}).get("last_run_at")
        return datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None
