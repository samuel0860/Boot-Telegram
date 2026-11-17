"""Configurações editáveis pelo painel (tabela app_settings).

Segredos (ex.: Secret Key do Mercado Livre) são gravados criptografados com
Fernet (AES + HMAC), usando uma chave derivada da SECRET_KEY do .env.
Eles nunca são exibidos de volta no painel.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import time
from decimal import Decimal, InvalidOperation

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from config import settings
from models import AppSetting

logger = logging.getLogger(__name__)

SECRET_KEYS = {"ml_secret_key"}

DEFAULTS: dict[str, str] = {
    "ml_app_id": "",
    "ml_secret_key": "",
    "promo_enabled": "0",
    "promo_channel_id": "",
    "promo_mode": "auto",                 # auto | manual
    "promo_interval_minutes": "45",
    "promo_max_posts_per_cycle": "3",
    "promo_spacing_seconds": "120",
    "promo_min_discount": "20",
    "promo_min_price": "0",
    "promo_max_price": "0",
    "promo_blocked_words": "",
    "promo_categories": "",
    "promo_keywords": "",
    "promo_no_repeat_days": "7",
    "promo_active_hours": "08:00-23:00",
    "promo_footer": "Link de afiliado: posso receber comissão pelas compras, sem custo extra para você.",
    # WhatsApp
    "wa_enabled": "0",
    "wa_instance": "promocoes",
    "wa_groups": "[]",                    # JSON: [{"id": "...@g.us", "name": "..."}]
    "wa_warmup_days": "7",
    "wa_warmup_per_day": "5",
    "wa_max_per_window": "5",
    "wa_window_hours": "4",
    "wa_max_per_day": "20",
    "wa_started_at": "",                  # início do aquecimento (ISO)
    "wa_group_delay_seconds": "10",
    "wa_max_age_hours": "6",
    # Telegram dos administradores / alertas
    "tg_admin_ids": "[]",                 # JSON: IDs vinculados pelo painel
    "tg_link_code": "",                   # JSON: código temporário de vínculo
    "bot_heartbeat_at": "",
    "alerts_enabled": "1",
    "daily_summary_hour": "21",
    "alerts_state": "{}",                 # JSON: controle de repetição dos alertas
    "promo_last_run_at": "",
    "promo_last_result": "",
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


def _fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(("app-settings:" + settings.secret_key).encode()).digest())
    return Fernet(key)


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken:
        logger.error("Não foi possível descriptografar uma configuração (SECRET_KEY mudou?). Cadastre-a de novo.")
        return ""


def get_all(session: Session) -> dict[str, str]:
    values = dict(DEFAULTS)
    for row in session.scalars(select(AppSetting)).all():
        if row.value is None:
            continue
        values[row.key] = decrypt(row.value) if row.is_secret else row.value
    return values


def set_values(session: Session, values: dict[str, str], admin_id: int | None = None) -> None:
    for key, value in values.items():
        if key not in DEFAULTS:
            raise KeyError(key)
        secret = key in SECRET_KEYS
        stored = encrypt(value) if (secret and value) else value
        stmt = pg_insert(AppSetting).values(key=key, value=stored, is_secret=secret, updated_by_admin_id=admin_id)
        session.execute(
            stmt.on_conflict_do_update(
                index_elements=[AppSetting.key],
                set_={"value": stmt.excluded.value, "is_secret": secret,
                      "updated_by_admin_id": admin_id, "updated_at": stmt.excluded.updated_at},
            )
        )


def split_list(raw: str) -> list[str]:
    return [p.strip() for p in re.split(r"[,\n;]+", raw or "") if p.strip()]


def mask(value: str) -> str:
    if not value:
        return ""
    return "•" * 8 + value[-4:] if len(value) > 6 else "•" * 6


# --------------------------------------------------------------------------- #
# Configuração tipada do bot de promoções
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PromoConfig:
    app_id: str
    secret_key: str
    enabled: bool
    channel_id: str
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
    footer: str = ""

    @property
    def credentials_ok(self) -> bool:
        return bool(self.app_id and self.secret_key)

    def is_active_time(self, now_local: time) -> bool:
        if not self.active_start or not self.active_end:
            return True
        if self.active_start <= self.active_end:
            return self.active_start <= now_local <= self.active_end
        return now_local >= self.active_start or now_local <= self.active_end  # atravessa meia-noite


def _int(v: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(str(v).strip())))
    except ValueError:
        return default


def _dec(v: str) -> Decimal:
    try:
        return max(Decimal(str(v).replace(",", ".").strip() or "0"), Decimal("0"))
    except InvalidOperation:
        return Decimal("0")


def parse_hours(raw: str) -> tuple[time | None, time | None]:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", raw or "")
    if not m:
        return None, None
    h1, m1, h2, m2 = map(int, m.groups())
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        return None, None
    return time(h1, m1), time(h2, m2)


def promo_config(values: dict[str, str]) -> PromoConfig:
    start, end = parse_hours(values["promo_active_hours"])
    return PromoConfig(
        app_id=values["ml_app_id"].strip(),
        secret_key=values["ml_secret_key"].strip(),
        enabled=values["promo_enabled"] == "1",
        channel_id=values["promo_channel_id"].strip(),
        mode="manual" if values["promo_mode"] == "manual" else "auto",
        interval_minutes=_int(values["promo_interval_minutes"], 45, 10, 1440),
        max_posts_per_cycle=_int(values["promo_max_posts_per_cycle"], 3, 1, 20),
        spacing_seconds=_int(values["promo_spacing_seconds"], 120, 15, 3600),
        min_discount=_int(values["promo_min_discount"], 20, 0, 95),
        min_price=_dec(values["promo_min_price"]),
        max_price=_dec(values["promo_max_price"]),
        blocked_words=[w.lower() for w in split_list(values["promo_blocked_words"])],
        categories=[c.upper() for c in split_list(values["promo_categories"]) if re.fullmatch(r"MLB\d+", c.upper())],
        keywords=split_list(values["promo_keywords"])[:10],
        no_repeat_days=_int(values["promo_no_repeat_days"], 7, 0, 365),
        active_start=start,
        active_end=end,
        footer=values["promo_footer"].strip(),
    )


def load_promo_config(session: Session) -> PromoConfig:
    return promo_config(get_all(session))
