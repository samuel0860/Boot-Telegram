"""Configuração de cada bot de ofertas (guardada em JSON em offer_bots.config).

As credenciais das lojas (Mercado Livre, Shopee e Amazon) são do MÓDULO — uma conta de
cada para todos os bots — e ficam em app_settings, com os segredos criptografados.
Cada bot escolhe de quais lojas busca ofertas.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from config import settings
from offers import post_template

TZ = ZoneInfo(settings.display_timezone)

DEFAULT_CONFIG: dict[str, str] = {
    # Lojas de onde o bot busca ofertas
    "src_mercadolivre": "1",
    "src_shopee": "0",
    # Shopee
    "shopee_keywords": "",
    "shopee_categories": "",           # IDs de categoria da Shopee (opcional)
    "shopee_sort": "sales",            # sales | commission | relevance | price_asc
    # Amazon
    "src_amazon": "0",
    "amazon_keywords": "",
    "amazon_nodes": "",                # IDs de categoria (browse node) da Amazon (opcional)
    "amazon_sort": "featured",         # featured | relevance | price_asc | reviews | newest
    # Busca no Mercado Livre
    "mode": "auto",                    # auto | manual
    "interval_minutes": "45",
    "max_posts_per_cycle": "3",
    "spacing_seconds": "120",
    "min_discount": "20",
    "min_price": "0",
    "max_price": "0",
    "blocked_words": "",
    "categories": "",
    "keywords": "",
    "no_repeat_days": "7",
    "active_hours": "08:00-23:00",
    # Texto do post
    "template": "",                    # vazio = modelo padrão
    "default_headline": "",            # vazio = chamada padrão
    "footer": "Link de afiliado: posso receber comissão pelas compras, sem custo extra para você.",
    # WhatsApp (número próprio do bot)
    "wa_enabled": "0",
    "wa_warmup_days": "7",
    "wa_warmup_per_day": "5",
    "wa_max_per_window": "5",
    "wa_window_hours": "4",
    "wa_max_per_day": "20",
    "wa_group_delay_seconds": "10",
    "wa_max_age_hours": "6",
}

# Categorias principais do Mercado Livre Brasil (IDs oficiais MLB)
ML_CATEGORIES: dict[str, str] = {
    "MLB1051": "Celulares e Telefones",
    "MLB1648": "Informática",
    "MLB1000": "Eletrônicos, Áudio e Vídeo",
    "MLB5726": "Eletrodomésticos",
    "MLB1144": "Games",
    "MLB1574": "Casa, Móveis e Decoração",
    "MLB263532": "Ferramentas",
    "MLB1276": "Esportes e Fitness",
    "MLB1430": "Calçados, Roupas e Bolsas",
    "MLB1246": "Beleza e Cuidado Pessoal",
    "MLB264586": "Saúde",
    "MLB1132": "Brinquedos e Hobbies",
    "MLB1384": "Bebês",
    "MLB1071": "Animais",
    "MLB1403": "Alimentos e Bebidas",
    "MLB5672": "Acessórios para Veículos",
    "MLB1039": "Câmeras e Acessórios",
    "MLB3937": "Joias e Relógios",
    "MLB1182": "Instrumentos Musicais",
    "MLB1196": "Livros, Revistas e Comics",
}


def _int(v, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(str(v).strip())))
    except (TypeError, ValueError):
        return default


def _dec(v) -> Decimal:
    try:
        return max(Decimal(str(v or "0").replace(",", ".").strip() or "0"), Decimal("0"))
    except InvalidOperation:
        return Decimal("0")


def split_list(raw: str) -> list[str]:
    return [p.strip() for p in re.split(r"[,\n;]+", raw or "") if p.strip()]


def parse_hours(raw: str) -> tuple[time | None, time | None]:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", raw or "")
    if not m:
        return None, None
    h1, m1, h2, m2 = map(int, m.groups())
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        return None, None
    return time(h1, m1), time(h2, m2)


@dataclass(frozen=True)
class BotConfig:
    # credenciais do módulo (Mercado Livre)
    app_id: str
    secret_key: str
    # busca e publicação
    enabled: bool
    mode: str
    interval_minutes: int
    max_posts_per_cycle: int
    spacing_seconds: int
    min_discount: int
    min_price: Decimal
    max_price: Decimal
    blocked_words: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    no_repeat_days: int = 7
    active_start: time | None = None
    active_end: time | None = None
    # texto do post
    footer: str = ""
    template: str = post_template.DEFAULT_TEMPLATE
    default_headline: str = post_template.DEFAULT_HEADLINE
    # WhatsApp
    wa_enabled: bool = False
    wa_warmup_days: int = 7
    wa_warmup_per_day: int = 5
    wa_max_per_window: int = 5
    wa_window_hours: int = 4
    wa_max_per_day: int = 20
    wa_group_delay_seconds: int = 10
    wa_max_age_hours: int = 6
    # lojas
    use_mercadolivre: bool = True
    use_shopee: bool = False
    shopee_app_id: str = ""
    shopee_secret: str = ""
    shopee_keywords: list[str] = field(default_factory=list)
    shopee_categories: list[int] = field(default_factory=list)
    shopee_sort: str = "sales"
    use_amazon: bool = False
    amazon_id: str = ""
    amazon_secret: str = ""
    amazon_tag: str = ""
    amazon_version: str = "3.1"
    amazon_keywords: list[str] = field(default_factory=list)
    amazon_nodes: list[str] = field(default_factory=list)
    amazon_sort: str = "featured"

    @property
    def credentials_ok(self) -> bool:
        """Credenciais do Mercado Livre preenchidas."""
        return bool(self.app_id and self.secret_key)

    @property
    def shopee_credentials_ok(self) -> bool:
        return bool(self.shopee_app_id and self.shopee_secret)

    @property
    def amazon_credentials_ok(self) -> bool:
        return bool(self.amazon_id and self.amazon_secret and self.amazon_tag)

    @property
    def ml_ready(self) -> bool:
        return self.use_mercadolivre and self.credentials_ok

    @property
    def shopee_ready(self) -> bool:
        return self.use_shopee and self.shopee_credentials_ok

    @property
    def amazon_ready(self) -> bool:
        return self.use_amazon and self.amazon_credentials_ok

    @property
    def any_store(self) -> bool:
        return self.use_mercadolivre or self.use_shopee or self.use_amazon

    @property
    def sources_ready(self) -> bool:
        """Pelo menos uma loja ligada e com credenciais."""
        return self.ml_ready or self.shopee_ready or self.amazon_ready

    def missing_credentials(self) -> list[str]:
        missing = []
        if self.use_mercadolivre and not self.credentials_ok:
            missing.append("Mercado Livre")
        if self.use_shopee and not self.shopee_credentials_ok:
            missing.append("Shopee")
        if self.use_amazon and not self.amazon_credentials_ok:
            missing.append("Amazon")
        return missing

    def is_active_time(self, now_local: time) -> bool:
        if not self.active_start or not self.active_end:
            return True
        if self.active_start <= self.active_end:
            return self.active_start <= now_local <= self.active_end
        return now_local >= self.active_start or now_local <= self.active_end  # atravessa meia-noite

    def active_now(self, now: datetime) -> bool:
        return self.is_active_time(now.astimezone(TZ).time())


def merged(raw: dict | None) -> dict[str, str]:
    values = dict(DEFAULT_CONFIG)
    for k, v in (raw or {}).items():
        if k in DEFAULT_CONFIG and v is not None:
            values[k] = str(v)
    return values


def parse(raw: dict | None, *, enabled: bool = False, app_id: str = "", secret_key: str = "",
          shopee_app_id: str = "", shopee_secret: str = "", amazon_id: str = "", amazon_secret: str = "",
          amazon_tag: str = "", amazon_version: str = "3.1") -> BotConfig:
    v = merged(raw)
    start, end = parse_hours(v["active_hours"])
    return BotConfig(
        app_id=app_id.strip(),
        secret_key=secret_key.strip(),
        enabled=enabled,
        mode="manual" if v["mode"] == "manual" else "auto",
        interval_minutes=_int(v["interval_minutes"], 45, 10, 1440),
        max_posts_per_cycle=_int(v["max_posts_per_cycle"], 3, 1, 20),
        spacing_seconds=_int(v["spacing_seconds"], 120, 15, 3600),
        min_discount=_int(v["min_discount"], 20, 0, 95),
        min_price=_dec(v["min_price"]),
        max_price=_dec(v["max_price"]),
        blocked_words=[w.lower() for w in split_list(v["blocked_words"])],
        categories=[c.upper() for c in split_list(v["categories"]) if re.fullmatch(r"MLB\d+", c.upper())],
        keywords=split_list(v["keywords"])[:10],
        no_repeat_days=_int(v["no_repeat_days"], 7, 0, 365),
        active_start=start,
        active_end=end,
        footer=v["footer"].strip(),
        template=v["template"].strip() or post_template.DEFAULT_TEMPLATE,
        default_headline=v["default_headline"].strip() or post_template.DEFAULT_HEADLINE,
        wa_enabled=v["wa_enabled"] == "1",
        wa_warmup_days=_int(v["wa_warmup_days"], 7, 0, 30),
        wa_warmup_per_day=_int(v["wa_warmup_per_day"], 5, 1, 10),
        wa_max_per_window=_int(v["wa_max_per_window"], 5, 1, 10),
        wa_window_hours=_int(v["wa_window_hours"], 4, 1, 12),
        wa_max_per_day=_int(v["wa_max_per_day"], 20, 1, 60),
        wa_group_delay_seconds=_int(v["wa_group_delay_seconds"], 10, 5, 120),
        wa_max_age_hours=_int(v["wa_max_age_hours"], 6, 1, 48),
        use_mercadolivre=v["src_mercadolivre"] == "1",
        use_shopee=v["src_shopee"] == "1",
        shopee_app_id=shopee_app_id.strip(),
        shopee_secret=shopee_secret.strip(),
        shopee_keywords=split_list(v["shopee_keywords"])[:10],
        shopee_categories=[int(c) for c in split_list(v["shopee_categories"]) if c.isdigit()][:10],
        shopee_sort=v["shopee_sort"] if v["shopee_sort"] in ("sales", "commission", "relevance", "price_asc") else "sales",
        use_amazon=v["src_amazon"] == "1",
        amazon_id=amazon_id.strip(),
        amazon_secret=amazon_secret.strip(),
        amazon_tag=amazon_tag.strip(),
        amazon_version=amazon_version.strip() or "3.1",
        amazon_keywords=split_list(v["amazon_keywords"])[:10],
        amazon_nodes=[c for c in split_list(v["amazon_nodes"]) if c.isdigit()][:10],
        amazon_sort=v["amazon_sort"] if v["amazon_sort"] in ("featured", "relevance", "price_asc", "reviews", "newest")
        else "featured",
    )
