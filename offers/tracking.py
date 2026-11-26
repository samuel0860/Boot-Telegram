"""Links de afiliado MARCADOS (sub_id) para saber qual bot, canal e horário vendem.

A Shopee aceita até 5 sub_ids no link curto de afiliado. O link continua sendo o seu (mesma
comissão); o relatório de conversões da Shopee passa a separar cliques e pedidos por:
  bot<id> · tg/wa · h<hora>   (ex.: bot3, tg, h20)

Só vale para a Shopee e só quando a opção "Medir por grupo e horário" está ligada no bot.
Se a geração falhar, o post sai com o link normal: nunca deixa de postar por causa disso.
"""
from __future__ import annotations

import logging
from datetime import datetime

from core.audit import utcnow
from models import PromoOffer
from offers.config import TZ, BotConfig

logger = logging.getLogger(__name__)
CHANNELS = {"telegram": "tg", "whatsapp": "wa"}

# Pode ser substituída nos testes
shopee_factory = None


def sub_ids(bot_id: int, channel: str, now: datetime | None = None) -> list[str]:
    hour = (now or utcnow()).astimezone(TZ).hour
    return [f"bot{bot_id}", CHANNELS.get(channel, channel)[:10], f"h{hour:02d}"]


def link_for(offer: PromoOffer, cfg: BotConfig, bot_id: int, channel: str, now: datetime | None = None) -> str:
    if not (cfg.track_links and offer.platform == "shopee" and cfg.shopee_credentials_ok):
        return offer.link
    if offer.source == "conversor" and offer.affiliate_url:
        return offer.link            # link que você mesmo colou: não troca
    from offers import collector
    from offers.shopee import ShopeeError

    factory = shopee_factory or collector.default_shopee_factory
    try:
        with factory(cfg) as client:
            return client.short_link(offer.product_url, sub_ids(bot_id, channel, now))
    except ShopeeError as exc:
        logger.warning("Link marcado da Shopee falhou (oferta %s): %s", offer.id, exc)
    except Exception:  # noqa: BLE001
        logger.exception("Link marcado da Shopee falhou (oferta %s)", offer.id)
    return offer.link
