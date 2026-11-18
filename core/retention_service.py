"""LGPD — apaga registros técnicos antigos que não são mais necessários.

O que é apagado (prazos no .env; 0 = nunca apagar):
- access_logs com mais de LOG_RETENTION_DAYS dias (histórico de ações, IPs);
- webhook_events com mais de WEBHOOK_RETENTION_DAYS dias (cópia das notificações);
- payments.raw_response de pagamentos antigos (resposta bruta do provedor),
  mantendo o pagamento em si — ele é registro financeiro.

Pedidos, pagamentos e acessos NÃO são apagados: são registros de venda que a
legislação fiscal/contábil pode exigir. Para excluir os dados de um cliente que
pedir, use o botão "Anonimizar" na página do usuário no painel.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, update

from config import settings
from core.audit import log_event, utcnow
from database import session_scope
from models import AccessLog, Payment, User, WebhookEvent

logger = logging.getLogger(__name__)
BATCH = 5000


@dataclass
class PurgeResult:
    logs: int = 0
    webhooks: int = 0
    payment_raw: int = 0

    @property
    def total(self) -> int:
        return self.logs + self.webhooks + self.payment_raw


def purge_old_records() -> PurgeResult:
    now = utcnow()
    result = PurgeResult()
    with session_scope() as session:
        if settings.log_retention_days > 0:
            cutoff = now - timedelta(days=settings.log_retention_days)
            result.logs = session.execute(delete(AccessLog).where(AccessLog.created_at < cutoff)).rowcount or 0
        if settings.webhook_retention_days > 0:
            cutoff = now - timedelta(days=settings.webhook_retention_days)
            result.webhooks = session.execute(
                delete(WebhookEvent).where(WebhookEvent.created_at < cutoff)).rowcount or 0
            result.payment_raw = session.execute(
                update(Payment).where(Payment.created_at < cutoff, Payment.raw_response.is_not(None))
                .values(raw_response=None)).rowcount or 0
        if result.total:
            log_event(session, "retention_purge", logs=result.logs, webhooks=result.webhooks,
                      payment_raw=result.payment_raw)
    if result.total:
        logger.info("Retenção (LGPD): %s logs, %s webhooks e %s respostas brutas apagados",
                    result.logs, result.webhooks, result.payment_raw)
    return result


def anonymize_user(user_id: int, admin_id: int | None, ip: str | None) -> bool:
    """Atende pedido de exclusão (LGPD): remove nome/usuário do Telegram e o
    histórico de ações do cliente. Mantém o ID numérico e os pedidos (registro
    financeiro) — sem eles não é possível tratar estornos e contestações."""
    with session_scope() as session:
        user = session.get(User, user_id, with_for_update=True)
        if user is None:
            return False
        user.username = None
        user.first_name = None
        user.last_name = None
        user.language_code = None
        session.execute(delete(AccessLog).where(AccessLog.user_id == user_id))
        log_event(session, "user_anonymized", admin_user_id=admin_id, ip_address=ip, user_id=user_id)
    return True
