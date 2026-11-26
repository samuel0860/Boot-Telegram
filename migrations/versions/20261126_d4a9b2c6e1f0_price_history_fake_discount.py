"""ofertas: histórico de preço e detector de desconto falso

Revision ID: d4a9b2c6e1f0
Revises: c8e2f4a1b7d3
Create Date: 2026-11-26 14:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4a9b2c6e1f0"
down_revision: str | None = "c8e2f4a1b7d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("promo_offers", sa.Column("price_note", sa.String(length=200), nullable=True))
    op.create_table(
        "promo_price_history",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("offer_id", sa.BigInteger(), nullable=False),
        sa.Column("price", sa.Numeric(12, 2), nullable=False),
        sa.Column("original_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_promo_price_history")),
        sa.ForeignKeyConstraint(["offer_id"], ["promo_offers.id"], ondelete="CASCADE",
                                name=op.f("fk_promo_price_history_offer_id_promo_offers")),
    )
    op.create_index("ix_promo_price_history_offer_id_seen_at", "promo_price_history", ["offer_id", "seen_at"])


def downgrade() -> None:
    op.drop_index("ix_promo_price_history_offer_id_seen_at", table_name="promo_price_history")
    op.drop_table("promo_price_history")
    op.drop_column("promo_offers", "price_note")
