"""Envio das ofertas para GRUPOS do WhatsApp via Evolution API (open source).

A Evolution API conecta um número como "aparelho conectado" (igual ao
WhatsApp Web) após a leitura do QR Code. Isso NÃO é a API oficial da Meta:
use um número dedicado e mantenha os limites conservadores — o WhatsApp pode
bloquear números que enviam mensagens em excesso.

Proteções aplicadas aqui:
- envio somente para os grupos selecionados no painel (nunca contatos);
- aquecimento: poucos envios por dia nos primeiros dias do número;
- depois: X ofertas a cada N horas, espalhadas, com teto diário;
- pausa entre grupos de uma mesma oferta;
- horário ativo; ofertas antigas na fila expiram em vez de serem enviadas.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select

from config import settings
from database import session_scope
from models import PromoOffer, WaStatus
from services import settings_service
from services.access_service import log_event, utcnow
from services.texts import brl

logger = logging.getLogger(__name__)
TZ = ZoneInfo(settings.display_timezone)
GROUP_JID_RE = re.compile(r"^[0-9-]{10,40}@g\.us$")


class WhatsAppError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Cliente HTTP da Evolution API
# --------------------------------------------------------------------------- #
class EvolutionClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None, *,
                 timeout: float = 30.0, transport: httpx.BaseTransport | None = None):
        self.base_url = (base_url or settings.evolution_api_url).rstrip("/")
        key = api_key if api_key is not None else settings.evolution_api_key
        if not key:
            raise WhatsAppError("EVOLUTION_API_KEY não configurada no .env.")
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout, transport=transport,
                                  headers={"apikey": key, "Content-Type": "application/json"})

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _call(self, method: str, path: str, **kwargs):
        try:
            r = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise WhatsAppError(f"Serviço do WhatsApp indisponível ({exc.__class__.__name__}). "
                                "Verifique o container telegram_whatsapp.") from exc
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code >= 400:
            msg = data.get("response", {}).get("message") if isinstance(data.get("response"), dict) else None
            msg = msg or data.get("message") or data.get("error") or r.text[:200]
            if isinstance(msg, list):
                msg = "; ".join(map(str, msg))
            raise WhatsAppError(f"HTTP {r.status_code}: {msg}")
        return data

    # ---- instância / conexão
    def state(self, instance: str) -> str:
        """'open' (conectado), 'connecting', 'close' ou 'missing' (instância não criada)."""
        try:
            data = self._call("GET", f"/instance/connectionState/{instance}")
        except WhatsAppError as exc:
            if "404" in str(exc) or "does not exist" in str(exc):
                return "missing"
            raise
        return (data.get("instance") or {}).get("state") or "close"

    def create(self, instance: str) -> dict:
        return self._call("POST", "/instance/create",
                          json={"instanceName": instance, "integration": "WHATSAPP-BAILEYS", "qrcode": True})

    def qr_code(self, instance: str) -> str | None:
        """Retorna a imagem do QR Code (data URL base64) para conectar o número."""
        data = self._call("GET", f"/instance/connect/{instance}")
        b64 = data.get("base64") or (data.get("qrcode") or {}).get("base64")
        return b64 if b64 and b64.startswith("data:image") else None

    def owner_number(self, instance: str) -> str | None:
        try:
            data = self._call("GET", "/instance/fetchInstances", params={"instanceName": instance})
        except WhatsAppError:
            return None
        items = data if isinstance(data, list) else data.get("results") or []
        for it in items:
            owner = it.get("ownerJid") or (it.get("instance") or {}).get("owner")
            if owner:
                return owner.split("@")[0]
        return None

    def logout(self, instance: str) -> None:
        self._call("DELETE", f"/instance/logout/{instance}")

    # ---- grupos e mensagens
    def groups(self, instance: str) -> list[dict]:
        data = self._call("GET", f"/group/fetchAllGroups/{instance}", params={"getParticipants": "false"})
        items = data if isinstance(data, list) else data.get("results") or []
        groups = [{"id": g["id"], "name": (g.get("subject") or g["id"])[:100], "size": g.get("size")}
                  for g in items if str(g.get("id", "")).endswith("@g.us")]
        return sorted(groups, key=lambda g: g["name"].lower())

    def send_image(self, instance: str, to: str, image_url: str, caption: str) -> None:
        self._call("POST", f"/message/sendMedia/{instance}", json={
            "number": to, "mediatype": "image", "mimetype": "image/jpeg",
            "media": image_url, "fileName": "oferta.jpg", "caption": caption,
        })

    def send_text(self, instance: str, to: str, text: str) -> None:
        self._call("POST", f"/message/sendText/{instance}", json={"number": to, "text": text, "linkPreview": True})


# Pode ser substituída nos testes
def default_client() -> EvolutionClient:
    return EvolutionClient()


# --------------------------------------------------------------------------- #
# Configuração
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class WaConfig:
    enabled: bool
    instance: str
    groups: list[dict] = field(default_factory=list)
    # Fase 1 — aquecimento do número
    warmup_days: int = 7
    warmup_per_day: int = 5
    # Fase 2 — ritmo normal: X ofertas a cada N horas, com teto diário
    max_per_window: int = 5
    window_hours: int = 4
    max_per_day: int = 20
    group_delay_seconds: int = 10
    max_age_hours: int = 6
    started_at: datetime | None = None

    @property
    def ready(self) -> bool:
        return self.enabled and bool(self.groups)

    def warmup_day(self, now: datetime) -> int | None:
        """Dia atual do aquecimento (1, 2, ...) ou None se já terminou."""
        if not self.warmup_days:
            return None
        start = self.started_at or now
        day = (now.astimezone(TZ).date() - start.astimezone(TZ).date()).days + 1
        return day if day <= self.warmup_days else None


def _int(v, default, lo, hi):
    try:
        return max(lo, min(hi, int(str(v).strip())))
    except ValueError:
        return default


def parse_groups(raw: str) -> list[dict]:
    try:
        data = json.loads(raw or "[]")
    except ValueError:
        return []
    return [{"id": g["id"], "name": str(g.get("name") or g["id"])[:100]}
            for g in data if isinstance(g, dict) and GROUP_JID_RE.match(str(g.get("id", "")))]


def _dt(raw: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def wa_config(values: dict[str, str]) -> WaConfig:
    instance = re.sub(r"[^a-zA-Z0-9_-]", "", values.get("wa_instance") or "promocoes")[:40] or "promocoes"
    return WaConfig(
        enabled=values.get("wa_enabled") == "1",
        instance=instance,
        groups=parse_groups(values.get("wa_groups", "[]")),
        warmup_days=_int(values.get("wa_warmup_days"), 7, 0, 30),
        warmup_per_day=_int(values.get("wa_warmup_per_day"), 5, 1, 10),
        max_per_window=_int(values.get("wa_max_per_window"), 5, 1, 10),
        window_hours=_int(values.get("wa_window_hours"), 4, 1, 12),
        max_per_day=_int(values.get("wa_max_per_day"), 20, 1, 60),
        group_delay_seconds=_int(values.get("wa_group_delay_seconds"), 10, 5, 120),
        max_age_hours=_int(values.get("wa_max_age_hours"), 6, 1, 48),
        started_at=_dt(values.get("wa_started_at")),
    )


def active_minutes(promo: settings_service.PromoConfig | None) -> int:
    """Duração do horário ativo em minutos (24h se não houver restrição)."""
    if not promo or not promo.active_start or not promo.active_end:
        return 24 * 60
    start = promo.active_start.hour * 60 + promo.active_start.minute
    end = promo.active_end.hour * 60 + promo.active_end.minute
    return (end - start) % (24 * 60) or 24 * 60


@dataclass(frozen=True)
class Plan:
    """Regra em vigor agora (depende da fase)."""
    phase: str                 # "aquecimento" | "normal"
    warmup_day: int | None
    daily_cap: int
    window_hours: int | None   # None no aquecimento
    window_cap: int | None
    interval_minutes: int      # espaçamento entre ofertas (distribui ao longo do período)


def current_plan(cfg: WaConfig, promo: settings_service.PromoConfig | None, now: datetime) -> Plan:
    day = cfg.warmup_day(now)
    if day is not None:
        cap = min(cfg.warmup_per_day, cfg.max_per_day)
        # espalha as ofertas do dia ao longo do horário ativo (ex.: 15h / 5 = 1 a cada 3h)
        interval = max(15, active_minutes(promo) // cap)
        return Plan("aquecimento", day, cap, None, None, interval)
    interval = max(15, cfg.window_hours * 60 // cfg.max_per_window)
    return Plan("normal", None, cfg.max_per_day, cfg.window_hours, cfg.max_per_window, interval)


# --------------------------------------------------------------------------- #
# Fila de envio
# --------------------------------------------------------------------------- #
_WA_MARKUP = re.compile(r"[*_~`]")


def caption_for(offer: PromoOffer, footer: str = "") -> str:
    """Formatação do WhatsApp: *negrito*, _itálico_, ~riscado~. Link vai no texto."""
    title = _WA_MARKUP.sub("", offer.title)[:200]
    lines = [f"🔥 *{title}*", ""]
    off = f"  🔻 *-{offer.discount_pct}%*" if offer.discount_pct else ""
    if offer.original_price and offer.original_price > offer.price:
        lines.append(f"❌ De: ~{brl(offer.original_price)}~")
        lines.append(f"✅ Por: *{brl(offer.price)}*{off}")
    else:
        lines.append(f"✅ *{brl(offer.price)}*{off}")
    if offer.free_shipping:
        lines.append("🚚 Frete grátis")
    lines += ["", f"🛒 Comprar: {offer.link}"]
    if footer:
        lines += ["", f"_{_WA_MARKUP.sub('', footer)}_"]
    return "\n".join(lines)


def mark_pending(session, offer: PromoOffer, values: dict[str, str] | None = None) -> None:
    """Coloca a oferta na fila do WhatsApp (se o envio estiver ligado)."""
    values = values or settings_service.get_all(session)
    if wa_config(values).ready and offer.wa_status in (WaStatus.NONE, WaStatus.FAILED, WaStatus.SKIPPED):
        offer.wa_status = WaStatus.PENDING
        offer.wa_queued_at = utcnow()
        offer.wa_error = None


@dataclass(frozen=True)
class WaJob:
    offer_id: int
    instance: str
    groups: list[dict]
    caption: str
    image_url: str | None
    group_delay_seconds: int


def limits_status(cfg: WaConfig, promo: settings_service.PromoConfig | None = None,
                  now: datetime | None = None) -> dict:
    """Situação dos limites: fase, envios do dia/janela e quando o próximo pode sair."""
    now = now or utcnow()
    plan = current_plan(cfg, promo, now)
    day_start = now.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    sent = PromoOffer.wa_sent_at
    with session_scope() as session:
        last = session.scalar(select(func.max(sent)))
        today = session.scalar(select(func.count()).select_from(PromoOffer).where(sent >= day_start))
        in_window = (session.scalar(select(func.count()).select_from(PromoOffer)
                                    .where(sent >= now - timedelta(hours=plan.window_hours)))
                     if plan.window_hours else None)
        pending = session.scalar(select(func.count()).select_from(PromoOffer)
                                 .where(PromoOffer.wa_status == WaStatus.PENDING))
    next_allowed = last + timedelta(minutes=plan.interval_minutes) if last else None
    return {"plan": plan, "last": last, "today": today, "in_window": in_window, "pending": pending,
            "next_allowed": next_allowed if next_allowed and next_allowed > now else None}


def claim_next() -> tuple[WaJob | None, str]:
    """Pega a próxima oferta para o WhatsApp respeitando TODOS os limites.

    Retorna (job, motivo). job=None quando nada deve ser enviado agora.
    """
    now = utcnow()
    with session_scope() as session:
        values = settings_service.get_all(session)
    cfg = wa_config(values)
    promo = settings_service.promo_config(values)
    if not cfg.ready:
        return None, "desligado"
    if not promo.is_active_time(now.astimezone(TZ).time()):
        return None, "fora do horário ativo"

    st = limits_status(cfg, promo, now)
    plan: Plan = st["plan"]
    if st["today"] >= plan.daily_cap:
        return None, "limite diário" + (" (aquecimento)" if plan.phase == "aquecimento" else "")
    if plan.window_cap is not None and st["in_window"] >= plan.window_cap:
        return None, "limite da janela"
    if st["next_allowed"]:
        return None, "intervalo entre ofertas"

    with session_scope() as session:
        # Expira ofertas antigas na fila (preço pode ter mudado)
        stale = session.scalars(
            select(PromoOffer).where(PromoOffer.wa_status == WaStatus.PENDING,
                                     PromoOffer.wa_queued_at < now - timedelta(hours=cfg.max_age_hours))
            .with_for_update(skip_locked=True)
        ).all()
        for o in stale:
            o.wa_status = WaStatus.SKIPPED
            o.wa_error = f"expirou na fila após {cfg.max_age_hours}h"
        session.flush()  # autoflush está desligado: grava antes de buscar a próxima

        offer = session.scalar(
            select(PromoOffer).where(PromoOffer.wa_status == WaStatus.PENDING)
            .order_by(PromoOffer.discount_pct.desc().nulls_last(), PromoOffer.wa_queued_at)
            .limit(1).with_for_update(skip_locked=True)
        )
        if offer is None:
            return None, "fila vazia"
        offer.wa_status = WaStatus.SENT   # reservado; se falhar vira FAILED
        offer.wa_sent_at = now
        return WaJob(offer.id, cfg.instance, cfg.groups, caption_for(offer, promo.footer),
                     offer.image_url, cfg.group_delay_seconds), "ok"


def finish(offer_id: int, sent_to: list[str], errors: list[str]) -> None:
    with session_scope() as session:
        offer = session.get(PromoOffer, offer_id, with_for_update=True)
        if not sent_to:
            offer.wa_status = WaStatus.FAILED
            offer.wa_sent_at = None
        offer.wa_error = "; ".join(errors)[:2000] if errors else None
        log_event(session, "whatsapp_posted" if sent_to else "whatsapp_failed", offer_id=offer_id,
                  groups=len(sent_to), errors=errors[:5] or None)


def send_job(job: WaJob, client: EvolutionClient, sleep) -> tuple[list[str], list[str]]:
    """Envia a oferta para cada grupo com pausa entre eles. `sleep` é injetável."""
    sent, errors = [], []
    for i, group in enumerate(job.groups):
        if i:
            sleep(job.group_delay_seconds)
        try:
            if job.image_url:
                try:
                    client.send_image(job.instance, group["id"], job.image_url, job.caption)
                except WhatsAppError:
                    client.send_text(job.instance, group["id"], job.caption)
            else:
                client.send_text(job.instance, group["id"], job.caption)
            sent.append(group["id"])
        except WhatsAppError as exc:
            errors.append(f"{group['name']}: {exc}")
            if "Connection Closed" in str(exc) or "not connected" in str(exc).lower():
                break  # número desconectado: não insiste
    return sent, errors
