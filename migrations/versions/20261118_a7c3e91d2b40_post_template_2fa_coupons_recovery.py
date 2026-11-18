"""modelo de post (chamada/cupom), 2FA, cupons de desconto, recuperação de PIX e upsell

Revision ID: a7c3e91d2b40
Revises: fd1c09ce71da
Create Date: 2026-11-18 10:00:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a7c3e91d2b40"
down_revision: Union[str, None] = "fd1c09ce71da"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Ofertas: chamada e cupom usados no modelo do post
    op.add_column("promo_offers", sa.Column("headline", sa.String(length=120), nullable=True))
    op.add_column("promo_offers", sa.Column("coupon", sa.String(length=40), nullable=True))

    # Verificação em duas etapas dos administradores
    op.add_column("admin_users", sa.Column("totp_secret", sa.Text(), nullable=True))
    op.add_column("admin_users", sa.Column("totp_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.add_column("admin_users", sa.Column("totp_last_step", sa.BigInteger(), nullable=True))

    # Cupons de desconto (planos do bot)
    op.create_table(
        "coupons",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("percent_off", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=True),
        sa.Column("max_uses", sa.Integer(), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("percent_off BETWEEN 1 AND 90", name=op.f("ck_coupons_percent_range")),
        sa.CheckConstraint("max_uses IS NULL OR max_uses > 0", name=op.f("ck_coupons_max_uses_positive")),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.id"], name=op.f("fk_coupons_plan_id_plans"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_coupons")),
        sa.UniqueConstraint("code", name=op.f("uq_coupons_code")),
    )

    # Pedidos: cupom, recuperação de PIX expirado e upsell
    op.add_column("orders", sa.Column("coupon_id", sa.Integer(), nullable=True))
    op.add_column("orders", sa.Column("recovery_sent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", sa.Column("upsell_sent_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(op.f("fk_orders_coupon_id_coupons"), "orders", "coupons", ["coupon_id"], ["id"],
                          ondelete="SET NULL")
    op.create_index("ix_orders_coupon_id", "orders", ["coupon_id"], unique=False)
    op.create_index("ix_orders_recovery_candidates", "orders", ["expires_at"], unique=False,
                    postgresql_where=sa.text("status = 'expired' AND recovery_sent_at IS NULL"))
    # Pedidos que já expiraram antes desta versão não recebem a mensagem de recuperação.
    op.execute("UPDATE orders SET recovery_sent_at = now() WHERE status = 'expired'")
    # Pedidos já entregues não recebem upsell retroativo.
    op.execute("UPDATE orders SET upsell_sent_at = now() WHERE delivered_at IS NOT NULL")


def downgrade() -> None:
    op.drop_index("ix_orders_recovery_candidates", table_name="orders",
                  postgresql_where=sa.text("status = 'expired' AND recovery_sent_at IS NULL"))
    op.drop_index("ix_orders_coupon_id", table_name="orders")
    op.drop_constraint(op.f("fk_orders_coupon_id_coupons"), "orders", type_="foreignkey")
    op.drop_column("orders", "upsell_sent_at")
    op.drop_column("orders", "recovery_sent_at")
    op.drop_column("orders", "coupon_id")
    op.drop_table("coupons")
    op.drop_column("admin_users", "totp_last_step")
    op.drop_column("admin_users", "totp_enabled")
    op.drop_column("admin_users", "totp_secret")
    op.drop_column("promo_offers", "coupon")
    op.drop_column("promo_offers", "headline")
