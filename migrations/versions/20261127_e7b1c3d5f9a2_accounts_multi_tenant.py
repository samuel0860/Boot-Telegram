"""plataforma: várias contas (clientes), cada uma com os próprios dados

- cria `accounts` e a conta 1 ("Conta principal", da plataforma) com TODOS os dados atuais;
- `account_id` em todas as tabelas de negócio;
- únicos passam a valer por conta (slug, código de cupom, telegram_id, oferta da loja);
- `app_settings` passa a ser por conta; o sinal de vida dos processos vai para `platform_settings`.

Revision ID: e7b1c3d5f9a2
Revises: d4a9b2c6e1f0
Create Date: 2026-11-27 09:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e7b1c3d5f9a2"
down_revision: str | None = "d4a9b2c6e1f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# tabela -> account_id obrigatório?
TABLES = {
    "users": True, "categories": True, "products": True, "plans": True, "orders": True, "coupons": True,
    "payments": True, "subscriptions": True, "access_logs": False, "webhook_events": False,
    "admin_users": True, "app_settings": True, "offer_bots": True, "offer_bot_targets": True,
    "promo_offers": True, "promo_price_history": True, "offer_posts": True,
}
# (tabela, único antigo, novo nome, colunas novas)
UNIQUES = [
    ("users", "uq_users_telegram_id", "uq_users_account_telegram", ["account_id", "telegram_id"], ["telegram_id"]),
    ("categories", "uq_categories_slug", "uq_categories_account_slug", ["account_id", "slug"], ["slug"]),
    ("products", "uq_products_slug", "uq_products_account_slug", ["account_id", "slug"], ["slug"]),
    ("plans", "uq_plans_slug", "uq_plans_account_slug", ["account_id", "slug"], ["slug"]),
    ("coupons", "uq_coupons_code", "uq_coupons_account_code", ["account_id", "code"], ["code"]),
    ("promo_offers", "uq_promo_offers_platform_external_id", "uq_promo_offers_account_platform_external",
     ["account_id", "platform", "external_id"], ["platform", "external_id"]),
]
PLATFORM_KEYS = ("offers_heartbeat_at", "bot_heartbeat_at")


def upgrade() -> None:
    status = postgresql.ENUM("active", "suspended", "canceled", name="account_status")
    status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("plan", sa.String(length=32), server_default="pro", nullable=False),
        sa.Column("status", postgresql.ENUM(name="account_status", create_type=False), server_default="active",
                  nullable=False),
        sa.Column("is_platform", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("kiwify_subscription_id", sa.String(length=80), nullable=True),
        sa.Column("kiwify_order_id", sa.String(length=80), nullable=True),
        sa.Column("paid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_accounts")),
    )
    op.create_index(op.f("ix_accounts_email"), "accounts", ["email"])
    op.create_index(op.f("ix_accounts_kiwify_subscription_id"), "accounts", ["kiwify_subscription_id"])
    op.execute("INSERT INTO accounts (id, name, plan, is_platform) VALUES (1, 'Conta principal', 'agencia', true)")
    op.execute("SELECT setval(pg_get_serial_sequence('accounts', 'id'), 1)")

    for table, required in TABLES.items():
        op.add_column(table, sa.Column("account_id", sa.Integer(), nullable=True))
        op.execute(f"UPDATE {table} SET account_id = 1")
        if required:
            op.alter_column(table, "account_id", nullable=False)
        op.create_foreign_key(op.f(f"fk_{table}_account_id_accounts"), table, "accounts", ["account_id"], ["id"],
                              ondelete="CASCADE")
        op.create_index(op.f(f"ix_{table}_account_id"), table, ["account_id"])

    for table, old, new, cols, _ in UNIQUES:
        op.drop_constraint(old, table, type_="unique")
        op.create_unique_constraint(new, table, cols)

    op.create_table(
        "platform_settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_platform_settings")),
    )
    keys = ", ".join(f"'{k}'" for k in PLATFORM_KEYS)
    op.execute(f"INSERT INTO platform_settings (key, value) SELECT key, value FROM app_settings WHERE key IN ({keys})")
    op.execute(f"DELETE FROM app_settings WHERE key IN ({keys})")
    op.drop_constraint("pk_app_settings", "app_settings", type_="primary")
    op.create_primary_key("pk_app_settings", "app_settings", ["account_id", "key"])


def downgrade() -> None:
    op.drop_constraint("pk_app_settings", "app_settings", type_="primary")
    op.execute("DELETE FROM app_settings WHERE account_id <> 1")
    op.create_primary_key("pk_app_settings", "app_settings", ["key"])
    op.execute("INSERT INTO app_settings (key, value, is_secret) SELECT key, value, false FROM platform_settings "
               "ON CONFLICT DO NOTHING")
    op.drop_table("platform_settings")
    for table, old, new, _, cols in UNIQUES:
        op.drop_constraint(new, table, type_="unique")
        op.create_unique_constraint(old, table, cols)
    for table in TABLES:
        op.drop_index(op.f(f"ix_{table}_account_id"), table_name=table)
        op.drop_constraint(op.f(f"fk_{table}_account_id_accounts"), table, type_="foreignkey")
        op.drop_column(table, "account_id")
    op.drop_index(op.f("ix_accounts_kiwify_subscription_id"), table_name="accounts")
    op.drop_index(op.f("ix_accounts_email"), table_name="accounts")
    op.drop_table("accounts")
    postgresql.ENUM(name="account_status").drop(op.get_bind(), checkfirst=True)
