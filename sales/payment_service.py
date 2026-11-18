"""Regras de negócio de pedidos e pagamentos.

Princípios de segurança:
- O acesso SÓ é liberado quando o pagamento é consultado na API do provedor
  e está "approved". O conteúdo do webhook nunca é usado como fonte de verdade.
- Antes de liberar: confere external_reference (pedido), valor, moeda e método.
- Idempotência: pedidos bloqueados com SELECT ... FOR UPDATE, UNIQUE em
  (provider, provider_payment_id), UNIQUE em subscriptions.order_id e
  UNIQUE em webhook_events(provider, event_key).
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from config import settings
from database import session_scope
from models import (
    Order,
    OrderStatus,
    Payment,
    PaymentStatus,
    Plan,
    User,
    WebhookEvent,
    WebhookStatus,
)
from sales import access_service, sales_service
from sales.access_service import log_event, utcnow
from sales.payments import (
    MERCADOPAGO,
    PaymentClient,
    PaymentNotFound,
    PaymentProviderError,
    ProviderPayment,
    verify_mercadopago_signature,
)

logger = logging.getLogger(__name__)


class CheckoutError(Exception):
    """Erro com mensagem amigável para o usuário."""


class CouponRejected(CheckoutError):
    """Cupom inválido para esta compra (o bot oferece comprar sem o cupom)."""


@dataclass(frozen=True)
class CheckoutResult:
    order_id: int
    order_public_id: str
    plan_name: str
    amount: Decimal
    currency: str
    qr_code: str
    qr_code_base64: str | None
    ticket_url: str | None
    expires_at: datetime | None
    reused: bool
    coupon_code: str | None = None
    list_price: Decimal | None = None


class ApplyResult(str, Enum):
    GRANTED = "granted"
    ALREADY_PROCESSED = "already_processed"
    UPDATED = "updated"
    REVOKED = "revoked"
    IGNORED = "ignored"
    VALIDATION_FAILED = "validation_failed"


# --------------------------------------------------------------------------- #
# Checkout (gera a cobrança PIX)
# --------------------------------------------------------------------------- #
def _result_from(order: Order, plan: Plan, payment: Payment, reused: bool) -> CheckoutResult:
    return CheckoutResult(
        order_id=order.id,
        order_public_id=str(order.public_id),
        plan_name=plan.name,
        amount=order.amount,
        currency=order.currency,
        qr_code=payment.pix_qr_code or "",
        qr_code_base64=payment.pix_qr_code_base64,
        ticket_url=payment.pix_ticket_url,
        expires_at=payment.expires_at,
        reused=reused,
        coupon_code=order.coupon.code if order.coupon_id and order.coupon else None,
        list_price=plan.price,
    )


def create_pix_checkout(telegram_id: int, plan_id: int, client: PaymentClient,
                        coupon_code: str | None = None) -> CheckoutResult:
    if not client.configured:
        logger.error("Tentativa de compra sem MP_ACCESS_TOKEN configurado")
        raise CheckoutError("Pagamentos indisponíveis no momento. Tente novamente mais tarde.")

    now = utcnow()
    # 1) Cria (ou reaproveita) o pedido — transação curta, sem chamadas externas.
    with session_scope() as session:
        user = session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            raise CheckoutError("Envie /start para iniciar o atendimento.")
        if user.is_blocked:
            raise CheckoutError("Sua conta está bloqueada. Fale com o suporte.")
        plan = session.get(Plan, plan_id)
        if plan is None or not plan.is_active:
            raise CheckoutError("Esta oferta não está mais disponível.")

        coupon = None
        amount = plan.price
        if coupon_code:
            try:
                coupon = sales_service.find_coupon(session, coupon_code, plan.id, now)
            except sales_service.CouponError as exc:
                raise CouponRejected(str(exc)) from exc
            amount = sales_service.discounted(plan.price, coupon.percent_off)

        # Reaproveita uma cobrança pendente ainda válida do mesmo plano.
        existing = session.execute(
            select(Order, Payment)
            .join(Payment, Payment.order_id == Order.id)
            .where(
                Order.user_id == user.id,
                Order.plan_id == plan.id,
                Order.status == OrderStatus.PENDING,
                Order.amount == amount,
                (Order.coupon_id == coupon.id) if coupon else Order.coupon_id.is_(None),
                Payment.status == PaymentStatus.PENDING,
                Payment.expires_at > now + timedelta(minutes=3),
            )
            .order_by(Order.id.desc())
            .limit(1)
        ).first()
        if existing:
            order, payment = existing
            return _result_from(order, plan, payment, reused=True)

        recent = session.scalar(
            select(func.count(Order.id)).where(
                Order.user_id == user.id, Order.created_at > now - timedelta(minutes=10)
            )
        )
        if recent >= settings.max_orders_per_10min:
            raise CheckoutError("Muitas tentativas em pouco tempo. Aguarde alguns minutos e tente novamente.")

        order = Order(
            user_id=user.id,
            plan_id=plan.id,
            amount=amount,
            coupon_id=coupon.id if coupon else None,
            currency=plan.currency,
            status=OrderStatus.PENDING,
            expires_at=now + timedelta(minutes=settings.pix_expiration_minutes),
        )
        session.add(order)
        session.flush()
        log_event(session, "order_created", user_id=user.id, order_id=order.id, plan=plan.slug, amount=str(amount),
                  coupon=coupon.code if coupon else None)
        order_id, order_public_id = order.id, str(order.public_id)
        amount, expires_at = order.amount, order.expires_at
        description = f"{plan.name} - pedido {order.id}" + (f" - cupom {coupon.code}" if coupon else "")
        payer_name = user.first_name

    # 2) Gera a cobrança no provedor (fora da transação).
    try:
        charge = client.create_pix_payment(
            idempotency_key=order_public_id,
            amount=amount,
            description=description,
            external_reference=order_public_id,
            payer_email=f"tg{telegram_id}@{settings.payer_email_domain}",
            payer_first_name=payer_name,
            expires_at=expires_at,
            notification_url=settings.webhook_url,
        )
    except PaymentProviderError as exc:
        with session_scope() as session:
            order = session.get(Order, order_id, with_for_update=True)
            if order.status == OrderStatus.PENDING:
                order.status = OrderStatus.FAILED
            log_event(session, "charge_failed", user_id=order.user_id, order_id=order_id, error=str(exc))
        logger.error("Falha ao gerar PIX do pedido %s: %s", order_id, exc)
        raise CheckoutError("Não foi possível gerar o PIX agora. Tente novamente em instantes.") from exc

    if charge.amount != amount:
        # Nunca deveria ocorrer; registra para auditoria.
        logger.error("Valor da cobrança (%s) difere do pedido (%s) — pedido %s", charge.amount, amount, order_id)

    # 3) Salva os dados da cobrança.
    with session_scope() as session:
        order = session.get(Order, order_id)
        plan = session.get(Plan, order.plan_id)
        payment = session.scalar(
            select(Payment).where(
                Payment.provider == charge.provider, Payment.provider_payment_id == charge.provider_payment_id
            )
        )
        if payment is None:  # pode já existir se um webhook chegou antes
            payment = Payment(
                order_id=order.id,
                provider=charge.provider,
                provider_payment_id=charge.provider_payment_id,
                status=PaymentStatus.PENDING,
                amount=charge.amount,
                currency=charge.currency,
            )
            session.add(payment)
        payment.status_detail = charge.status_detail
        payment.pix_qr_code = charge.qr_code
        payment.pix_qr_code_base64 = charge.qr_code_base64
        payment.pix_ticket_url = charge.ticket_url
        payment.expires_at = charge.expires_at
        payment.raw_response = charge.raw
        session.flush()
        log_event(session, "charge_created", user_id=order.user_id, order_id=order.id,
                  provider_payment_id=charge.provider_payment_id)
        return _result_from(order, plan, payment, reused=False)


def cancel_pending_order(telegram_id: int, order_public_id: str) -> bool:
    """Cancela um pedido pendente localmente. Se o PIX for pago mesmo assim,
    o pagamento ainda será reconhecido (ver apply_provider_payment)."""
    try:
        public_id = uuid.UUID(order_public_id)
    except ValueError:
        return False
    with session_scope() as session:
        order = session.scalar(
            select(Order)
            .join(User, User.id == Order.user_id)
            .where(Order.public_id == public_id, User.telegram_id == telegram_id)
            .with_for_update(of=Order)
        )
        if order is None or order.status != OrderStatus.PENDING:
            return False
        order.status = OrderStatus.CANCELLED
        log_event(session, "order_cancelled_by_user", user_id=order.user_id, order_id=order.id)
        return True


# --------------------------------------------------------------------------- #
# Aplicação do estado REAL do pagamento (fonte: API do provedor)
# --------------------------------------------------------------------------- #
def apply_provider_payment(session: Session, pp: ProviderPayment) -> ApplyResult:
    """Aplica o estado de um pagamento consultado na API do provedor.

    Deve ser executado dentro de uma transação (o chamador faz commit).
    """
    try:
        order_public_id = uuid.UUID(str(pp.external_reference))
    except (ValueError, TypeError):
        logger.warning("Pagamento %s sem external_reference válido — ignorado", pp.provider_payment_id)
        return ApplyResult.IGNORED

    # Ordem de bloqueio fixa (pedido -> pagamento) evita deadlocks.
    order = session.scalar(select(Order).where(Order.public_id == order_public_id).with_for_update())
    if order is None:
        logger.warning("Pagamento %s referencia pedido inexistente %s", pp.provider_payment_id, order_public_id)
        return ApplyResult.IGNORED

    payment = session.scalar(
        select(Payment)
        .where(Payment.provider == pp.provider, Payment.provider_payment_id == pp.provider_payment_id)
        .with_for_update()
    )
    if payment is None:
        payment = Payment(
            order_id=order.id,
            provider=pp.provider,
            provider_payment_id=pp.provider_payment_id,
            status=PaymentStatus.PENDING,
            amount=pp.amount,
            currency=pp.currency or order.currency,
        )
        session.add(payment)
        session.flush()
    elif payment.order_id != order.id:
        logger.error("Pagamento %s pertence a outro pedido — possível fraude", pp.provider_payment_id)
        log_event(session, "payment_order_mismatch", order_id=order.id,
                  provider_payment_id=pp.provider_payment_id)
        return ApplyResult.VALIDATION_FAILED

    payment.raw_response = pp.raw
    payment.status_detail = pp.status_detail

    # Validação do valor, moeda e método antes de qualquer liberação.
    problems = []
    if pp.amount != order.amount:
        problems.append(f"valor {pp.amount} difere do pedido {order.amount}")
    if pp.currency and pp.currency != order.currency:
        problems.append(f"moeda {pp.currency} difere do pedido {order.currency}")
    if pp.payment_method_id and pp.payment_method_id != "pix":
        problems.append(f"método {pp.payment_method_id} não é pix")
    if problems:
        payment.status = pp.status
        payment.validation_error = "; ".join(problems)
        log_event(session, "payment_validation_failed", user_id=order.user_id, order_id=order.id,
                  provider_payment_id=pp.provider_payment_id, problems=problems)
        logger.error("Pagamento %s reprovado na validação: %s", pp.provider_payment_id, problems)
        return ApplyResult.VALIDATION_FAILED

    payment.validation_error = None
    payment.status = pp.status

    if pp.status == PaymentStatus.APPROVED:
        payment.approved_at = payment.approved_at or pp.approved_at or utcnow()
        if order.status in (OrderStatus.PAID, OrderStatus.REFUNDED):
            return ApplyResult.ALREADY_PROCESSED
        # PENDING, EXPIRED, CANCELLED ou FAILED: o cliente pagou — o pagamento é honrado.
        previous = order.status
        order.status = OrderStatus.PAID
        order.paid_at = utcnow()
        log_event(session, "payment_approved", user_id=order.user_id, order_id=order.id,
                  provider_payment_id=pp.provider_payment_id, amount=str(pp.amount),
                  previous_order_status=previous.value)
        access_service.grant_access_for_order(session, order)
        return ApplyResult.GRANTED

    if pp.status in (PaymentStatus.REFUNDED, PaymentStatus.CHARGED_BACK):
        if order.status == OrderStatus.PAID:
            order.status = OrderStatus.REFUNDED
            access_service.revoke_order_access(session, order, f"pagamento {pp.status.value}")
            log_event(session, "payment_reversed", user_id=order.user_id, order_id=order.id,
                      provider_payment_id=pp.provider_payment_id, status=pp.status.value)
            return ApplyResult.REVOKED
        return ApplyResult.UPDATED

    if pp.status in (PaymentStatus.REJECTED, PaymentStatus.CANCELLED) and order.status == OrderStatus.PENDING:
        order.status = OrderStatus.CANCELLED
    return ApplyResult.UPDATED


def reconcile_order(order_public_id: str, client: PaymentClient, telegram_id: int | None = None) -> OrderStatus | None:
    """Consulta o provedor e atualiza o pedido. Usado pelo botão "verificar
    pagamento" e pelo job de reconciliação (caso algum webhook se perca)."""
    try:
        public_id = uuid.UUID(order_public_id)
    except ValueError:
        return None
    with session_scope() as session:
        stmt = select(Order).where(Order.public_id == public_id)
        if telegram_id is not None:
            stmt = stmt.join(User, User.id == Order.user_id).where(User.telegram_id == telegram_id)
        order = session.scalar(stmt)
        if order is None:
            return None
        provider_ids = session.scalars(
            select(Payment.provider_payment_id).where(Payment.order_id == order.id, Payment.provider == MERCADOPAGO)
        ).all()
        status = order.status
    for provider_payment_id in provider_ids:
        try:
            pp = client.get_payment(provider_payment_id)
        except PaymentNotFound:
            continue
        with session_scope() as session:
            apply_provider_payment(session, pp)
    with session_scope() as session:
        status = session.scalar(select(Order.status).where(Order.public_id == public_id))
    return status


def reconcile_pending_orders(client: PaymentClient, max_age_hours: int = 48) -> int:
    """Revisa pedidos pendentes recentes e expira os vencidos."""
    if not client.configured:
        return 0
    now = utcnow()
    with session_scope() as session:
        public_ids = session.scalars(
            select(Order.public_id)
            .where(
                Order.status.in_([OrderStatus.PENDING, OrderStatus.CANCELLED, OrderStatus.EXPIRED]),
                Order.created_at > now - timedelta(hours=max_age_hours),
                Order.updated_at < now - timedelta(seconds=60),
            )
            .order_by(Order.id)
            .limit(100)
        ).all()
    changed = 0
    for public_id in public_ids:
        try:
            status = reconcile_order(str(public_id), client)
            if status == OrderStatus.PAID:
                changed += 1
        except PaymentProviderError as exc:
            logger.warning("Reconciliação do pedido %s falhou: %s", public_id, exc)
    # Expira pedidos pendentes cujo PIX venceu (pagamento tardio ainda é honrado).
    with session_scope() as session:
        expired = session.scalars(
            select(Order).where(Order.status == OrderStatus.PENDING, Order.expires_at < now).with_for_update(skip_locked=True)
        ).all()
        for order in expired:
            order.status = OrderStatus.EXPIRED
    return changed


# --------------------------------------------------------------------------- #
# Webhook do Mercado Pago
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class WebhookResponse:
    status_code: int
    message: str


def _event_key(request_id: str | None, query: Mapping[str, str], body: bytes) -> str:
    if request_id:
        return request_id[:128]
    digest = hashlib.sha256(json.dumps(dict(query), sort_keys=True).encode() + b"|" + body).hexdigest()
    return f"sha256:{digest}"


def _finish_event(event_id: int, status: WebhookStatus, result: str | None = None, error: str | None = None) -> None:
    with session_scope() as session:
        event = session.get(WebhookEvent, event_id)
        event.status = status
        event.result = result
        event.error = error[:2000] if error else None
        event.processed_at = utcnow()


def handle_mercadopago_webhook(
    *, headers: Mapping[str, str], query: Mapping[str, str], body: bytes, client: PaymentClient
) -> WebhookResponse:
    try:
        payload: dict[str, Any] = json.loads(body) if body else {}
        if not isinstance(payload, dict):
            payload = {}
    except ValueError:
        payload = {}

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    data_id = query.get("data.id") or (str(data.get("id")) if data.get("id") is not None else None) or query.get("id")
    event_type = query.get("type") or payload.get("type") or query.get("topic") or payload.get("topic")
    x_signature = headers.get("x-signature")
    x_request_id = headers.get("x-request-id")

    # 1) Autentica ANTES de gravar qualquer coisa: requisições sem assinatura
    #    válida não criam linhas no banco (evita encher webhook_events de lixo).
    if not settings.mp_webhook_secret:
        logger.error("Webhook recebido mas MP_WEBHOOK_SECRET não está configurado")
        return WebhookResponse(503, "webhook not configured")
    if not verify_mercadopago_signature(
        x_signature=x_signature, x_request_id=x_request_id, data_id=data_id, secret=settings.mp_webhook_secret
    ):
        logger.warning("Webhook com assinatura inválida (request-id=%s)", (x_request_id or "")[:64])
        return WebhookResponse(401, "invalid signature")
    # Obs.: não rejeitamos por "ts" antigo. Uma notificação repetida só faz o sistema
    # consultar de novo o estado REAL na API do Mercado Pago — é inofensiva — e
    # recusar reenvios legítimos do provedor poderia atrasar liberações.

    event_key = _event_key(x_request_id, query, body)

    # 2) Registro do evento (idempotente).
    with session_scope() as session:
        inserted_id = session.scalar(
            pg_insert(WebhookEvent)
            .values(
                provider=MERCADOPAGO,
                event_key=event_key,
                event_type=(str(event_type)[:64] if event_type else None),
                resource_id=(str(data_id)[:64] if data_id else None),
                signature_valid=True,
                status=WebhookStatus.RECEIVED,
                payload={"query": dict(query), "body": payload},
            )
            .on_conflict_do_nothing(index_elements=["provider", "event_key"])
            .returning(WebhookEvent.id)
        )
        if inserted_id is None:
            event = session.scalar(
                select(WebhookEvent)
                .where(WebhookEvent.provider == MERCADOPAGO, WebhookEvent.event_key == event_key)
                .with_for_update()
            )
            if event.status in (WebhookStatus.PROCESSED, WebhookStatus.IGNORED):
                return WebhookResponse(200, "duplicate")
            event.attempts += 1
            event.signature_valid = True
            event.status = WebhookStatus.RECEIVED
            event_id = event.id
        else:
            event_id = inserted_id

    if event_type != "payment" or not data_id:
        _finish_event(event_id, WebhookStatus.IGNORED, result=f"tipo {event_type!r} ignorado")
        return WebhookResponse(200, "ignored")

    # Fonte de verdade: consulta à API do provedor.
    try:
        pp = client.get_payment(str(data_id))
    except PaymentNotFound:
        _finish_event(event_id, WebhookStatus.IGNORED, result="pagamento não encontrado no provedor")
        return WebhookResponse(200, "not found")
    except PaymentProviderError as exc:
        _finish_event(event_id, WebhookStatus.FAILED, error=str(exc))
        return WebhookResponse(502, "provider error")  # provedor reenviará

    try:
        with session_scope() as session:
            result = apply_provider_payment(session, pp)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Erro ao aplicar pagamento %s", data_id)
        _finish_event(event_id, WebhookStatus.FAILED, error=f"{exc.__class__.__name__}: {exc}")
        return WebhookResponse(500, "processing error")

    final = WebhookStatus.IGNORED if result == ApplyResult.IGNORED else WebhookStatus.PROCESSED
    _finish_event(event_id, final, result=result.value)
    logger.info("Webhook processado: pagamento=%s resultado=%s", data_id, result.value)
    return WebhookResponse(200, result.value)
