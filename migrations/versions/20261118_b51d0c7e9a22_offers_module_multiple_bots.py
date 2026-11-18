"""módulo de ofertas: vários bots (token, WhatsApp e destinos próprios) e postagens por bot

- cria offer_bots, offer_bot_targets e offer_posts;
- migra a configuração antiga do "bot de promoções" (app_settings promo_* / wa_*)
  para o primeiro bot de ofertas, mantendo a instância do WhatsApp já conectada;
- move o estado de publicação de promo_offers para offer_posts.

Revision ID: b51d0c7e9a22
Revises: a7c3e91d2b40
Create Date: 2026-11-18 14:30:00

"""
import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b51d0c7e9a22"
down_revision: str | None = "a7c3e91d2b40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OFFER_STATUS = ("new", "queued", "posted", "discarded", "failed")
WA_STATUS = ("none", "pending", "sent", "failed", "skipped")

# app_settings antigas -> chave no JSON de configuração do bot
CONFIG_KEYS = {
    "promo_mode": "mode", "promo_interval_minutes": "interval_minutes",
    "promo_max_posts_per_cycle": "max_posts_per_cycle", "promo_spacing_seconds": "spacing_seconds",
    "promo_min_discount": "min_discount", "promo_min_price": "min_price", "promo_max_price": "max_price",
    "promo_blocked_words": "blocked_words", "promo_categories": "categories", "promo_keywords": "keywords",
    "promo_no_repeat_days": "no_repeat_days", "promo_active_hours": "active_hours", "promo_footer": "footer",
    "promo_template": "template", "promo_default_headline": "default_headline",
    "wa_enabled": "wa_enabled", "wa_warmup_days": "wa_warmup_days", "wa_warmup_per_day": "wa_warmup_per_day",
    "wa_max_per_window": "wa_max_per_window", "wa_window_hours": "wa_window_hours",
    "wa_max_per_day": "wa_max_per_day", "wa_group_delay_seconds": "wa_group_delay_seconds",
    "wa_max_age_hours": "wa_max_age_hours",
}
OLD_KEYS = list(CONFIG_KEYS) + ["promo_enabled", "promo_channel_id", "wa_instance", "wa_groups", "wa_started_at",
                                "promo_last_run_at", "promo_last_result"]


def upgrade() -> None:
    op.create_table(
        "offer_bots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("telegram_token", sa.Text(), nullable=True),
        sa.Column("telegram_username", sa.String(length=64), nullable=True),
        sa.Column("telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("wa_instance", sa.String(length=40), nullable=True),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"),
                  nullable=False),
        sa.Column("state", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"),
                  nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_offer_bots")),
        sa.UniqueConstraint("wa_instance", name=op.f("uq_offer_bots_wa_instance")),
    )
    op.create_table(
        "offer_bot_targets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("bot_id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.Enum("telegram", "whatsapp", name="target_channel", native_enum=False,
                                     create_constraint=True, length=32), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["bot_id"], ["offer_bots.id"], name=op.f("fk_offer_bot_targets_bot_id_offer_bots"),
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_offer_bot_targets")),
        sa.UniqueConstraint("bot_id", "channel", "target_id", name="uq_offer_bot_targets_bot_channel_target"),
    )
    op.create_table(
        "offer_posts",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("offer_id", sa.BigInteger(), nullable=False),
        sa.Column("bot_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.Enum(*OFFER_STATUS, name="offer_status", native_enum=False, create_constraint=True,
                                    length=32), nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tg_messages", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("wa_status", sa.Enum(*WA_STATUS, name="wa_status", native_enum=False, create_constraint=True,
                                       length=32), server_default="none", nullable=False),
        sa.Column("wa_queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("wa_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("wa_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["bot_id"], ["offer_bots.id"], name=op.f("fk_offer_posts_bot_id_offer_bots"),
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["offer_id"], ["promo_offers.id"], name=op.f("fk_offer_posts_offer_id_promo_offers"),
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_offer_posts")),
        sa.UniqueConstraint("offer_id", "bot_id", name="uq_offer_posts_offer_bot"),
    )
    op.create_index("ix_offer_posts_bot_id_status_queued_at", "offer_posts", ["bot_id", "status", "queued_at"])
    op.create_index("ix_offer_posts_bot_id_posted_at", "offer_posts", ["bot_id", "posted_at"])
    op.create_index("ix_offer_posts_bot_id_wa_status_wa_queued_at", "offer_posts",
                    ["bot_id", "wa_status", "wa_queued_at"])
    op.create_index("ix_offer_posts_bot_id_wa_sent_at", "offer_posts", ["bot_id", "wa_sent_at"])

    # ---- migra a configuração antiga para o primeiro bot de ofertas
    conn = op.get_bind()
    old = dict(conn.execute(sa.text("SELECT key, value FROM app_settings WHERE key = ANY(:k)"),
                            {"k": OLD_KEYS}).all())
    has_old = any((old.get(k) or "").strip() for k in ("promo_channel_id", "wa_groups", "promo_enabled", "wa_instance"))
    has_offers = conn.execute(sa.text("SELECT EXISTS (SELECT 1 FROM promo_offers)")).scalar()
    if has_old or has_offers:
        config = {new: old[k] for k, new in CONFIG_KEYS.items() if old.get(k) not in (None, "")}
        state = {}
        if old.get("promo_last_run_at"):
            state["last_run_at"] = old["promo_last_run_at"]
        if old.get("promo_last_result"):
            try:
                state["last_result"] = json.loads(old["promo_last_result"])
            except ValueError:
                pass
        if old.get("wa_started_at"):
            state["wa_started_at"] = old["wa_started_at"]
        bot_id = conn.execute(sa.text(
            "INSERT INTO offer_bots (name, is_active, wa_instance, config, state) "
            "VALUES ('Bot de ofertas 1', :active, :instance, CAST(:config AS jsonb), CAST(:state AS jsonb)) "
            "RETURNING id"),
            {"active": old.get("promo_enabled") == "1" or old.get("wa_enabled") == "1",
             "instance": (old.get("wa_instance") or "").strip() or None,
             "config": json.dumps(config), "state": json.dumps(state)}).scalar()
        if not (old.get("wa_instance") or "").strip():
            conn.execute(sa.text("UPDATE offer_bots SET wa_instance = 'ofertas' || id WHERE id = :id"), {"id": bot_id})
        channel = (old.get("promo_channel_id") or "").strip()
        if channel:
            conn.execute(sa.text("INSERT INTO offer_bot_targets (bot_id, channel, target_id, name) "
                                 "VALUES (:b, 'telegram', :t, :t)"), {"b": bot_id, "t": channel[:64]})
        try:
            groups = json.loads(old.get("wa_groups") or "[]")
        except ValueError:
            groups = []
        for g in groups if isinstance(groups, list) else []:
            if isinstance(g, dict) and str(g.get("id", "")).endswith("@g.us"):
                conn.execute(sa.text("INSERT INTO offer_bot_targets (bot_id, channel, target_id, name) "
                                     "VALUES (:b, 'whatsapp', :t, :n) ON CONFLICT DO NOTHING"),
                             {"b": bot_id, "t": str(g["id"])[:64], "n": str(g.get("name") or g["id"])[:120]})
        conn.execute(sa.text(
            "INSERT INTO offer_posts (offer_id, bot_id, status, queued_at, posted_at, tg_messages, error, "
            "wa_status, wa_queued_at, wa_sent_at, wa_error) "
            "SELECT id, :b, status, queued_at, posted_at, "
            "CASE WHEN telegram_message_id IS NULL THEN NULL ELSE jsonb_build_array(jsonb_build_object("
            "'target', telegram_chat_id, 'message_id', telegram_message_id)) END, "
            "error, wa_status, wa_queued_at, wa_sent_at, wa_error FROM promo_offers"), {"b": bot_id})

    conn.execute(sa.text("DELETE FROM app_settings WHERE key = ANY(:k)"), {"k": OLD_KEYS})

    # ---- promo_offers passa a guardar só o produto
    for ix in ("ix_promo_offers_posted_at", "ix_promo_offers_status_discount_pct", "ix_promo_offers_status_queued_at",
               "ix_promo_offers_wa_sent_at", "ix_promo_offers_wa_status_wa_queued_at"):
        op.drop_index(ix, table_name="promo_offers")
    for col in ("status", "queued_at", "posted_at", "telegram_chat_id", "telegram_message_id", "error",
                "wa_status", "wa_queued_at", "wa_sent_at", "wa_error"):
        op.drop_column("promo_offers", col)


def downgrade() -> None:
    op.add_column("promo_offers", sa.Column("status", sa.Enum(*OFFER_STATUS, name="offer_status", native_enum=False,
                                                              create_constraint=True, length=32),
                                            server_default="new", nullable=False))
    op.add_column("promo_offers", sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("promo_offers", sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("promo_offers", sa.Column("telegram_chat_id", sa.String(length=64), nullable=True))
    op.add_column("promo_offers", sa.Column("telegram_message_id", sa.BigInteger(), nullable=True))
    op.add_column("promo_offers", sa.Column("error", sa.Text(), nullable=True))
    op.add_column("promo_offers", sa.Column("wa_status", sa.Enum(*WA_STATUS, name="wa_status", native_enum=False,
                                                                 create_constraint=True, length=32),
                                            server_default="none", nullable=False))
    op.add_column("promo_offers", sa.Column("wa_queued_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("promo_offers", sa.Column("wa_sent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("promo_offers", sa.Column("wa_error", sa.Text(), nullable=True))
    op.create_index("ix_promo_offers_wa_status_wa_queued_at", "promo_offers", ["wa_status", "wa_queued_at"])
    op.create_index("ix_promo_offers_wa_sent_at", "promo_offers", ["wa_sent_at"])
    op.create_index("ix_promo_offers_status_queued_at", "promo_offers", ["status", "queued_at"])
    op.create_index("ix_promo_offers_status_discount_pct", "promo_offers", ["status", "discount_pct"])
    op.create_index("ix_promo_offers_posted_at", "promo_offers", ["posted_at"])
    op.drop_table("offer_posts")
    op.drop_table("offer_bot_targets")
    op.drop_table("offer_bots")
