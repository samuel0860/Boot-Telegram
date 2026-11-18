"""MÓDULO DE OFERTAS — processo que roda todos os bots de ofertas (Telegram + WhatsApp).

Execução:  python offers_bot.py

Os bots, seus tokens, destinos e configurações são cadastrados no painel
(Ofertas → Bots de ofertas). Este processo é independente do bot de vendas.
"""
from __future__ import annotations

import asyncio
import logging

from config import configure_logging, settings
from core import error_reporter
from database import check_connection
from offers.runtime import OffersRuntime

logger = logging.getLogger("offers")


def main() -> None:
    configure_logging()
    settings.require_database()
    if settings.error_reports:
        error_reporter.install("módulo de ofertas")
    if not check_connection():
        raise SystemExit("Não foi possível conectar ao PostgreSQL. Verifique DATABASE_URL.")
    asyncio.run(OffersRuntime().run_forever())


if __name__ == "__main__":
    main()
