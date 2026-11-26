"""ofertas: comissão, nota e vendidos (pontuação e prova social no post)

Revision ID: f2c8d4e6a1b3
Revises: e7b1c3d5f9a2
Create Date: 2026-11-27 15:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2c8d4e6a1b3"
down_revision: str | None = "e7b1c3d5f9a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("promo_offers", sa.Column("commission_rate", sa.Numeric(6, 4), nullable=True))
    op.add_column("promo_offers", sa.Column("rating", sa.Numeric(3, 2), nullable=True))
    op.add_column("promo_offers", sa.Column("sold", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("promo_offers", "sold")
    op.drop_column("promo_offers", "rating")
    op.drop_column("promo_offers", "commission_rate")
