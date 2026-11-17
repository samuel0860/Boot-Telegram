"""Models SQLAlchemy de todas as tabelas do PostgreSQL.

Valores monetários usam NUMERIC(12, 2) (Decimal no Python) — nunca FLOAT.
Status são armazenados como VARCHAR + CHECK constraint (fácil de evoluir com
migrations, sem os problemas de ALTER TYPE de enums nativos).
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base

MONEY = Numeric(12, 2)


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class AccessType(str, enum.Enum):
    ONE_TIME = "one_time"          # pack individual (pagamento único)
    SUBSCRIPTION = "subscription"  # assinatura com validade


class ContentType(str, enum.Enum):
    TELEGRAM_CHAT = "telegram_chat"  # grupo/canal privado (content_value = chat_id)
    LINK = "link"                    # URL externa
    FILE = "file"                    # documento (content_value = file_id do Telegram)
    PHOTO = "photo"                  # foto (file_id)
    VIDEO = "video"                  # vídeo (file_id)
    TEXT = "text"                    # texto livre


class OrderStatus(str, enum.Enum):
    PENDING = "pending"
    PAID = "paid"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"
    FAILED = "failed"


class PaymentStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"
    CHARGED_BACK = "charged_back"


class SubscriptionStatus(str, enum.Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


class WebhookStatus(str, enum.Enum):
    RECEIVED = "received"
    PROCESSED = "processed"
    IGNORED = "ignored"
    FAILED = "failed"


def _enum(enum_cls: type[enum.Enum], name: str) -> Enum:
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# --------------------------------------------------------------------------- #
# Associação plano <-> produtos (um plano libera N produtos/conteúdos)
# --------------------------------------------------------------------------- #
plan_products = Table(
    "plan_products",
    Base.metadata,
    Column("plan_id", Integer, ForeignKey("plans.id", ondelete="CASCADE"), primary_key=True),
    Column("product_id", Integer, ForeignKey("products.id", ondelete="CASCADE"), primary_key=True),
    Index("ix_plan_products_product_id", "product_id"),
)


# --------------------------------------------------------------------------- #
# Tabelas
# --------------------------------------------------------------------------- #
class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    last_name: Mapped[str | None] = mapped_column(String(128))
    language_code: Mapped[str | None] = mapped_column(String(16))
    source: Mapped[str | None] = mapped_column(String(64))  # parâmetro do /start (ex.: tiktok)
    is_blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    orders: Mapped[list[Order]] = relationship(back_populates="user")
    subscriptions: Mapped[list[Subscription]] = relationship(back_populates="user")

    __table_args__ = (
        Index("ix_users_username", "username"),
        Index("ix_users_created_at", "created_at"),
    )

    @property
    def display_name(self) -> str:
        name = " ".join(p for p in (self.first_name, self.last_name) if p)
        if self.username:
            return f"{name} (@{self.username})" if name else f"@{self.username}"
        return name or str(self.telegram_id)


class Category(TimestampMixin, Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    products: Mapped[list[Product]] = relationship(back_populates="category", order_by="Product.sort_order")


class Product(TimestampMixin, Base):
    """Produto / pack / conteúdo entregue após o pagamento."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL")
    )
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)  # descrição pública (mostrada antes da compra)
    content_type: Mapped[ContentType] = mapped_column(_enum(ContentType, "content_type"), nullable=False)
    content_value: Mapped[str] = mapped_column(Text, nullable=False)  # conteúdo privado
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    category: Mapped[Category | None] = relationship(back_populates="products")
    plans: Mapped[list[Plan]] = relationship(secondary=plan_products, back_populates="products")

    __table_args__ = (
        Index("ix_products_category_id_sort_order", "category_id", "sort_order"),
        Index("ix_products_content_type_content_value", "content_type", "content_value"),
    )


class Plan(TimestampMixin, Base):
    """Plano / oferta exibida no bot."""

    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default=text("'BRL'"))
    access_type: Mapped[AccessType] = mapped_column(_enum(AccessType, "access_type"), nullable=False)
    # NULL = acesso vitalício (permitido somente para packs one_time)
    duration_days: Mapped[int | None] = mapped_column(Integer)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    products: Mapped[list[Product]] = relationship(
        secondary=plan_products, back_populates="plans", order_by="Product.sort_order"
    )

    __table_args__ = (
        CheckConstraint("price > 0", name="price_positive"),
        CheckConstraint("duration_days IS NULL OR duration_days > 0", name="duration_positive"),
        CheckConstraint(
            "access_type <> 'subscription' OR duration_days IS NOT NULL",
            name="subscription_requires_duration",
        ),
        Index("ix_plans_is_active_sort_order", "is_active", "sort_order"),
    )

    @property
    def duration_label(self) -> str:
        if not self.duration_days:
            return "acesso vitalício"
        if self.duration_days == 1:
            return "1 dia"
        if self.duration_days % 30 == 0:
            months = self.duration_days // 30
            return "1 mês" if months == 1 else f"{months} meses"
        return f"{self.duration_days} dias"


class Order(TimestampMixin, Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    public_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid.uuid4
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id", ondelete="RESTRICT"), nullable=False)
    # Snapshot do preço no momento da compra (alterar o plano não altera pedidos antigos)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default=text("'BRL'"))
    status: Mapped[OrderStatus] = mapped_column(
        _enum(OrderStatus, "order_status"), nullable=False, default=OrderStatus.PENDING
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Controle de entrega do conteúdo pelo bot (idempotente)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    delivery_error: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship(back_populates="orders")
    plan: Mapped[Plan] = relationship()
    payments: Mapped[list[Payment]] = relationship(back_populates="order", order_by="Payment.id")
    subscription: Mapped[Subscription | None] = relationship(back_populates="order", uselist=False)

    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        Index("ix_orders_user_id_status", "user_id", "status"),
        Index("ix_orders_status_created_at", "status", "created_at"),
        Index(
            "ix_orders_pending_delivery",
            "paid_at",
            postgresql_where=text("status = 'paid' AND delivered_at IS NULL"),
        ),
    )


class Payment(TimestampMixin, Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_payment_id: Mapped[str] = mapped_column(String(64), nullable=False)
    method: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'pix'"))
    status: Mapped[PaymentStatus] = mapped_column(
        _enum(PaymentStatus, "payment_status"), nullable=False, default=PaymentStatus.PENDING
    )
    status_detail: Mapped[str | None] = mapped_column(String(64))
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default=text("'BRL'"))
    pix_qr_code: Mapped[str | None] = mapped_column(Text)          # copia e cola
    pix_qr_code_base64: Mapped[str | None] = mapped_column(Text)   # imagem PNG em base64
    pix_ticket_url: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    validation_error: Mapped[str | None] = mapped_column(Text)
    raw_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    order: Mapped[Order] = relationship(back_populates="payments")

    __table_args__ = (
        # Impede registrar/processar o mesmo pagamento do provedor duas vezes
        UniqueConstraint("provider", "provider_payment_id", name="uq_payments_provider_payment"),
        Index("ix_payments_order_id", "order_id"),
        Index("ix_payments_status_created_at", "status", "created_at"),
    )


class Subscription(TimestampMixin, Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id", ondelete="RESTRICT"), nullable=False)
    # UNIQUE: um pedido gera no máximo um acesso (proteção contra liberação duplicada)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"), unique=True, nullable=False
    )
    status: Mapped[SubscriptionStatus] = mapped_column(
        _enum(SubscriptionStatus, "subscription_status"), nullable=False, default=SubscriptionStatus.ACTIVE
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # NULL = vitalício
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoke_reason: Mapped[str | None] = mapped_column(String(255))
    reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Quando o bot removeu o usuário dos grupos/canais após expirar/revogar
    access_removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="subscriptions")
    plan: Mapped[Plan] = relationship()
    order: Mapped[Order] = relationship(back_populates="subscription")

    __table_args__ = (
        CheckConstraint("expires_at IS NULL OR expires_at > starts_at", name="valid_period"),
        Index("ix_subscriptions_user_id_status", "user_id", "status"),
        Index("ix_subscriptions_status_expires_at", "status", "expires_at"),
    )

    def is_valid(self, now: datetime) -> bool:
        return (
            self.status == SubscriptionStatus.ACTIVE
            and self.starts_at <= now
            and (self.expires_at is None or self.expires_at > now)
        )


class AccessLog(Base):
    """Histórico de liberações, acessos e ações administrativas."""

    __tablename__ = "access_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    admin_user_id: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"))
    subscription_id: Mapped[int | None] = mapped_column(ForeignKey("subscriptions.id", ondelete="SET NULL"))
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    ip_address: Mapped[str | None] = mapped_column(String(45))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[User | None] = relationship()
    admin_user: Mapped[AdminUser | None] = relationship()

    __table_args__ = (
        Index("ix_access_logs_user_id_created_at", "user_id", "created_at"),
        Index("ix_access_logs_action_created_at", "action", "created_at"),
        Index("ix_access_logs_created_at", "created_at"),
    )


class WebhookEvent(Base):
    """Eventos recebidos dos provedores (auditoria + idempotência)."""

    __tablename__ = "webhook_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    event_key: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str | None] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(64))
    signature_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    status: Mapped[WebhookStatus] = mapped_column(
        _enum(WebhookStatus, "webhook_status"), nullable=False, default=WebhookStatus.RECEIVED
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    result: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("provider", "event_key", name="uq_webhook_events_provider_event_key"),
        Index("ix_webhook_events_resource_id", "resource_id"),
        Index("ix_webhook_events_status_created_at", "status", "created_at"),
    )


class AdminUser(TimestampMixin, Base):
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)  # Argon2id
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    is_superuser: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    failed_login_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --------------------------------------------------------------------------- #
# Configurações editáveis pelo painel + bot de promoções (Mercado Livre)
# --------------------------------------------------------------------------- #
class AppSetting(Base):
    """Configurações chave/valor editadas no painel. Segredos ficam criptografados."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    is_secret: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    updated_by_admin_id: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class OfferStatus(str, enum.Enum):
    NEW = "new"              # coletada, aguardando (aprovação manual ou próximo ciclo)
    QUEUED = "queued"        # na fila para o bot publicar no canal
    POSTED = "posted"
    DISCARDED = "discarded"
    FAILED = "failed"


class WaStatus(str, enum.Enum):
    NONE = "none"          # não vai para o WhatsApp
    PENDING = "pending"    # aguardando os limites de envio
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"    # expirou na fila (preço poderia estar desatualizado)


class PromoOffer(TimestampMixin, Base):
    """Ofertas coletadas na API do Mercado Livre para publicar no canal."""

    __tablename__ = "promo_offers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    product_url: Mapped[str] = mapped_column(Text, nullable=False)
    affiliate_url: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text)
    price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    original_price: Mapped[Decimal | None] = mapped_column(MONEY)
    discount_pct: Mapped[int | None] = mapped_column(Integer)
    free_shipping: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    category_id: Mapped[str | None] = mapped_column(String(32))
    source: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[OfferStatus] = mapped_column(
        _enum(OfferStatus, "offer_status"), nullable=False, default=OfferStatus.NEW
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64))
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    error: Mapped[str | None] = mapped_column(Text)
    # WhatsApp
    wa_status: Mapped[WaStatus] = mapped_column(
        _enum(WaStatus, "wa_status"), nullable=False, default=WaStatus.NONE, server_default=WaStatus.NONE.value
    )
    wa_queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    wa_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    wa_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("platform", "external_id", name="uq_promo_offers_platform_external_id"),
        Index("ix_promo_offers_wa_status_wa_queued_at", "wa_status", "wa_queued_at"),
        Index("ix_promo_offers_wa_sent_at", "wa_sent_at"),
        CheckConstraint("price > 0", name="price_positive"),
        Index("ix_promo_offers_status_discount_pct", "status", "discount_pct"),
        Index("ix_promo_offers_status_queued_at", "status", "queued_at"),
        Index("ix_promo_offers_posted_at", "posted_at"),
    )

    @property
    def link(self) -> str:
        return self.affiliate_url or self.product_url
