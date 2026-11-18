"""Administradores no Telegram e infraestrutura de alertas (compartilhado pelos dois módulos).

- Vínculo: o painel gera um código; o admin envia /vincular CÓDIGO a qualquer bot
  (vendas ou ofertas). O ID do Telegram é o mesmo para todos os bots.
- Alertas: cada módulo detecta os próprios problemas (sales/alerts.py e
  offers/alerts.py); aqui fica o "cooldown" para não repetir a mesma mensagem.
- Sinal de vida de cada processo e agendamento do resumo diário.
"""
from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from config import settings
from core import settings_service
from core.audit import log_event, utcnow
from database import session_scope

logger = logging.getLogger(__name__)
TZ = ZoneInfo(settings.display_timezone)
LINK_CODE_TTL = timedelta(minutes=15)
ALERT_COOLDOWN = timedelta(hours=6)
HEARTBEAT_STALE = timedelta(minutes=3)


def _json(raw: str | None, default):
    try:
        value = json.loads(raw) if raw else default
        return value if isinstance(value, type(default)) else default
    except ValueError:
        return default


# --------------------------------------------------------------------------- #
# Administradores vinculados
# --------------------------------------------------------------------------- #
def linked_admin_ids(values: dict[str, str]) -> set[int]:
    return {int(i) for i in _json(values.get("tg_admin_ids"), []) if str(i).lstrip("-").isdigit()}


def admin_ids(values: dict[str, str] | None = None) -> set[int]:
    if values is None:
        with session_scope() as session:
            values = settings_service.get_all(session)
    return set(settings.admin_telegram_ids) | linked_admin_ids(values)


def create_link_code(admin_user_id: int) -> str:
    code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
    with session_scope() as session:
        settings_service.set_values(session, {"tg_link_code": json.dumps({
            "code": code, "admin_user_id": admin_user_id,
            "expires": (utcnow() + LINK_CODE_TTL).isoformat(),
        })}, admin_user_id)
    return code


def current_link_code(values: dict[str, str]) -> dict | None:
    data = _json(values.get("tg_link_code"), {})
    try:
        if data and datetime.fromisoformat(data["expires"]) > utcnow():
            return data
    except (KeyError, ValueError):
        pass
    return None


def redeem_link_code(code: str, telegram_id: int, name: str) -> bool:
    code = (code or "").strip().upper()
    with session_scope() as session:
        values = settings_service.get_all(session)
        data = current_link_code(values)
        if not data or not secrets.compare_digest(data["code"], code):
            return False
        ids = sorted(linked_admin_ids(values) | {telegram_id})
        settings_service.set_values(session, {"tg_admin_ids": json.dumps(ids), "tg_link_code": ""},
                                    data.get("admin_user_id"))
        log_event(session, "telegram_admin_linked", admin_user_id=data.get("admin_user_id"),
                  telegram_id=telegram_id, name=name[:64])
    return True


def unlink_admin(telegram_id: int, admin_user_id: int | None) -> None:
    with session_scope() as session:
        values = settings_service.get_all(session)
        ids = sorted(linked_admin_ids(values) - {telegram_id})
        settings_service.set_values(session, {"tg_admin_ids": json.dumps(ids)}, admin_user_id)
        log_event(session, "telegram_admin_unlinked", admin_user_id=admin_user_id, telegram_id=telegram_id)


# --------------------------------------------------------------------------- #
# Sinal de vida de cada processo
# --------------------------------------------------------------------------- #
SALES_HEARTBEAT = "bot_heartbeat_at"
OFFERS_HEARTBEAT = "offers_heartbeat_at"


def heartbeat(key: str = SALES_HEARTBEAT) -> None:
    with session_scope() as session:
        settings_service.set_values(session, {key: utcnow().isoformat()})


def bot_online(values: dict[str, str], now: datetime | None = None, key: str = SALES_HEARTBEAT) -> bool:
    try:
        last = datetime.fromisoformat(values.get(key) or "")
    except ValueError:
        return False
    return (now or utcnow()) - last < HEARTBEAT_STALE


def sales_bot_id() -> int | None:
    """ID do bot de vendas (é a parte antes do ':' no token) — sem chamar a API."""
    head = (settings.telegram_bot_token or "").split(":", 1)[0]
    return int(head) if head.isdigit() else None


# --------------------------------------------------------------------------- #
# Alertas com cooldown (cada módulo usa o próprio "escopo")
# --------------------------------------------------------------------------- #
def alerts_to_send(problems: list[tuple[str, str]], now: datetime | None = None, scope: str = "sales") -> list[str]:
    """Aplica o cooldown e registra o envio. Problemas resolvidos liberam o alerta de novo.

    `scope` separa os alertas de cada processo (ex.: "sales", "offers:3") para um
    não apagar o controle do outro.
    """
    now = now or utcnow()
    prefix = f"{scope}:"
    with session_scope() as session:
        values = settings_service.get_all(session)
        if values.get("alerts_enabled") != "1":
            return []
        state = _json(values.get("alerts_state"), {})
        sent_at = state.get("sent", {})
        active = {prefix + k for k, _ in problems}
        # resolvidos (deste escopo): esquece; os de outros escopos ficam como estão
        sent_at = {k: v for k, v in sent_at.items() if not k.startswith(prefix) or k in active}
        messages = []
        for key, msg in problems:
            last = sent_at.get(prefix + key)
            if last and now - datetime.fromisoformat(last) < ALERT_COOLDOWN:
                continue
            sent_at[prefix + key] = now.isoformat()
            messages.append(msg)
        state["sent"] = sent_at
        settings_service.set_values(session, {"alerts_state": json.dumps(state)})
    return messages


# --------------------------------------------------------------------------- #
# Resumo diário (agendamento)
# --------------------------------------------------------------------------- #
def summary_due(values: dict[str, str], now: datetime | None = None, scope: str = "sales") -> bool:
    now = (now or utcnow()).astimezone(TZ)
    try:
        hour = int(values.get("daily_summary_hour") or 21)
    except ValueError:
        hour = 21
    if values.get("alerts_enabled") != "1" or not (0 <= hour <= 23) or now.hour < hour:
        return False
    dates = _json(values.get("alerts_state"), {}).get("summary_dates", {})
    return dates.get(scope) != now.date().isoformat()


def mark_summary_sent(now: datetime | None = None, scope: str = "sales") -> None:
    now = (now or utcnow()).astimezone(TZ)
    with session_scope() as session:
        values = settings_service.get_all(session)
        state = _json(values.get("alerts_state"), {})
        state.setdefault("summary_dates", {})[scope] = now.date().isoformat()
        settings_service.set_values(session, {"alerts_state": json.dumps(state)})


def day_start(now: datetime | None = None) -> datetime:
    return (now or utcnow()).astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
