"""Rotinas periódicas do bot (JobQueue)."""
from __future__ import annotations

import logging
import time

from telegram.ext import (
    ContextTypes,
)

from core import alert_service, error_reporter, retention_service, settings_service
from database import session_scope
from sales import alerts as sales_alerts
from sales import payment_service, telegram_service
from sales.bot.common import run_db
from sales.payments import get_payment_client

logger = logging.getLogger("bot")


# --------------------------------------------------------------------------- #
# Jobs periódicos
# --------------------------------------------------------------------------- #
async def job_deliveries(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await telegram_service.process_pending_deliveries(context.bot)
    except Exception:  # noqa: BLE001
        logger.exception("Erro no job de entregas")


async def job_reconcile(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await run_db(payment_service.reconcile_pending_orders, get_payment_client())
    except Exception:  # noqa: BLE001
        logger.exception("Erro no job de reconciliação")


async def job_expirations(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await telegram_service.process_expirations(context.bot)
        await telegram_service.process_reminders(context.bot)
    except Exception:  # noqa: BLE001
        logger.exception("Erro no job de expiração")


async def job_recoveries(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await telegram_service.process_recoveries(context.bot)
    except Exception:  # noqa: BLE001
        logger.exception("Erro no job de recuperação de PIX")


async def job_retention(context: ContextTypes.DEFAULT_TYPE) -> None:
    """LGPD: apaga registros técnicos antigos (prazos no .env)."""
    try:
        await run_db(retention_service.purge_old_records)
    except Exception:  # noqa: BLE001
        logger.exception("Erro no job de retenção de dados")


# --------------------------------------------------------------------------- #
# Alertas, resumo diário e sinal de vida
# --------------------------------------------------------------------------- #
_last_alert_check = 0.0


async def job_health(context: ContextTypes.DEFAULT_TYPE) -> None:
    global _last_alert_check
    try:
        await run_db(alert_service.heartbeat)
        if time.monotonic() - _last_alert_check < 300:
            return
        _last_alert_check = time.monotonic()

        def _check():
            with session_scope() as session:
                values = settings_service.get_all(session)
            alerts = alert_service.alerts_to_send(sales_alerts.detect_problems(), scope=sales_alerts.SCOPE)
            summary = (sales_alerts.daily_summary()
                       if alert_service.summary_due(values, scope=sales_alerts.SCOPE) else None)
            return alerts, summary, alert_service.admin_ids(values)

        alerts, summary, admins = await run_db(_check)
        if not admins:
            return
        for admin_id in admins:
            for text in alerts:
                await telegram_service.safe_send(context.bot, admin_id, text)
            if summary:
                await telegram_service.safe_send(context.bot, admin_id, summary)
        if summary:
            await run_db(alert_service.mark_summary_sent, scope=sales_alerts.SCOPE)
    except Exception:  # noqa: BLE001
        logger.exception("Erro no job de saúde/alertas")


async def job_errors(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await error_reporter.flush_async(context.bot)
    except Exception:  # noqa: BLE001
        logger.warning("Falha ao enviar o aviso de erros aos administradores", exc_info=True)
