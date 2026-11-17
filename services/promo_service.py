"""Bot de promoções: coleta ofertas na API do Mercado Livre, filtra, evita
repetição e coloca na fila para o bot do Telegram publicar no canal.

Fluxo:
  run_cycle()  -> coleta + filtros + grava em promo_offers
                  modo "auto": as melhores vão para a fila (status queued)
                  modo "manual": ficam como "new" esperando aprovação no painel
  claim_next_post() / finish_post() -> usados pelo bot para publicar a fila
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from config import settings
from database import session_scope
from models import OfferStatus, PromoOffer
from services import settings_service, whatsapp_service
from services.access_service import log_event, utcnow
from services.mercadolivre import MercadoLivreClient, MLAuthError, MLError, MLProduct
from services.settings_service import PromoConfig
from services.texts import brl

logger = logging.getLogger(__name__)

PLATFORM = "mercadolivre"
DEFAULT_CATEGORIES = ["MLB1051", "MLB1648", "MLB1000", "MLB5726", "MLB1574", "MLB1276"]
MAX_LOOKUPS_PER_CYCLE = 60
TZ = ZoneInfo(settings.display_timezone)

ClientFactory = Callable[[PromoConfig], MercadoLivreClient]


def default_client_factory(cfg: PromoConfig) -> MercadoLivreClient:
    return MercadoLivreClient(cfg.app_id, cfg.secret_key)


# --------------------------------------------------------------------------- #
# Coleta e filtros
# --------------------------------------------------------------------------- #
@dataclass
class CycleResult:
    collected: int = 0
    eligible: int = 0
    new: int = 0
    updated: int = 0
    queued: int = 0
    errors: list[str] = field(default_factory=list)
    finished_at: str = ""

    def summary(self) -> str:
        s = (f"{self.collected} produtos lidos, {self.eligible} dentro dos filtros, "
             f"{self.new} novos, {self.queued} enviados para a fila")
        if self.errors:
            s += f" — {len(self.errors)} aviso(s): " + "; ".join(self.errors[:3])
        return s


def collect(client: MercadoLivreClient, cfg: PromoConfig, result: CycleResult) -> list[MLProduct]:
    sources: list[tuple[str, list[tuple[str, str]]]] = []
    categories = cfg.categories or ([] if cfg.keywords else DEFAULT_CATEGORIES)
    for cat in categories:
        try:
            sources.append((f"mais-vendidos:{cat}", client.highlights(cat)))
        except MLAuthError:
            raise
        except MLError as exc:
            result.errors.append(f"categoria {cat}: {exc}")
    for kw in cfg.keywords:
        try:
            sources.append((f"busca:{kw}"[:100], [(pid, "PRODUCT") for pid in client.search_products(kw)]))
        except MLAuthError:
            raise
        except MLError as exc:
            result.errors.append(f"busca '{kw}': {exc}")

    products: dict[str, MLProduct] = {}
    lookups = 0
    # intercala as fontes para não gastar todo o limite numa única categoria
    queues = [list(ids) for _, ids in sources]
    names = [name for name, _ in sources]
    while lookups < MAX_LOOKUPS_PER_CYCLE and any(queues):
        for idx, queue in enumerate(queues):
            if not queue or lookups >= MAX_LOOKUPS_PER_CYCLE:
                continue
            ext_id, kind = queue.pop(0)
            if ext_id in products:
                continue
            lookups += 1
            try:
                if kind == "PRODUCT":
                    p = client.product(ext_id, source=names[idx])
                elif kind == "ITEM":
                    p = client.item(ext_id, source=names[idx])
                else:
                    p = None
            except MLAuthError:
                raise
            except MLError as exc:
                result.errors.append(f"{ext_id}: {exc}")
                continue
            if p:
                products[p.external_id] = p
    result.collected = len(products)
    return list(products.values())


def check_filters(p: MLProduct, cfg: PromoConfig) -> str | None:
    """Retorna o motivo da recusa, ou None se a oferta passou."""
    discount = p.discount_pct or 0
    if discount < cfg.min_discount:
        return f"desconto {discount}% < {cfg.min_discount}%"
    if cfg.min_price and p.price < cfg.min_price:
        return "preço abaixo do mínimo"
    if cfg.max_price and p.price > cfg.max_price:
        return "preço acima do máximo"
    title = p.title.lower()
    for word in cfg.blocked_words:
        if word in title:
            return f"palavra bloqueada: {word}"
    return None


def _save_offers(products: list[MLProduct], cfg: PromoConfig, result: CycleResult, *, allow_queue: bool) -> None:
    now = utcnow()
    with session_scope() as session:
        seen_ids = []
        for p in products:
            if check_filters(p, cfg):
                continue
            result.eligible += 1
            offer = session.scalar(
                select(PromoOffer).where(PromoOffer.platform == PLATFORM, PromoOffer.external_id == p.external_id)
                .with_for_update()
            )
            if offer is None:
                offer = PromoOffer(platform=PLATFORM, external_id=p.external_id, status=OfferStatus.NEW)
                session.add(offer)
                result.new += 1
            else:
                result.updated += 1
                repost_after = now - timedelta(days=cfg.no_repeat_days)
                if offer.status == OfferStatus.POSTED and offer.posted_at and offer.posted_at < repost_after:
                    offer.status = OfferStatus.NEW  # já pode ser repostada
                elif offer.status == OfferStatus.FAILED:
                    offer.status = OfferStatus.NEW
            offer.title = p.title
            offer.product_url = p.url
            offer.image_url = p.image
            offer.price = p.price
            offer.original_price = p.original_price
            offer.discount_pct = p.discount_pct
            offer.free_shipping = p.free_shipping
            offer.category_id = p.category_id
            offer.source = p.source
            offer.last_seen_at = now
            session.flush()
            seen_ids.append(offer.id)

        if allow_queue and cfg.mode == "auto" and seen_ids:
            best = session.scalars(
                select(PromoOffer)
                .where(PromoOffer.id.in_(seen_ids), PromoOffer.status == OfferStatus.NEW)
                .order_by(PromoOffer.discount_pct.desc().nulls_last(), PromoOffer.price)
                .limit(cfg.max_posts_per_cycle)
            ).all()
            values = settings_service.get_all(session)
            for offer in best:
                offer.status = OfferStatus.QUEUED
                offer.queued_at = now
                whatsapp_service.mark_pending(session, offer, values)
            result.queued = len(best)


def run_cycle(client_factory: ClientFactory = default_client_factory, *, triggered_by: str = "bot") -> CycleResult:
    """Executa um ciclo completo. Nunca lança exceção: erros vão em result.errors."""
    result = CycleResult()
    with session_scope() as session:
        cfg = settings_service.load_promo_config(session)
    if not cfg.credentials_ok:
        result.errors.append("Configure o App ID e a Secret Key do Mercado Livre no painel.")
    else:
        try:
            with client_factory(cfg) as client:
                products = collect(client, cfg, result)
            # Só manda para a fila automaticamente se o bot estiver ligado.
            _save_offers(products, cfg, result, allow_queue=cfg.enabled)
        except MLError as exc:
            result.errors.append(str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Erro no ciclo de promoções")
            result.errors.append(f"erro inesperado: {exc.__class__.__name__}")

    result.finished_at = utcnow().isoformat()
    with session_scope() as session:
        settings_service.set_values(session, {
            "promo_last_run_at": result.finished_at,
            "promo_last_result": json.dumps({"summary": result.summary(), "by": triggered_by,
                                             "ok": not result.errors or result.eligible > 0}),
        })
        log_event(session, "promo_cycle", collected=result.collected, eligible=result.eligible,
                  new=result.new, queued=result.queued, errors=result.errors[:5] or None, by=triggered_by)
    logger.info("Ciclo de promoções (%s): %s", triggered_by, result.summary())
    return result


def is_cycle_due(values: dict[str, str], cfg: PromoConfig, now: datetime | None = None) -> bool:
    if not (cfg.enabled and cfg.credentials_ok and cfg.channel_id):
        return False
    now = now or utcnow()
    if not cfg.is_active_time(now.astimezone(TZ).time()):
        return False
    last = values.get("promo_last_run_at")
    if not last:
        return True
    try:
        return now - datetime.fromisoformat(last) >= timedelta(minutes=cfg.interval_minutes)
    except ValueError:
        return True


def last_result(values: dict[str, str]) -> dict:
    try:
        return json.loads(values.get("promo_last_result") or "{}")
    except ValueError:
        return {}


# --------------------------------------------------------------------------- #
# Ações do painel
# --------------------------------------------------------------------------- #
def queue_offer(offer_id: int, affiliate_url: str | None, admin_id: int | None, ip: str | None) -> bool:
    with session_scope() as session:
        offer = session.get(PromoOffer, offer_id, with_for_update=True)
        if offer is None or offer.status == OfferStatus.QUEUED:
            return False
        if affiliate_url is not None:
            offer.affiliate_url = affiliate_url or None
        offer.status = OfferStatus.QUEUED
        offer.queued_at = utcnow()
        offer.error = None
        whatsapp_service.mark_pending(session, offer)
        log_event(session, "promo_offer_queued", admin_user_id=admin_id, ip_address=ip, offer_id=offer.id)
        return True


def set_affiliate(offer_id: int, affiliate_url: str | None) -> None:
    with session_scope() as session:
        offer = session.get(PromoOffer, offer_id, with_for_update=True)
        if offer:
            offer.affiliate_url = affiliate_url or None


def discard_offer(offer_id: int, admin_id: int | None, ip: str | None) -> bool:
    with session_scope() as session:
        offer = session.get(PromoOffer, offer_id, with_for_update=True)
        if offer is None or offer.status == OfferStatus.DISCARDED:
            return False
        offer.status = OfferStatus.DISCARDED
        log_event(session, "promo_offer_discarded", admin_user_id=admin_id, ip_address=ip, offer_id=offer.id)
        return True


def restore_offer(offer_id: int) -> None:
    with session_scope() as session:
        offer = session.get(PromoOffer, offer_id, with_for_update=True)
        if offer and offer.status in (OfferStatus.DISCARDED, OfferStatus.FAILED):
            offer.status = OfferStatus.NEW


# --------------------------------------------------------------------------- #
# Publicação (usado pelo bot)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PostJob:
    offer_id: int
    channel_id: str
    caption: str
    image_url: str | None
    link: str


def caption_for(offer: PromoOffer, footer: str = "") -> str:
    lines = [f"🔥 <b>{escape(offer.title[:200])}</b>", ""]
    discount = f"  🔻 <b>-{offer.discount_pct}%</b>" if offer.discount_pct else ""
    if offer.original_price and offer.original_price > offer.price:
        lines.append(f"❌ De: <s>{brl(offer.original_price)}</s>")
        lines.append(f"✅ Por: <b>{brl(offer.price)}</b>{discount}")
    else:
        lines.append(f"✅ <b>{brl(offer.price)}</b>{discount}")
    if offer.free_shipping:
        lines.append("🚚 Frete grátis")
    lines += ["", "💛 Mercado Livre"]
    if footer:
        lines += ["", f"<i>{escape(footer)}</i>"]
    return "\n".join(lines)[:1024]


def claim_next_post() -> PostJob | None:
    """Pega a próxima oferta da fila respeitando o espaçamento entre posts."""
    with session_scope() as session:
        cfg = settings_service.load_promo_config(session)
        if not cfg.channel_id:
            return None
        last_post = session.scalar(select(func.max(PromoOffer.posted_at)))
        if last_post and utcnow() - last_post < timedelta(seconds=cfg.spacing_seconds):
            return None
        offer = session.scalar(
            select(PromoOffer).where(PromoOffer.status == OfferStatus.QUEUED)
            .order_by(PromoOffer.queued_at).limit(1).with_for_update(skip_locked=True)
        )
        if offer is None:
            return None
        # marca já como publicada (evita post duplicado); em caso de erro vira "failed"
        offer.status = OfferStatus.POSTED
        offer.posted_at = utcnow()
        offer.telegram_chat_id = cfg.channel_id
        return PostJob(offer.id, cfg.channel_id, caption_for(offer, cfg.footer), offer.image_url, offer.link)


def finish_post(offer_id: int, message_id: int | None, error: str | None) -> None:
    with session_scope() as session:
        offer = session.get(PromoOffer, offer_id, with_for_update=True)
        if error:
            offer.status = OfferStatus.FAILED
            offer.error = error[:2000]
            offer.posted_at = None
            log_event(session, "promo_post_failed", offer_id=offer.id, error=error[:300])
        else:
            offer.telegram_message_id = message_id
            offer.error = None
            log_event(session, "promo_posted", offer_id=offer.id, channel=offer.telegram_chat_id,
                      discount=offer.discount_pct, price=str(offer.price))


def channel_target(channel_id: str) -> int | str:
    channel_id = channel_id.strip()
    return int(channel_id) if channel_id.lstrip("-").isdigit() else channel_id


def stats() -> dict[str, int]:
    with session_scope() as session:
        rows = session.execute(select(PromoOffer.status, func.count()).group_by(PromoOffer.status)).all()
        counts = {s.value: 0 for s in OfferStatus}
        counts.update({status.value: n for status, n in rows})
        since = utcnow() - timedelta(hours=24)
        counts["posted_24h"] = session.scalar(
            select(func.count()).select_from(PromoOffer).where(PromoOffer.posted_at >= since,
                                                               PromoOffer.status == OfferStatus.POSTED)
        )
        return counts

