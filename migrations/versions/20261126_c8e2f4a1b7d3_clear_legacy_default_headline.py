"""ofertas: chamada automática — limpa a chamada fixa antiga "PRECIN NO MERCADO LIVRE"

Esse texto era o padrão antigo e ficou gravado como chamada FIXA nos bots que salvaram o
modelo antes da chamada automática existir; com ele, até ofertas da Shopee/Amazon saíam
"no Mercado Livre". Vazio = chamada automática (produto + desconto).

Revision ID: c8e2f4a1b7d3
Revises: b51d0c7e9a22
Create Date: 2026-11-26 10:00:00

"""
from collections.abc import Sequence

from alembic import op

revision: str = "c8e2f4a1b7d3"
down_revision: str | None = "b51d0c7e9a22"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEGACY = "PRECIN NO MERCADO LIVRE"


def upgrade() -> None:
    op.execute(
        "UPDATE offer_bots SET config = jsonb_set(config, '{default_headline}', '\"\"') "
        f"WHERE upper(trim(config->>'default_headline')) = '{LEGACY}'"
    )


def downgrade() -> None:
    pass  # não há como saber quais bots tinham o texto antigo
