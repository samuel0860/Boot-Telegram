"""Estatísticas dos bots de promoção (posts por dia, por loja, por bot e por tipo de produto).

A geometria dos gráficos (SVG) é calculada aqui: o painel não usa JavaScript de gráfico
nem estilos inline (CSP), só atributos do SVG.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import OfferBot, OfferPost, OfferStatus, PromoOffer
from offers.config import TZ
from offers.phrases import category_for

STORES = (("mercadolivre", "Mercado Livre"), ("shopee", "Shopee"), ("amazon", "Amazon"), ("tiktok", "TikTok Shop"))
PERIODS = (7, 30, 90)
MAX_TYPES = 8

# Gráfico de colunas (posts por dia)
W, H = 960, 240
PAD_L, PAD_R, PAD_T, PAD_B = 34, 8, 12, 24


@dataclass
class Segment:
    store: str
    label: str
    count: int
    y: float
    h: float


@dataclass
class DayBar:
    day: date
    total: int
    x: float
    w: float
    segments: list[Segment] = field(default_factory=list)
    show_label: bool = False

    @property
    def label(self) -> str:
        return self.day.strftime("%d/%m")


@dataclass
class HBar:
    label: str
    count: int
    width: float           # 0..100 (% do maior)
    store: str = ""


@dataclass
class Stats:
    days: int
    total: int
    wa_total: int
    avg_discount: int | None
    suspicious: int
    bars: list[DayBar]
    ticks: list[tuple[float, int]]
    stores: list[HBar]
    types: list[HBar]
    bots: list[HBar]
    top: list[PromoOffer]
    width: int = W
    height: int = H
    baseline: float = H - PAD_B
    plot_left: int = PAD_L
    plot_right: int = W - PAD_R


def _nice_max(value: int) -> int:
    for step in (4, 8, 12, 20, 40, 60, 100, 200, 400, 1000, 2000, 4000):
        if value <= step:
            return step
    return value


def _hbars(counter: Counter, labels: dict[str, str] | None = None, limit: int | None = None) -> list[HBar]:
    items = counter.most_common()
    if limit and len(items) > limit:
        rest = sum(n for _, n in items[limit - 1:])
        items = items[:limit - 1] + [("Outros", rest)]
    top = max((n for _, n in items), default=0) or 1
    return [HBar(label=(labels or {}).get(k, k), count=n, width=round(n * 100 / top, 1), store=k)
            for k, n in items if n]


def build(session: Session, bot_id: int | None, days: int, now: datetime) -> Stats:
    days = days if days in PERIODS else 30
    local_today = now.astimezone(TZ).date()
    first_day = local_today - timedelta(days=days - 1)
    since = datetime.combine(first_day, datetime.min.time(), tzinfo=TZ)

    stmt = (select(OfferPost, PromoOffer).join(PromoOffer, PromoOffer.id == OfferPost.offer_id)
            .where(OfferPost.status == OfferStatus.POSTED, OfferPost.posted_at >= since))
    wa_stmt = select(OfferPost.wa_sent_at).where(OfferPost.wa_sent_at >= since)
    if bot_id:
        stmt = stmt.where(OfferPost.bot_id == bot_id)
        wa_stmt = wa_stmt.where(OfferPost.bot_id == bot_id)
    rows = session.execute(stmt).all()
    wa_total = len(session.scalars(wa_stmt).all())

    per_day: dict[date, Counter] = defaultdict(Counter)
    stores, types, by_bot = Counter(), Counter(), Counter()
    discounts: list[int] = []
    suspicious = 0
    offers: dict[int, PromoOffer] = {}
    for post, offer in rows:
        per_day[post.posted_at.astimezone(TZ).date()][offer.platform] += 1
        stores[offer.platform] += 1
        types[category_for(offer.title)] += 1
        by_bot[post.bot_id] += 1
        if offer.discount_pct:
            discounts.append(offer.discount_pct)
        if offer.price_note:
            suspicious += 1
        offers[offer.id] = offer

    # colunas empilhadas por loja
    top_day = max((sum(c.values()) for c in per_day.values()), default=0)
    y_max = _nice_max(top_day) if top_day else 4
    plot_h = H - PAD_T - PAD_B
    slot = (W - PAD_L - PAD_R) / days
    bar_w = max(min(slot - 2, 28), 2)
    label_every = 1 if days <= 7 else (5 if days <= 30 else 15)
    bars: list[DayBar] = []
    for i in range(days):
        d = first_day + timedelta(days=i)
        counts = per_day.get(d, Counter())
        x = PAD_L + i * slot + (slot - bar_w) / 2
        bar = DayBar(day=d, total=sum(counts.values()), x=round(x, 1), w=round(bar_w, 1),
                     show_label=(days - 1 - i) % label_every == 0)
        y = H - PAD_B
        for key, label in STORES:
            n = counts.get(key, 0)
            if not n:
                continue
            h = n * plot_h / y_max
            gap = 2 if bar.segments else 0          # 2px entre segmentos empilhados
            y -= h
            bar.segments.append(Segment(key, label, n, round(y, 1), round(max(h - gap, 1), 1)))
        bars.append(bar)
    ticks = [(round(H - PAD_B - plot_h * f, 1), round(y_max * f)) for f in (0, .25, .5, .75, 1)]

    bot_names = {b.id: b.name for b in session.scalars(select(OfferBot)).all()}
    top = sorted(offers.values(), key=lambda o: (o.discount_pct or 0), reverse=True)[:5]
    return Stats(
        days=days, total=len(rows), wa_total=wa_total,
        avg_discount=round(sum(discounts) / len(discounts)) if discounts else None,
        suspicious=suspicious, bars=bars, ticks=ticks,
        stores=_hbars(stores, dict(STORES)), types=_hbars(types, limit=MAX_TYPES),
        bots=_hbars(Counter({bot_names.get(k, f"Bot {k}"): n for k, n in by_bot.items()})),
        top=top,
    )
