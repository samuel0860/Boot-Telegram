"""Gateway de pagamento do MÓDULO DE VENDAS (hoje: Mercado Pago — PIX).

O dinheiro cai direto na conta do gateway configurado (sua conta), sem
intermediários. O gateway é escolhido por PAYMENT_GATEWAY no .env.

Para adicionar outro gateway (ex.: Asaas, Efí, PagBank):
1. crie uma classe com a interface `PaymentClient` (create_pix_payment / get_payment),
   devolvendo PixCharge / ProviderPayment;
2. registre-a em GATEWAYS abaixo;
3. crie a rota de webhook dele em app.py, validando a assinatura do provedor e
   chamando payment_service.apply_provider_payment com o pagamento CONSULTADO na API.

Este módulo apenas conversa com a API do provedor. Regras de negócio
(pedidos, validação de valores, liberação de acesso) ficam em
services/payment_service.py.

Documentação oficial:
- Criar pagamento PIX: POST /v1/payments  (payment_method_id = "pix")
- Consultar pagamento: GET /v1/payments/{id}
- Assinatura de webhooks: header x-signature (HMAC-SHA256)
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

import httpx

from config import settings
from models import PaymentStatus

logger = logging.getLogger(__name__)

MERCADOPAGO = "mercadopago"
CENT = Decimal("0.01")

# Mapeamento dos status do Mercado Pago para os status internos.
MP_STATUS_MAP: dict[str, PaymentStatus] = {
    "pending": PaymentStatus.PENDING,
    "in_process": PaymentStatus.PENDING,
    "authorized": PaymentStatus.PENDING,
    "in_mediation": PaymentStatus.PENDING,
    "approved": PaymentStatus.APPROVED,
    "rejected": PaymentStatus.REJECTED,
    "cancelled": PaymentStatus.CANCELLED,
    "refunded": PaymentStatus.REFUNDED,
    "charged_back": PaymentStatus.CHARGED_BACK,
}


class PaymentProviderError(Exception):
    """Erro de comunicação ou resposta inválida do provedor."""


class PaymentNotFound(PaymentProviderError):
    """O pagamento consultado não existe no provedor."""


class PaymentsNotConfigured(PaymentProviderError):
    """Credenciais do provedor não configuradas."""


@dataclass(frozen=True)
class PixCharge:
    provider: str
    provider_payment_id: str
    status: PaymentStatus
    status_detail: str | None
    amount: Decimal
    currency: str
    qr_code: str
    qr_code_base64: str | None
    ticket_url: str | None
    expires_at: datetime | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class ProviderPayment:
    """Estado de um pagamento CONSULTADO diretamente na API do provedor."""

    provider: str
    provider_payment_id: str
    status: PaymentStatus
    status_raw: str
    status_detail: str | None
    amount: Decimal
    currency: str
    external_reference: str | None
    payment_method_id: str | None
    approved_at: datetime | None
    raw: dict[str, Any]


class PaymentClient(Protocol):
    provider_name: str

    @property
    def configured(self) -> bool: ...

    def create_pix_payment(
        self,
        *,
        idempotency_key: str,
        amount: Decimal,
        description: str,
        external_reference: str,
        payer_email: str,
        payer_first_name: str | None,
        expires_at: datetime,
        notification_url: str | None,
    ) -> PixCharge: ...

    def get_payment(self, payment_id: str) -> ProviderPayment: ...


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_amount(value: Any) -> Decimal:
    try:
        return Decimal(str(value)).quantize(CENT)
    except (InvalidOperation, TypeError) as exc:
        raise PaymentProviderError(f"Valor inválido retornado pelo provedor: {value!r}") from exc


class MercadoPagoClient:
    provider_name = MERCADOPAGO

    def __init__(self, access_token: str, api_base: str = "https://api.mercadopago.com", timeout: float = 20.0):
        self._token = access_token
        self._api_base = api_base.rstrip("/")
        self._timeout = timeout
        self._http: httpx.Client | None = None
        self._lock = threading.Lock()

    def _client(self) -> httpx.Client:
        """Cliente HTTP reaproveitado (mantém a conexão TLS aberta entre chamadas).
        httpx.Client é seguro para uso por várias threads."""
        if self._http is None:
            with self._lock:
                if self._http is None:
                    self._http = httpx.Client(timeout=self._timeout,
                                              limits=httpx.Limits(max_connections=10, max_keepalive_connections=5))
        return self._http

    @property
    def configured(self) -> bool:
        return bool(self._token)

    def _headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        if not self._token:
            raise PaymentsNotConfigured("MP_ACCESS_TOKEN não configurado")
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if idempotency_key:
            headers["X-Idempotency-Key"] = idempotency_key
        return headers

    def _request(self, method: str, path: str, *, json: dict | None = None, idempotency_key: str | None = None) -> dict:
        url = f"{self._api_base}{path}"
        try:
            response = self._client().request(method, url, json=json, headers=self._headers(idempotency_key))
        except httpx.HTTPError as exc:
            raise PaymentProviderError(f"Falha de comunicação com o Mercado Pago: {exc.__class__.__name__}") from exc

        if response.status_code == 404:
            raise PaymentNotFound(f"Recurso não encontrado: {path}")
        if response.status_code >= 400:
            # Não loga headers (contém o token). Corpo é útil para diagnóstico.
            logger.error("Mercado Pago %s %s -> %s: %s", method, path, response.status_code, response.text[:1000])
            raise PaymentProviderError(f"Mercado Pago retornou HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise PaymentProviderError("Resposta inválida (não JSON) do Mercado Pago") from exc

    def create_pix_payment(
        self,
        *,
        idempotency_key: str,
        amount: Decimal,
        description: str,
        external_reference: str,
        payer_email: str,
        payer_first_name: str | None,
        expires_at: datetime,
        notification_url: str | None,
    ) -> PixCharge:
        body: dict[str, Any] = {
            # A API espera número; o valor já está quantizado em 2 casas.
            "transaction_amount": float(amount.quantize(CENT)),
            "description": description[:255],
            "payment_method_id": "pix",
            "external_reference": external_reference,
            "date_of_expiration": expires_at.isoformat(timespec="milliseconds"),
            "payer": {"email": payer_email},
        }
        if payer_first_name:
            body["payer"]["first_name"] = payer_first_name[:60]
        if notification_url:
            body["notification_url"] = notification_url

        data = self._request("POST", "/v1/payments", json=body, idempotency_key=idempotency_key)
        tx = (data.get("point_of_interaction") or {}).get("transaction_data") or {}
        qr_code = tx.get("qr_code")
        if not data.get("id") or not qr_code:
            raise PaymentProviderError("Resposta do Mercado Pago sem id ou QR Code PIX")

        status_raw = str(data.get("status") or "pending")
        return PixCharge(
            provider=self.provider_name,
            provider_payment_id=str(data["id"]),
            status=MP_STATUS_MAP.get(status_raw, PaymentStatus.PENDING),
            status_detail=data.get("status_detail"),
            amount=_parse_amount(data.get("transaction_amount")),
            currency=str(data.get("currency_id") or settings.currency),
            qr_code=qr_code,
            qr_code_base64=tx.get("qr_code_base64"),
            ticket_url=tx.get("ticket_url"),
            expires_at=_parse_dt(data.get("date_of_expiration")) or expires_at,
            raw=_sanitize(data),
        )

    def get_payment(self, payment_id: str) -> ProviderPayment:
        if not str(payment_id).isdigit():
            raise PaymentNotFound("ID de pagamento inválido")
        data = self._request("GET", f"/v1/payments/{payment_id}")
        status_raw = str(data.get("status") or "")
        status = MP_STATUS_MAP.get(status_raw)
        if status is None:
            raise PaymentProviderError(f"Status desconhecido do Mercado Pago: {status_raw!r}")
        return ProviderPayment(
            provider=self.provider_name,
            provider_payment_id=str(data.get("id")),
            status=status,
            status_raw=status_raw,
            status_detail=data.get("status_detail"),
            amount=_parse_amount(data.get("transaction_amount")),
            currency=str(data.get("currency_id") or ""),
            external_reference=data.get("external_reference"),
            payment_method_id=data.get("payment_method_id"),
            approved_at=_parse_dt(data.get("date_approved")),
            raw=_sanitize(data),
        )


def _sanitize(data: dict[str, Any]) -> dict[str, Any]:
    """Remove campos volumosos/sensíveis antes de salvar no banco."""
    keep = {
        "id", "status", "status_detail", "transaction_amount", "currency_id", "external_reference",
        "payment_method_id", "payment_type_id", "date_created", "date_approved", "date_last_updated",
        "date_of_expiration", "live_mode", "description",
    }
    return {k: v for k, v in data.items() if k in keep}


# --------------------------------------------------------------------------- #
# Validação da assinatura dos webhooks
# --------------------------------------------------------------------------- #
def parse_signature_header(x_signature: str | None) -> tuple[str | None, str | None]:
    """Extrai ts e v1 do header 'x-signature: ts=...,v1=...'."""
    if not x_signature:
        return None, None
    ts = v1 = None
    for part in x_signature.split(","):
        key, _, value = part.strip().partition("=")
        if key == "ts":
            ts = value.strip()
        elif key == "v1":
            v1 = value.strip()
    return ts, v1


def build_signature_manifest(data_id: str | None, request_id: str | None, ts: str) -> str:
    manifest = ""
    if data_id:
        data_id = str(data_id)
        manifest += f"id:{data_id.lower() if data_id.isalnum() else data_id};"
    if request_id:
        manifest += f"request-id:{request_id};"
    manifest += f"ts:{ts};"
    return manifest


def verify_mercadopago_signature(
    *, x_signature: str | None, x_request_id: str | None, data_id: str | None, secret: str
) -> bool:
    """Valida o HMAC-SHA256 enviado pelo Mercado Pago (comparação em tempo constante)."""
    if not secret:
        return False
    ts, v1 = parse_signature_header(x_signature)
    if not ts or not v1:
        return False
    manifest = build_signature_manifest(data_id, x_request_id, ts)
    expected = hmac.new(secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, v1.lower())


# Gateways disponíveis: nome no .env (PAYMENT_GATEWAY) -> fábrica do cliente
GATEWAYS = {
    MERCADOPAGO: lambda: MercadoPagoClient(settings.mp_access_token, settings.mp_api_base),
}

_client: PaymentClient | None = None


def get_payment_client() -> PaymentClient:
    global _client
    if _client is None:
        factory = GATEWAYS.get(settings.payment_gateway)
        if factory is None:
            raise PaymentsNotConfigured(
                f"Gateway '{settings.payment_gateway}' não suportado. Disponíveis: {', '.join(GATEWAYS)}")
        _client = factory()
    return _client
