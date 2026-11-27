"""Resultado na Shopee (pedidos e comissão dos SEUS links), lido pela API de afiliados.

- A API da Shopee informa pedidos e comissão, mas NÃO informa cliques (esses só no app).
- "Comissão estimada" = comissão dos pedidos que não foram cancelados (pendentes e concluídos),
  como no app da Shopee. A comissão só é paga depois que o pedido é concluído.
- A leitura fica guardada por 15 minutos (por conta) para o painel não consultar a Shopee toda hora.
- Com "Medir por grupo e horário" ligado no bot, o sub_id do link (bot3-tg-h20) separa o resultado
  por bot, canal e horário.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from core import settings_service
from core.audit import utcnow
from offers.config import TZ
from offers.shopee import ShopeeClient, ShopeeError

CACHE_KEY = "shopee_report"
TTL = timedelta(minutes=15)
WINDOW_DAYS = 90                    # a API aceita até ~90 dias
MAX_ROWS = 20000
STATUS_LABELS = {"PENDING": "Pendente", "COMPLETED": "Concluído", "CANCELLED": "Cancelado", "UNPAID": "Não pago"}
SUB_RE = re.compile(r"bot(\d+)", re.I)


def _dec(value) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except InvalidOperation:
        return Decimal("0")


def to_rows(nodes: list[dict]) -> list[dict]:
    """Um registro por PEDIDO: data, status, comissão, sub_id e o produto principal."""
    rows = []
    for node in nodes:
        orders = node.get("orders") or []
        for order in orders:
            items = order.get("items") or []
            commission = sum((_dec(i.get("itemTotalCommission")) for i in items), Decimal("0"))
            if not items:   # sem itens no retorno: usa o total da conversão
                commission = _dec(node.get("totalCommission")) / max(1, len(orders))
            first = items[0] if items else {}
            rows.append({
                "ts": int(node.get("purchaseTime") or 0),
                "order": str(order.get("orderId") or node.get("conversionId") or ""),
                "status": str(order.get("orderStatus") or "PENDING").upper(),
                "commission": str(commission.quantize(Decimal("0.01"))),
                "sub": str(node.get("utmContent") or "")[:100],
                "item": str(first.get("itemName") or "")[:120],
                "item_id": str(first.get("itemId") or ""),
                "qty": sum(int(i.get("qty") or 0) for i in items) or 1,
            })
    return rows[:MAX_ROWS]


@dataclass
class Report:
    rows: list[dict] = field(default_factory=list)
    fetched_at: datetime | None = None
    error: str | None = None

    @property
    def available(self) -> bool:
        return self.fetched_at is not None


def load(session: Session, client_factory: Callable[[], ShopeeClient] | None, *, force: bool = False,
         now: datetime | None = None) -> Report:
    """Relatório da conta da vez: do cache (até 15 min) ou da Shopee."""
    now = now or utcnow()
    values = settings_service.get_all(session)
    cached = _cached(values)
    if cached.available and not force and now - cached.fetched_at < TTL:
        return cached
    if client_factory is None:
        return cached
    try:
        with client_factory() as client:
            nodes = client.conversion_report(int((now - timedelta(days=WINDOW_DAYS)).timestamp()), int(now.timestamp()))
    except ShopeeError as exc:
        cached.error = str(exc)
        return cached
    report = Report(rows=to_rows(nodes), fetched_at=now)
    settings_service.set_values(session, {CACHE_KEY: json.dumps({"at": now.isoformat(), "rows": report.rows},
                                                               separators=(",", ":"))})
    return report


def _cached(values: dict[str, str]) -> Report:
    try:
        data = json.loads(values.get(CACHE_KEY) or "{}")
        return Report(rows=data.get("rows") or [], fetched_at=datetime.fromisoformat(data["at"]))
    except (ValueError, KeyError, TypeError):
        return Report()


def decode_sub(sub: str, bot_names: dict[int, str]) -> tuple[str, str, str]:
    """('Ofertas Tech', 'Telegram', '20h') a partir de 'bot3-tg-h20' (partes que faltarem ficam '—')."""
    parts = [p for p in re.split(r"[-_|,\s]+", sub or "") if p]
    bot = channel = hour = "—"
    for p in parts:
        low = p.lower()
        m = SUB_RE.fullmatch(low)
        if m:
            bot = bot_names.get(int(m.group(1)), f"Bot {m.group(1)}")
        elif low in ("tg", "telegram"):
            channel = "Telegram"
        elif low in ("wa", "whatsapp"):
            channel = "WhatsApp"
        elif re.fullmatch(r"h\d{2}", low):
            hour = f"{int(low[1:])}h"
    return bot, channel, hour


def summarize(rows: list[dict], start: date, end: date, bot_names: dict[int, str] | None = None) -> dict:
    """Resumo do período [start, end] (datas no fuso do Brasil)."""
    bot_names = bot_names or {}
    picked = []
    for r in rows:
        day = datetime.fromtimestamp(r["ts"], TZ).date() if r.get("ts") else None
        if day and start <= day <= end:
            picked.append((day, r))
    valid = [(d, r) for d, r in picked if r["status"] != "CANCELLED"]
    commission = sum((Decimal(r["commission"]) for _, r in valid), Decimal("0"))
    by_status = Counter(r["status"] for _, r in picked)
    per_day: dict[date, list] = defaultdict(lambda: [0, Decimal("0")])
    for d, r in valid:
        per_day[d][0] += 1
        per_day[d][1] += Decimal(r["commission"])
    groups: dict[tuple, list] = defaultdict(lambda: [0, Decimal("0")])
    items: dict[str, list] = defaultdict(lambda: [0, Decimal("0")])
    for _, r in valid:
        key = decode_sub(r.get("sub", ""), bot_names)
        groups[key][0] += 1
        groups[key][1] += Decimal(r["commission"])
        name = r.get("item") or "Produto"
        items[name][0] += r.get("qty") or 1
        items[name][1] += Decimal(r["commission"])
    tracked = any(k != ("—", "—", "—") for k in groups)
    return {
        "orders": len(valid), "commission": commission.quantize(Decimal("0.01")),
        "cancelled": by_status.get("CANCELLED", 0),
        "by_status": [(STATUS_LABELS.get(k, k.title()), n) for k, n in by_status.most_common()],
        "days": sorted(per_day.items()),
        "groups": sorted(((k, v[0], v[1]) for k, v in groups.items()), key=lambda g: g[2], reverse=True)[:30],
        "tracked": tracked,
        "products": sorted(((k, v[0], v[1]) for k, v in items.items()), key=lambda i: i[2], reverse=True)[:8],
    }
