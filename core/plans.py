"""Planos da assinatura (vendidos na Kiwify) e os limites de cada um."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlanSpec:
    key: str
    name: str
    price: int                 # R$/mês (referência; quem cobra é a Kiwify)
    max_offer_bots: int
    whatsapp: bool
    sales_bot: bool
    tagline: str


PLANS: dict[str, PlanSpec] = {
    "start": PlanSpec("start", "Start", 47, 1, False, False,
                      "1 bot de promoções no Telegram"),
    "pro": PlanSpec("pro", "Pro", 97, 3, True, True,
                    "3 bots de promoções, WhatsApp e bot de vendas com PIX"),
    "agencia": PlanSpec("agencia", "Agência", 197, 10, True, True,
                        "10 bots de promoções, WhatsApp e bot de vendas com PIX"),
}
DEFAULT_PLAN = "pro"


def spec(plan_key: str | None) -> PlanSpec:
    return PLANS.get(plan_key or "", PLANS[DEFAULT_PLAN])
