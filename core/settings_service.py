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

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from config import settings
from core.tenancy import ALL_ACCOUNTS, account_or_primary
from models import AppSetting, PlatformSetting

logger = logging.getLogger(__name__)

SECRET_KEYS = {"ml_secret_key", "shopee_secret", "amazon_secret"}
# Valores da PLATAFORMA (fora das contas): sinal de vida dos processos
PLATFORM_KEYS = {"offers_heartbeat_at", "bot_heartbeat_at"}


def _account(account_id: int | None) -> int:
    """A conta informada, ou a da vez, ou a principal."""
    return account_id or account_or_primary()

DEFAULTS: dict[str, str] = {
    # MÓDULO DE OFERTAS — conta do Mercado Livre (uma para todos os bots de ofertas)
    "ml_app_id": "",
    "ml_secret_key": "",
    # MÓDULO DE OFERTAS — conta de afiliado da Shopee (Open API)
    "shopee_app_id": "",
    "shopee_secret": "",
    # MÓDULO DE OFERTAS — conta de associado da Amazon (Creators API)
    "amazon_credential_id": "",
    "amazon_secret": "",
    "amazon_partner_tag": "",
    "amazon_version": "3.1",
    "offers_heartbeat_at": "",
    # MÓDULO DE VENDAS
    "bot_heartbeat_at": "",
    # Administradores no Telegram / alertas (compartilhado)
    "tg_admin_ids": "[]",                 # JSON: IDs vinculados pelo painel
    "tg_link_code": "",                   # JSON: código temporário de vínculo
    "alerts_enabled": "1",
    "daily_summary_hour": "21",
    "alerts_state": "{}",                 # JSON: controle de repetição dos alertas
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


def get_all(session: Session, account_id: int | None = None) -> dict[str, str]:
    """Configurações da conta (a da vez, ou a informada) + valores da plataforma."""
    values = dict(DEFAULTS)
    for row in session.scalars(select(PlatformSetting)).all():
        if row.key in PLATFORM_KEYS and row.value is not None:
            values[row.key] = row.value
    rows = session.scalars(select(AppSetting).where(AppSetting.account_id == _account(account_id))
                           .execution_options(**{ALL_ACCOUNTS: True})).all()
    for row in rows:
        if row.value is None:
            continue
        values[row.key] = decrypt(row.value) if row.is_secret else row.value
    return values


def set_values(session: Session, values: dict[str, str], admin_id: int | None = None,
               account_id: int | None = None) -> None:
    acct = _account(account_id)
    for key, value in values.items():
        if key not in DEFAULTS:
            raise KeyError(key)
        if key in PLATFORM_KEYS:
            pstmt = pg_insert(PlatformSetting).values(key=key, value=value)
            session.execute(pstmt.on_conflict_do_update(index_elements=[PlatformSetting.key],
                                                        set_={"value": pstmt.excluded.value,
                                                              "updated_at": pstmt.excluded.updated_at}))
            continue
        secret = key in SECRET_KEYS
        stored = encrypt(value) if (secret and value) else value
        stmt = pg_insert(AppSetting).values(account_id=acct, key=key, value=stored, is_secret=secret,
                                            updated_by_admin_id=admin_id)
        session.execute(
            stmt.on_conflict_do_update(
                index_elements=[AppSetting.account_id, AppSetting.key],
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
