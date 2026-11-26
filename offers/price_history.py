"""Detector de desconto falso.

Guarda o preço de cada oferta (um registro por dia, ou quando o preço muda) e compara o
preço de hoje com o preço "de costume" dos últimos 30 dias. Se a loja diz que deu desconto,
mas o preço está igual ao que já vinha sendo cobrado, o "De" foi inflado: a oferta fica
marcada como suspeita e o bot não a publica sozinho (dá para publicar manualmente).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import PriceHistory, PromoOffer
from offers.post_template import short_brl

WINDOW = timedelta(days=30)        # período usado como "preço de costume"
MIN_AGE = timedelta(days=2)        # ignora os registros mais recentes (o desconto pode ter começado ontem)
MIN_POINTS = 2                     # sem histórico suficiente não dá para julgar
TOLERANCE = Decimal("0.95")        # precisa estar pelo menos 5% abaixo do preço de costume
SAME_POINT = timedelta(hours=20)


def usual_price(session: Session, offer_id: int, now: datetime) -> Decimal | None:
    prices = session.scalars(
        select(PriceHistory.price).where(PriceHistory.offer_id == offer_id,
                                         PriceHistory.seen_at >= now - WINDOW,
                                         PriceHistory.seen_at <= now - MIN_AGE)
    ).all()
    if len(prices) < MIN_POINTS:
        return None
    return Decimal(median(prices))


def check(session: Session, offer: PromoOffer, now: datetime) -> str | None:
    """Motivo, se o desconto parece falso; None se parece real (ou ainda não dá para saber)."""
    if not offer.discount_pct or offer.id is None:
        return None
    usual = usual_price(session, offer.id, now)
    if usual is None or offer.price < usual * TOLERANCE:
        return None
    return (f"Desconto suspeito: nos últimos dias o preço já era R${short_brl(usual)} "
            f"(o “De” parece inflado).")[:200]


def record(session: Session, offer: PromoOffer, now: datetime) -> None:
    last = session.scalar(select(PriceHistory).where(PriceHistory.offer_id == offer.id)
                          .order_by(PriceHistory.seen_at.desc()).limit(1))
    if last and last.price == offer.price and now - last.seen_at < SAME_POINT:
        return
    session.add(PriceHistory(offer_id=offer.id, price=offer.price, original_price=offer.original_price, seen_at=now))
    session.flush()


def update(session: Session, offer: PromoOffer, now: datetime) -> None:
    """Chamado sempre que a oferta é lida na loja: marca/desmarca e registra o preço."""
    offer.price_note = check(session, offer, now)
    record(session, offer, now)
