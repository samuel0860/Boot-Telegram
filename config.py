"""Configuração central do projeto.

Todas as configurações vêm de variáveis de ambiente (arquivo .env carregado
com python-dotenv). Variáveis já definidas no ambiente (ex.: pelo Docker
Compose) têm prioridade sobre o .env.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=False)


class ConfigError(RuntimeError):
    """Configuração obrigatória ausente ou inválida."""


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "sim"}


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"Variável {name} deve ser um número inteiro") from exc


def _int_list(name: str) -> frozenset[int]:
    raw = _str(name)
    if not raw:
        return frozenset()
    try:
        return frozenset(int(item) for item in raw.replace(";", ",").split(",") if item.strip())
    except ValueError as exc:
        raise ConfigError(f"Variável {name} deve conter IDs numéricos separados por vírgula") from exc


@dataclass(frozen=True)
class Settings:
    # Aplicação
    app_env: str = field(default_factory=lambda: _str("APP_ENV", "development").lower())
    log_level: str = field(default_factory=lambda: _str("LOG_LEVEL", "INFO").upper())
    secret_key: str = field(default_factory=lambda: _str("SECRET_KEY"))
    public_base_url: str = field(default_factory=lambda: _str("PUBLIC_BASE_URL").rstrip("/"))
    display_timezone: str = field(default_factory=lambda: _str("DISPLAY_TIMEZONE", "America/Sao_Paulo"))

    # Banco de dados
    database_url: str = field(default_factory=lambda: _str("DATABASE_URL"))
    sql_echo: bool = field(default_factory=lambda: _bool("SQL_ECHO"))
    db_pool_size: int = field(default_factory=lambda: _int("DB_POOL_SIZE", 5))
    db_max_overflow: int = field(default_factory=lambda: _int("DB_MAX_OVERFLOW", 10))

    # Telegram
    telegram_bot_token: str = field(default_factory=lambda: _str("TELEGRAM_BOT_TOKEN"))
    telegram_bot_username: str = field(default_factory=lambda: _str("TELEGRAM_BOT_USERNAME").lstrip("@"))
    telegram_start_param: str = field(default_factory=lambda: _str("TELEGRAM_START_PARAM", "tiktok"))
    support_contact: str = field(default_factory=lambda: _str("SUPPORT_CONTACT"))
    admin_telegram_ids: frozenset[int] = field(default_factory=lambda: _int_list("ADMIN_TELEGRAM_IDS"))
    invite_link_ttl_hours: int = field(default_factory=lambda: _int("INVITE_LINK_TTL_HOURS", 24))
    expiry_reminder_days: int = field(default_factory=lambda: _int("EXPIRY_REMINDER_DAYS", 3))
    delivery_max_attempts: int = field(default_factory=lambda: _int("DELIVERY_MAX_ATTEMPTS", 5))

    # Pagamentos (Mercado Pago / PIX)
    mp_access_token: str = field(default_factory=lambda: _str("MP_ACCESS_TOKEN"))
    mp_webhook_secret: str = field(default_factory=lambda: _str("MP_WEBHOOK_SECRET"))
    mp_api_base: str = field(default_factory=lambda: _str("MP_API_BASE", "https://api.mercadopago.com").rstrip("/"))
    pix_expiration_minutes: int = field(default_factory=lambda: _int("PIX_EXPIRATION_MINUTES", 30))
    payer_email_domain: str = field(default_factory=lambda: _str("PAYER_EMAIL_DOMAIN", "example.com"))
    currency: str = field(default_factory=lambda: _str("CURRENCY", "BRL").upper())
    max_orders_per_10min: int = field(default_factory=lambda: _int("MAX_ORDERS_PER_10MIN", 5))

    # Painel administrativo
    session_max_age_seconds: int = field(default_factory=lambda: _int("SESSION_MAX_AGE_SECONDS", 8 * 3600))
    admin_max_failed_logins: int = field(default_factory=lambda: _int("ADMIN_MAX_FAILED_LOGINS", 5))
    admin_lockout_minutes: int = field(default_factory=lambda: _int("ADMIN_LOCKOUT_MINUTES", 15))

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def session_cookie_secure(self) -> bool:
        return _bool("SESSION_COOKIE_SECURE", self.is_production)

    @property
    def telegram_link(self) -> str:
        """Link público do bot usado pela página inicial."""
        if not self.telegram_bot_username:
            return "#"
        link = f"https://t.me/{quote(self.telegram_bot_username)}"
        if self.telegram_start_param:
            link += f"?start={quote(self.telegram_start_param)}"
        return link

    @property
    def payments_configured(self) -> bool:
        return bool(self.mp_access_token)

    @property
    def webhook_url(self) -> str | None:
        if self.public_base_url.startswith("https://"):
            return f"{self.public_base_url}/webhooks/mercadopago"
        return None

    # ---- Validações por processo ----
    def require_database(self) -> None:
        if not self.database_url:
            raise ConfigError("DATABASE_URL não definida. Copie .env.example para .env e configure.")
        if not self.database_url.startswith("postgresql"):
            raise ConfigError("DATABASE_URL deve apontar para PostgreSQL (postgresql+psycopg://...). SQLite não é suportado.")

    def require_web(self) -> None:
        self.require_database()
        if len(self.secret_key) < 32:
            raise ConfigError("SECRET_KEY deve ter pelo menos 32 caracteres (gere com: openssl rand -hex 32).")
        if self.is_production and not self.mp_webhook_secret:
            raise ConfigError("MP_WEBHOOK_SECRET é obrigatório em produção.")

    def require_bot(self) -> None:
        self.require_database()
        if not self.telegram_bot_token:
            raise ConfigError("TELEGRAM_BOT_TOKEN não definido.")


settings = Settings()


def configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
    )
    # Evita que o token do bot apareça em logs de requisições HTTP.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # Jobs periódicos rodam a cada 10 s; só registra avisos/erros deles.
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
