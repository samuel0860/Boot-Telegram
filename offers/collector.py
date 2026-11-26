"""Coleta de ofertas (Mercado Livre, Shopee e Amazon), POR BOT de ofertas.

Cada bot busca com os próprios filtros (categorias, palavras-chave, desconto...).
O produto fica em promo_offers (compartilhado) e o que o bot faz com ele fica em
offer_posts (um registro por bot):

  run_cycle(bot_id) -> coleta + filtros + grava
                       modo "auto": as melhores vão para a fila (status queued)
                       modo "manual": ficam como "new" esperando aprovação no painel
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.audit import log_event, utcnow
from database import session_scope
from models import OfferBot, OfferPost, OfferStatus, PromoOffer, TargetChannel, WaStatus
from offers import bots as bot_service
from offers.amazon import AmazonAuthError, AmazonClient, AmazonError
from offers.config import BotConfig
from offers.mercadolivre import MercadoLivreClient, MLAuthError, MLError, MLProduct, Product
from offers.shopee import ShopeeAuthError, ShopeeClient, ShopeeError

logger = logging.getLogger(__name__)

PLATFORM = "mercadolivre"
DEFAULT_CATEGORIES = ["MLB1051", "MLB1648", "MLB1000", "MLB5726", "MLB1574", "MLB1276"]
MAX_LOOKUPS_PER_CYCLE = 60

ClientFactory = Callable[[BotConfig], MercadoLivreClient]
ShopeeFactory = Callable[[BotConfig], ShopeeClient]
AmazonFactory = Callable[[BotConfig], AmazonClient]


def default_client_factory(cfg: BotConfig) -> MercadoLivreClient:
    return MercadoLivreClient(cfg.app_id, cfg.secret_key)


def default_shopee_factory(cfg: BotConfig) -> ShopeeClient:
    return ShopeeClient(cfg.shopee_app_id, cfg.shopee_secret)


def default_amazon_factory(cfg: BotConfig) -> AmazonClient:
    return AmazonClient(cfg.amazon_id, cfg.amazon_secret, cfg.amazon_tag, cfg.amazon_version)


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


# --------------------------------------------------------------------------- #
# Coleta e filtros
# --------------------------------------------------------------------------- #
def collect(client: MercadoLivreClient, cfg: BotConfig, result: CycleResult) -> list[MLProduct]:
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
    queues = [list(ids) for _, ids in sources]   # intercala as fontes
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
    result.collected += len(products)
    return list(products.values())


def check_filters(p: MLProduct, cfg: BotConfig) -> str | None:
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


def upsert_offer(session: Session, p: MLProduct, now: datetime) -> tuple[PromoOffer, bool]:
    platform = getattr(p, "platform", PLATFORM) or PLATFORM
    offer = session.scalar(
        select(PromoOffer).where(PromoOffer.platform == platform, PromoOffer.external_id == p.external_id)
        .with_for_update()
    )
    created = offer is None
    if created:
        offer = PromoOffer(platform=platform, external_id=p.external_id)
        session.add(offer)
    if p.affiliate_url and not offer.affiliate_url:
        offer.affiliate_url = p.affiliate_url  # Shopee/Amazon: a API já devolve o link de afiliado da sua conta
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
    return offer, created


def post_for(session: Session, offer_id: int, bot_id: int) -> tuple[OfferPost, bool]:
    """Registro da oferta neste bot (cria se não existir). Retorna (post, criado?)."""
    stmt = select(OfferPost).where(OfferPost.offer_id == offer_id, OfferPost.bot_id == bot_id).with_for_update()
    post = session.scalar(stmt)
    if post is not None:
        return post, False
    post = OfferPost(offer_id=offer_id, bot_id=bot_id, status=OfferStatus.NEW, wa_status=WaStatus.NONE)
    session.add(post)
    session.flush()
    return post, True


def _save(bot_id: int, products: list[MLProduct], cfg: BotConfig, result: CycleResult, *, allow_queue: bool) -> None:
    from offers import whatsapp

    now = utcnow()
    with session_scope() as session:
        seen_posts = []
        for p in products:
            if check_filters(p, cfg):
                continue
            result.eligible += 1
            offer, _ = upsert_offer(session, p, now)
            post, created = post_for(session, offer.id, bot_id)
            if created:
                result.new += 1
            else:
                result.updated += 1
            repost_after = now - timedelta(days=cfg.no_repeat_days)
            if post.status == OfferStatus.POSTED and post.posted_at and post.posted_at < repost_after:
                post.status = OfferStatus.NEW  # já pode ser repostada
            elif post.status == OfferStatus.FAILED:
                post.status = OfferStatus.NEW
            seen_posts.append(post.id)

        if allow_queue and cfg.mode == "auto" and seen_posts:
            best = session.scalars(
                select(OfferPost).join(PromoOffer, PromoOffer.id == OfferPost.offer_id)
                .where(OfferPost.id.in_(seen_posts), OfferPost.status == OfferStatus.NEW)
                .order_by(PromoOffer.discount_pct.desc().nulls_last(), PromoOffer.price)
                .limit(cfg.max_posts_per_cycle)
            ).all()
            bot = session.get(OfferBot, bot_id)
            for post in best:
                post.status = OfferStatus.QUEUED
                post.queued_at = now
                whatsapp.mark_pending(post, bot, cfg)
            result.queued = len(best)


def collect_shopee(client: ShopeeClient, cfg: BotConfig, result: CycleResult) -> list[Product]:
    """Ofertas da Shopee por palavra-chave e/ou categoria (ou as mais vendidas, se nada for informado)."""
    products: dict[str, Product] = {}
    searches: list[tuple[str, dict]] = [(f"shopee:{kw}"[:100], {"keyword": kw}) for kw in cfg.shopee_keywords]
    searches += [(f"shopee:cat {c}", {"category_id": c}) for c in cfg.shopee_categories]
    if not searches:
        searches = [("shopee:mais vendidos", {})]
    for name, kwargs in searches:
        try:
            for p in client.offers(sort=cfg.shopee_sort, limit=20, source=name, **kwargs):
                products.setdefault(p.external_id, p)
        except ShopeeAuthError:
            raise
        except ShopeeError as exc:
            result.errors.append(f"{name}: {exc}")
    return list(products.values())


def collect_amazon(client: AmazonClient, cfg: BotConfig, result: CycleResult) -> list[Product]:
    """Ofertas da Amazon por palavra-chave e/ou categoria (browse node).

    A busca já pede à Amazon só produtos com o desconto mínimo do bot.
    """
    products: dict[str, Product] = {}
    searches: list[tuple[str, dict]] = [(f"amazon:{kw}"[:100], {"keywords": kw}) for kw in cfg.amazon_keywords]
    searches += [(f"amazon:cat {n}", {"browse_node_id": n}) for n in cfg.amazon_nodes]
    if not searches:
        searches = [("amazon:ofertas", {"keywords": "oferta"})]
    for name, kwargs in searches:
        try:
            for p in client.search(sort=cfg.amazon_sort, min_saving=cfg.min_discount, source=name, **kwargs):
                products.setdefault(p.external_id, p)
        except AmazonAuthError:
            raise
        except AmazonError as exc:
            result.errors.append(f"{name}: {exc}")
    return list(products.values())


MISSING_MSG = {
    "Mercado Livre": "Configure o App ID e a Secret Key do Mercado Livre em Ofertas → Lojas e APIs.",
    "Shopee": "Configure o AppID e a Senha da Shopee em Ofertas → Lojas e APIs.",
    "Amazon": "Configure o Credential ID, o Secret e o Partner Tag da Amazon em Ofertas → Lojas e APIs.",
}


def run_cycle(bot_id: int, client_factory: ClientFactory = default_client_factory, *,
              shopee_factory: ShopeeFactory = default_shopee_factory,
              amazon_factory: AmazonFactory = default_amazon_factory, triggered_by: str = "bot") -> CycleResult:
    """Executa um ciclo completo do bot (todas as lojas ligadas nele).

    Nunca lança exceção: erros vão em result.errors.
    """
    result = CycleResult()
    with session_scope() as session:
        bot = session.get(OfferBot, bot_id)
        if bot is None:
            result.errors.append("bot não encontrado")
            return result
        cfg = bot_service.config_for(session, bot)
    for store in cfg.missing_credentials():
        result.errors.append(MISSING_MSG[store])
    if not cfg.any_store:
        result.errors.append("Nenhuma loja ligada neste bot (aba Busca e filtros).")

    products: list[Product] = []
    if cfg.ml_ready:
        try:
            with client_factory(cfg) as client:
                products += collect(client, cfg, result)
        except MLError as exc:
            result.errors.append(f"Mercado Livre: {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Erro na busca do Mercado Livre do bot %s", bot_id)
            result.errors.append(f"Mercado Livre: erro inesperado {exc.__class__.__name__}")
    if cfg.shopee_ready:
        try:
            with shopee_factory(cfg) as client:
                found = collect_shopee(client, cfg, result)
            result.collected += len(found)
            products += found
        except ShopeeError as exc:
            result.errors.append(str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Erro na busca da Shopee do bot %s", bot_id)
            result.errors.append(f"Shopee: erro inesperado {exc.__class__.__name__}")
    if cfg.amazon_ready:
        try:
            with amazon_factory(cfg) as client:
                found = collect_amazon(client, cfg, result)
            result.collected += len(found)
            products += found
        except AmazonError as exc:
            result.errors.append(str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Erro na busca da Amazon do bot %s", bot_id)
            result.errors.append(f"Amazon: erro inesperado {exc.__class__.__name__}")
    if products:
        try:
            # só manda para a fila automaticamente se o bot estiver ligado
            _save(bot_id, products, cfg, result, allow_queue=cfg.enabled)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Erro ao gravar ofertas do bot %s", bot_id)
            result.errors.append(f"erro inesperado: {exc.__class__.__name__}")

    result.finished_at = utcnow().isoformat()
    with session_scope() as session:
        bot_service.update_state(session, bot_id, last_run_at=result.finished_at, last_result={
            "summary": result.summary(), "by": triggered_by, "ok": not result.errors or result.eligible > 0})
        log_event(session, "offers_cycle", bot_id=bot_id, collected=result.collected, eligible=result.eligible,
                  new=result.new, queued=result.queued, errors=result.errors[:5] or None, by=triggered_by)
    logger.info("Ciclo de ofertas do bot %s (%s): %s", bot_id, triggered_by, result.summary())
    return result


def is_cycle_due(bot: OfferBot, cfg: BotConfig, now: datetime | None = None) -> bool:
    has_destination = any(t.is_active for t in bot.targets)
    if not (cfg.enabled and cfg.sources_ready and has_destination):
        return False
    now = now or utcnow()
    if not cfg.active_now(now):
        return False
    last = bot_service.last_run(bot)
    return last is None or now - last >= timedelta(minutes=cfg.interval_minutes)


def last_result(bot: OfferBot) -> dict:
    data = (bot.state or {}).get("last_result") or {}
    if isinstance(data, str):
        try:
            return json.loads(data)
        except ValueError:
            return {}
    return data


# --------------------------------------------------------------------------- #
# Ações do painel / do modo conversor
# --------------------------------------------------------------------------- #
def set_offer_details(session: Session, offer: PromoOffer, affiliate_url: str | None, headline: str | None,
                      coupon: str | None) -> None:
    """None = não altera; "" = limpa. Link de afiliado, chamada e cupom são da oferta (mesma conta)."""
    if affiliate_url is not None:
        offer.affiliate_url = affiliate_url or None
    if headline is not None:
        offer.headline = headline.strip()[:120] or None
    if coupon is not None:
        offer.coupon = coupon.strip()[:40] or None


def queue_post(offer_id: int, bot_id: int, *, telegram: bool = True, whatsapp_too: bool = True,
               admin_id: int | None = None, ip: str | None = None, first: bool = False) -> bool:
    """Coloca a oferta na fila do bot (Telegram e/ou WhatsApp). first=True: fura a fila do Telegram
    (post manual sai antes das ofertas automáticas que já estavam esperando)."""
    from offers import whatsapp

    with session_scope() as session:
        bot = session.get(OfferBot, bot_id)
        if bot is None or session.get(PromoOffer, offer_id) is None:
            return False
        cfg = bot_service.config_for(session, bot)
        post, _ = post_for(session, offer_id, bot_id)
        changed = False
        if telegram and post.status != OfferStatus.QUEUED and bot_service.active_targets(bot, TargetChannel.TELEGRAM):
            post.status = OfferStatus.QUEUED
            post.queued_at = utcnow() - timedelta(days=1) if first else utcnow()
            post.error = None
            changed = True
        if whatsapp_too:
            if post.wa_status not in (WaStatus.PENDING,):
                post.wa_status = WaStatus.NONE
            changed = whatsapp.mark_pending(post, bot, cfg) or changed
        if changed:
            log_event(session, "offer_queued", admin_user_id=admin_id, ip_address=ip, offer_id=offer_id, bot_id=bot_id)
        return changed


def discard_post(offer_id: int, bot_id: int, admin_id: int | None = None, ip: str | None = None) -> bool:
    with session_scope() as session:
        post, _ = post_for(session, offer_id, bot_id)
        if post.status == OfferStatus.DISCARDED:
            return False
        post.status = OfferStatus.DISCARDED
        if post.wa_status == WaStatus.PENDING:
            post.wa_status = WaStatus.SKIPPED
            post.wa_error = "descartada"
        log_event(session, "offer_discarded", admin_user_id=admin_id, ip_address=ip, offer_id=offer_id, bot_id=bot_id)
        return True


def restore_post(offer_id: int, bot_id: int) -> None:
    with session_scope() as session:
        post, _ = post_for(session, offer_id, bot_id)
        if post.status in (OfferStatus.DISCARDED, OfferStatus.FAILED):
            post.status = OfferStatus.NEW


def stats(bot_id: int) -> dict[str, int]:
    with session_scope() as session:
        rows = session.execute(select(OfferPost.status, func.count()).where(OfferPost.bot_id == bot_id)
                               .group_by(OfferPost.status)).all()
        counts = {s.value: 0 for s in OfferStatus}
        counts.update({status.value: n for status, n in rows})
        since = utcnow() - timedelta(hours=24)
        counts["posted_24h"] = session.scalar(
            select(func.count()).select_from(OfferPost).where(
                OfferPost.bot_id == bot_id, OfferPost.posted_at >= since, OfferPost.status == OfferStatus.POSTED))
        counts["wa_24h"] = session.scalar(
            select(func.count()).select_from(OfferPost).where(OfferPost.bot_id == bot_id, OfferPost.wa_sent_at >= since))
        counts["wa_pending"] = session.scalar(
            select(func.count()).select_from(OfferPost).where(OfferPost.bot_id == bot_id,
                                                              OfferPost.wa_status == WaStatus.PENDING))
        return counts
