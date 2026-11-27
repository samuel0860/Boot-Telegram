"""FINANCEIRO: tudo o que a conta ganhou, num lugar só.

Fontes:
- Comissões de afiliado da Shopee (relatório de conversões da API, ver shopee_report.py):
    garantida = pedidos concluídos · pendente = ainda não concluídos · cancelada = não paga.
- Vendas do bot próprio (PIX aprovados, tabela orders).
Mercado Livre, Amazon e TikTok Shop ainda não entregam relatório de comissão pela API: aparecem
como "sem relatório" (o valor fica no painel de afiliado de cada loja).

A geometria do gráfico é calculada aqui (o painel não usa JS de gráfico nem estilo inline).
"""
from __future__ import annotations

import calendar
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import OfferPost, OfferStatus, Order, OrderStatus, Plan, PromoOffer
from offers.config import TZ

ZERO = Decimal("0.00")
PERIODS = {"hoje": "Hoje", "7": "7 dias", "30": "30 dias", "mes": "Este mês", "90": "90 dias"}
W, H, PAD_L, PAD_R, PAD_T, PAD_B = 960, 240, 56, 8, 12, 24


@dataclass
class Seg:
    key: str
    label: str
    value: Decimal
    y: float
    h: float


@dataclass
class Bar:
    day: date
    total: Decimal
    x: float
    w: float
    segs: list[Seg] = field(default_factory=list)
    show_label: bool = False

    @property
    def label(self) -> str:
        return self.day.strftime("%d/%m")


def period_range(key: str, today: date) -> tuple[date, date, str]:
    key = key if key in PERIODS else "30"
    if key == "hoje":
        return today, today, key
    if key == "mes":
        return today.replace(day=1), today, key
    return today - timedelta(days=int(key) - 1), today, key


def _local_day(dt: datetime) -> date:
    return dt.astimezone(TZ).date()


def build(session: Session, rows: list[dict] | None, period: str, now: datetime, *,
          shopee_connected: bool = False, other_stores: dict[str, bool] | None = None) -> dict:
    today = _local_day(now)
    start, end, period = period_range(period, today)
    since = datetime.combine(start, datetime.min.time(), tzinfo=TZ)

    # ---- Shopee (comissões)
    confirmed = pending = cancelled = ZERO
    orders = cancelled_orders = 0
    per_day: dict[date, dict[str, Decimal]] = defaultdict(lambda: {"shopee": ZERO, "vendas": ZERO})
    products: dict[str, list] = defaultdict(lambda: [0, ZERO])
    for r in rows or []:
        if not r.get("ts"):
            continue
        day = datetime.fromtimestamp(r["ts"], TZ).date()
        if not (start <= day <= end):
            continue
        value = Decimal(r["commission"])
        status = r.get("status", "PENDING")
        if status == "CANCELLED":
            cancelled += value
            cancelled_orders += 1
            continue
        orders += 1
        if status == "COMPLETED":
            confirmed += value
        else:
            pending += value
        per_day[day]["shopee"] += value
        name = r.get("item") or "Produto"
        products[name][0] += r.get("qty") or 1
        products[name][1] += value

    # ---- Vendas do bot próprio (PIX aprovados)
    paid = session.execute(
        select(Order.paid_at, Order.amount, Plan.name).join(Plan, Plan.id == Order.plan_id)
        .where(Order.status == OrderStatus.PAID, Order.paid_at >= since)
    ).all()
    sales = ZERO
    sales_count = 0
    plans: Counter = Counter()
    for paid_at, amount, plan in paid:
        day = _local_day(paid_at)
        if day > end:
            continue
        sales += amount
        sales_count += 1
        per_day[day]["vendas"] += amount
        plans[plan] += amount

    # ---- Ofertas mais divulgadas (posts publicados no período)
    published = session.execute(
        select(PromoOffer.title, PromoOffer.platform, func.count())
        .join(OfferPost, OfferPost.offer_id == PromoOffer.id)
        .where(OfferPost.status == OfferStatus.POSTED, OfferPost.posted_at >= since)
        .group_by(PromoOffer.title, PromoOffer.platform).order_by(func.count().desc()).limit(8)
    ).all()

    total = confirmed + pending + sales
    days_in_period = (end - start).days + 1
    forecast = None
    if period == "mes":
        month_days = calendar.monthrange(today.year, today.month)[1]
        forecast = (total / Decimal(days_in_period) * month_days).quantize(Decimal("0.01")) if days_in_period else None

    stores = [{"name": "Shopee", "connected": shopee_connected, "value": confirmed + pending, "orders": orders}]
    for name, linked in (other_stores or {}).items():
        stores.append({"name": name, "connected": linked, "value": None, "orders": None})

    return {
        "period": period, "label": PERIODS[period], "start": start, "end": end,
        "total": total, "confirmed": confirmed, "pending": pending, "cancelled": cancelled,
        "orders": orders, "cancelled_orders": cancelled_orders,
        "sales": sales, "sales_count": sales_count, "forecast": forecast,
        "avg_day": (total / days_in_period).quantize(Decimal("0.01")) if days_in_period else ZERO,
        "products": sorted(((k, v[0], v[1]) for k, v in products.items()), key=lambda p: p[2], reverse=True)[:8],
        "plans": plans.most_common(5),
        "published": published,
        "stores": stores,
        "chart": _chart(per_day, start, end) if days_in_period > 1 else None,
    }


def _nice_max(value: Decimal) -> Decimal:
    for step in (10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000):
        if value <= step:
            return Decimal(step)
    return value


def _chart(per_day: dict, start: date, end: date) -> dict:
    days = (end - start).days + 1
    top = max((v["shopee"] + v["vendas"] for v in per_day.values()), default=ZERO)
    y_max = _nice_max(top) if top > 0 else Decimal(10)
    plot_h = H - PAD_T - PAD_B
    slot = (W - PAD_L - PAD_R) / days
    bar_w = max(min(slot - 2, 26), 2)
    every = 1 if days <= 7 else (5 if days <= 31 else 15)
    bars = []
    for i in range(days):
        d = start + timedelta(days=i)
        vals = per_day.get(d, {"shopee": ZERO, "vendas": ZERO})
        x = PAD_L + i * slot + (slot - bar_w) / 2
        bar = Bar(d, vals["shopee"] + vals["vendas"], round(x, 1), round(bar_w, 1),
                  show_label=(days - 1 - i) % every == 0)
        y = H - PAD_B
        for key, label in (("shopee", "Comissão Shopee"), ("vendas", "Vendas do bot")):
            v = vals[key]
            if v <= 0:
                continue
            h = float(v) * plot_h / float(y_max)
            gap = 2 if bar.segs else 0
            y -= h
            bar.segs.append(Seg(key, label, v, round(y, 1), round(max(h - gap, 1), 1)))
        bars.append(bar)
    ticks = [(round(H - PAD_B - plot_h * f, 1), (y_max * Decimal(str(f))).quantize(Decimal("1"))) for f in (0, .5, 1)]
    return {"bars": bars, "ticks": ticks, "width": W, "height": H, "baseline": H - PAD_B,
            "left": PAD_L, "right": W - PAD_R}
