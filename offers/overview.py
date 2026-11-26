"""Central dos bots de promoção: situação de cada bot numa olhada só.

Para cada bot: nível (tudo certo / atenção / desligado), o que falta configurar,
avisos, último post, próxima busca e resultado da última busca.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from models import OfferBot, TargetChannel
from offers import bots as bot_service
from offers import collector, whatsapp
from offers.config import BotConfig

LEVELS = {"ok": "Funcionando", "warn": "Precisa de atenção", "off": "Desligado", "setup": "Configuração incompleta"}


@dataclass
class Step:
    label: str
    done: bool
    href: str
    optional: bool = False


@dataclass
class BotOverview:
    bot: OfferBot
    level: str
    steps: list[Step]
    problems: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    last_post_at: datetime | None = None
    next_search_at: datetime | None = None
    last_summary: str = ""
    tg_count: int = 0
    wa_count: int = 0
    stores: list[str] = field(default_factory=list)

    @property
    def level_label(self) -> str:
        return LEVELS[self.level]

    @property
    def done_steps(self) -> int:
        return sum(1 for s in self.steps if s.done and not s.optional)

    @property
    def required_steps(self) -> int:
        return sum(1 for s in self.steps if not s.optional)


def build(bot: OfferBot, cfg: BotConfig, stats: dict, worker_online: bool, now: datetime) -> BotOverview:
    base = f"/admin/offers/bots/{bot.id}"
    tg = bot_service.active_targets(bot, TargetChannel.TELEGRAM)
    wa = whatsapp.wa_config(bot, cfg)
    wa_groups = bot_service.active_targets(bot, TargetChannel.WHATSAPP)
    steps = [
        Step("Token do Telegram cadastrado", bool(bot.telegram_token), f"{base}?tab=telegram"),
        Step("Canal ou grupo do Telegram", bool(tg), f"{base}?tab=telegram"),
        Step("Loja ligada com a conta configurada", cfg.sources_ready and not cfg.missing_credentials(),
             f"{base}?tab=busca"),
        Step("WhatsApp conectado com grupos", wa.ready or (cfg.wa_enabled and bool(wa_groups)),
             f"{base}?tab=whatsapp", optional=True),
        Step("Bot ligado", bot.is_active, base),
    ]
    if not (tg or wa_groups):
        # sem Telegram, o WhatsApp vira obrigatório
        steps[0].optional = steps[1].optional = True
        steps[3].optional = False

    problems: list[str] = []
    for store in cfg.missing_credentials():
        article = "do" if store == "Mercado Livre" else "da"
        problems.append(f"Falta a conta {article} {store} (Lojas e APIs).")
    last = collector.last_result(bot)
    if bot.is_active and not worker_online:
        problems.append("O processo que publica as ofertas não está respondendo.")
    if last and not last.get("ok", True):
        problems.append("A última busca deu erro: " + str(last.get("summary", ""))[:160])
    if stats.get("failed"):
        problems.append(f"{stats['failed']} oferta(s) com falha ao publicar.")
    if bot.is_active and cfg.wa_enabled and wa_groups and not bot.wa_instance:
        problems.append("WhatsApp ligado, mas o número não está conectado.")

    required_ok = all(s.done for s in steps if not s.optional and s.label != "Bot ligado")
    if not required_ok:
        level = "setup"
    elif not bot.is_active:
        level = "off"
    elif problems:
        level = "warn"
    else:
        level = "ok"

    next_search = None
    if bot.is_active and cfg.sources_ready:
        run = bot_service.last_run(bot)
        next_search = (run + timedelta(minutes=cfg.interval_minutes)) if run else now
        next_search = max(next_search, now)

    stores = [name for on, name in ((cfg.use_mercadolivre, "Mercado Livre"), (cfg.use_shopee, "Shopee"),
                                    (cfg.use_amazon, "Amazon")) if on]
    return BotOverview(bot=bot, level=level, steps=steps, problems=problems, stats=stats,
                       last_post_at=stats.get("last_posted_at"), next_search_at=next_search,
                       last_summary=str(last.get("summary", "")) if last else "",
                       tg_count=len(tg), wa_count=len(wa_groups), stores=stores)
