"""Utilidades compartilhadas pelas telas do painel (templates, flash, paginação, auditoria)."""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from math import ceil
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from admin.auth import (
    client_ip,
    get_csrf_token,
)
from config import BASE_DIR, settings
from core.audit import log_event, utcnow
from models import (
    AdminUser,
)
from sales.texts import brl

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
TZ = ZoneInfo(settings.display_timezone)
PER_PAGE = 50
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _fmt_dt(value) -> str:
    return value.astimezone(TZ).strftime("%d/%m/%Y %H:%M") if value else "—"


templates.env.filters["brl"] = lambda v: brl(v) if v is not None else "—"
templates.env.filters["dt"] = _fmt_dt
templates.env.globals["csrf_token"] = get_csrf_token


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def nav_context() -> dict[str, Any]:
    """Dados do menu lateral: identidade do bot de vendas e a lista dos bots de promoção."""
    from core import alert_service, settings_service
    from database import session_scope
    from models import OfferBot

    try:
        with session_scope() as session:
            values = settings_service.get_all(session)
            bots = [
                {"id": b.id, "name": b.name, "on": b.is_active, "username": b.telegram_username}
                for b in session.scalars(select(OfferBot).order_by(OfferBot.name, OfferBot.id))
            ]
        return {
            "nav_sales": {
                "username": settings.telegram_bot_username,
                "online": alert_service.bot_online(values),
            },
            "nav_offers": {
                "online": alert_service.bot_online(values, key=alert_service.OFFERS_HEARTBEAT),
                "bots": bots,
            },
        }
    except Exception:  # noqa: BLE001 - o menu nunca pode derrubar a página
        return {"nav_sales": {"username": settings.telegram_bot_username, "online": False},
                "nav_offers": {"online": False, "bots": []}}


def render(request: Request, template: str, admin: AdminUser | None, **ctx: Any) -> HTMLResponse:
    flashes = request.session.pop("flash", [])
    base = {"admin": admin, "flashes": flashes, "now": utcnow()}
    if admin is not None:
        base.update(nav_context())
    return templates.TemplateResponse(request, template, {**base, **ctx})


def flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", []).append({"kind": kind, "message": message})


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def paginate(db: Session, stmt, page: int) -> tuple[list, dict]:
    page = max(page, 1)
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    items = db.scalars(stmt.offset((page - 1) * PER_PAGE).limit(PER_PAGE)).all()
    return list(items), {"page": page, "pages": max(ceil(total / PER_PAGE), 1), "total": total}


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value[:100]


def parse_price(raw: str) -> Decimal:
    raw = raw.strip().replace("R$", "").replace(" ", "")
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    try:
        value = Decimal(raw).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValueError("Preço inválido. Use o formato 9,90.") from exc
    if value <= 0 or value > Decimal("100000"):
        raise ValueError("O preço deve ser maior que zero e menor que 100.000.")
    return value


def parse_optional_int(raw: str, field: str, minimum: int = 1) -> int | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    if not raw.lstrip("-").isdigit() or int(raw) < minimum:
        raise ValueError(f"{field} inválido.")
    return int(raw)


def audit(db: Session, request: Request, admin: AdminUser, action: str, **details: Any) -> None:
    log_event(db, action, admin_user_id=admin.id, ip_address=client_ip(request), **details)


def valid_affiliate(url: str) -> str | None:
    url = url.strip()
    if not url:
        return ""
    if not url.startswith("https://") or len(url) > 500 or any(c.isspace() for c in url):
        return None
    return url
