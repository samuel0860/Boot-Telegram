"""Publicação das ofertas nos canais/grupos do TELEGRAM de cada bot de ofertas."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func, select

from core.audit import log_event, utcnow
from database import session_scope
from models import OfferBot, OfferPost, OfferStatus, PromoOffer, TargetChannel
from offers import bots as bot_service
from offers import post_template
from offers.config import BotConfig

STORE_LABELS = {"mercadolivre": "Mercado Livre", "shopee": "Shopee"}


def post_data(offer: PromoOffer) -> post_template.PostData:
    return post_template.PostData(
        title=offer.title, price=offer.price, link=offer.link, original_price=offer.original_price,
        discount_pct=offer.discount_pct, free_shipping=offer.free_shipping,
        headline=offer.headline, coupon=offer.coupon, store=STORE_LABELS.get(offer.platform or "", ""),
    )


def telegram_caption(offer: PromoOffer, cfg: BotConfig) -> str:
    """Legenda (HTML) a partir do modelo do bot."""
    return post_template.render_telegram(post_data(offer), cfg.template, cfg.default_headline, cfg.footer)


def whatsapp_caption(offer: PromoOffer, cfg: BotConfig) -> str:
    return post_template.render_whatsapp(post_data(offer), cfg.template, cfg.default_headline, cfg.footer)


@dataclass(frozen=True)
class PostJob:
    post_id: int
    offer_id: int
    bot_id: int
    targets: list[tuple[str, str]] = field(default_factory=list)   # (target_id, nome)
    caption: str = ""
    image_url: str | None = None
    link: str = ""


def claim_next_post(bot_id: int) -> PostJob | None:
    """Próxima oferta da fila do bot, respeitando o intervalo entre posts e o horário."""
    now = utcnow()
    with session_scope() as session:
        bot = session.get(OfferBot, bot_id)
        if bot is None or not bot.is_active:
            return None
        cfg = bot_service.config_for(session, bot)
        targets = [(t.target_id, t.name) for t in bot_service.active_targets(bot, TargetChannel.TELEGRAM)]
        if not targets or not cfg.active_now(now):
            return None
        last_post = session.scalar(select(func.max(OfferPost.posted_at)).where(OfferPost.bot_id == bot_id))
        if last_post and now - last_post < timedelta(seconds=cfg.spacing_seconds):
            return None
        post = session.scalar(
            select(OfferPost).where(OfferPost.bot_id == bot_id, OfferPost.status == OfferStatus.QUEUED)
            .order_by(OfferPost.queued_at).limit(1).with_for_update(skip_locked=True)
        )
        if post is None:
            return None
        # marca já como publicada (evita post duplicado); em caso de erro vira "failed"
        post.status = OfferStatus.POSTED
        post.posted_at = now
        offer = session.get(PromoOffer, post.offer_id)
        return PostJob(post.id, offer.id, bot_id, targets, telegram_caption(offer, cfg), offer.image_url, offer.link)


def finish_post(post_id: int, sent: list[dict], errors: list[str]) -> None:
    """sent = [{"target": ..., "message_id": ...}] — se nenhum destino recebeu, a oferta vira "falha"."""
    with session_scope() as session:
        post = session.get(OfferPost, post_id, with_for_update=True)
        if post is None:
            return
        post.tg_messages = sent or None
        post.error = "; ".join(errors)[:2000] if errors else None
        if not sent:
            post.status = OfferStatus.FAILED
            post.posted_at = None
            log_event(session, "offer_post_failed", offer_id=post.offer_id, bot_id=post.bot_id,
                      error=(errors[0] if errors else "")[:300])
        else:
            log_event(session, "offer_posted", offer_id=post.offer_id, bot_id=post.bot_id, targets=len(sent),
                      errors=errors[:3] or None)


def channel_target(target_id: str) -> int | str:
    target_id = target_id.strip()
    return int(target_id) if target_id.lstrip("-").isdigit() else target_id
