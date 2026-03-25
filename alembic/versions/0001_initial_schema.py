"""Initial schema

Revision ID: 0001
Revises:
Create Date: 2025-01-01 00:00:00.000000

Single migration reflecting the final schema.
No legacy migrations — clean slate only.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── tenants ───────────────────────────────────────────────────────────────
    op.create_table(
        "tenants",
        sa.Column("id",             UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at",     sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at",     sa.DateTime(timezone=True), nullable=False),
        sa.Column("email",          sa.String(256), nullable=False),
        sa.Column("password_hash",  sa.String(256), nullable=False),
        sa.Column("is_active",      sa.Boolean(), server_default="true",  nullable=False),
        sa.Column("is_admin",       sa.Boolean(), server_default="false", nullable=False),
        sa.Column("email_verified", sa.Boolean(), server_default="false", nullable=False),
    )
    op.create_index("ix_tenants_email", "tenants", ["email"], unique=True)

    # ── refresh_tokens ────────────────────────────────────────────────────────
    op.create_table(
        "refresh_tokens",
        sa.Column("id",          sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("tenant_id",   UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("token_hash",  sa.String(256), nullable=False),
        sa.Column("created_at",  sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at",  sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked",     sa.Boolean(), server_default="false", nullable=False),
        sa.Column("user_agent",  sa.String(512), nullable=True),
        sa.Column("ip_address",  sa.String(64),  nullable=True),
    )
    op.create_index("ix_refresh_tokens_tenant_id", "refresh_tokens", ["tenant_id"])
    op.create_index("ix_refresh_tokens_token_hash", "refresh_tokens", ["token_hash"], unique=True)

    # ── api_keys ──────────────────────────────────────────────────────────────
    op.create_table(
        "api_keys",
        sa.Column("id",           sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("created_at",   sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant_id",    UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("name",         sa.String(128), nullable=False),
        sa.Column("key_hash",     sa.String(64),  nullable=False),
        sa.Column("key_prefix",   sa.String(20),  nullable=False),
        sa.Column("is_active",    sa.Boolean(), server_default="true", nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_api_keys_tenant_active", "api_keys", ["tenant_id", "is_active"])
    op.create_index("ix_api_keys_key_hash", "api_keys", ["key_hash"], unique=True)

    # ── broker_accounts ───────────────────────────────────────────────────────
    op.create_table(
        "broker_accounts",
        sa.Column("id",                      sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("created_at",              sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at",              sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant_id",               UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("broker",                  sa.String(32), nullable=False),
        sa.Column("account_alias",           sa.String(64), nullable=False),
        sa.Column("display_name",            sa.String(128), nullable=True),
        sa.Column("credentials_encrypted",   sa.Text(), nullable=False),
        sa.Column("instrument_map",          sa.JSON(), nullable=True),
        sa.Column("is_active",               sa.Boolean(), server_default="true", nullable=False),
        sa.UniqueConstraint("tenant_id", "broker", "account_alias", name="uq_broker_account"),
    )
    op.create_index("ix_broker_accounts_tenant", "broker_accounts", ["tenant_id"])

    # ── plans ─────────────────────────────────────────────────────────────────
    op.create_table(
        "plans",
        sa.Column("id",                  sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("name",                sa.String(64), nullable=False),
        sa.Column("display_name",        sa.String(128), nullable=False),
        sa.Column("stripe_price_id",     sa.String(128), nullable=True),
        sa.Column("max_broker_accounts", sa.Integer(), server_default="1",   nullable=False),
        sa.Column("max_monthly_orders",  sa.Integer(), server_default="100", nullable=False),
        sa.Column("max_open_orders",     sa.Integer(), server_default="5",   nullable=False),
        sa.Column("requests_per_minute", sa.Integer(), server_default="10",  nullable=False),
        sa.Column("allowed_order_types", sa.JSON(), nullable=True),
        sa.Column("max_position_size",   sa.Float(), nullable=True),
        sa.Column("max_daily_loss",      sa.Float(), nullable=True),
        sa.Column("is_active",           sa.Boolean(), server_default="true", nullable=False),
        sa.UniqueConstraint("name"),
    )

    # ── subscriptions ─────────────────────────────────────────────────────────
    op.create_table(
        "subscriptions",
        sa.Column("id",                     sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("created_at",             sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at",             sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant_id",              UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("plan_id",                sa.Integer(), sa.ForeignKey("plans.id"), nullable=False),
        sa.Column("stripe_customer_id",     sa.String(128), nullable=True),
        sa.Column("stripe_subscription_id", sa.String(128), nullable=True),
        sa.Column("status",                 sa.String(32), server_default="active", nullable=False),
        sa.Column("current_period_start",   sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period_end",     sa.DateTime(timezone=True), nullable=True),
        sa.Column("orders_this_period",     sa.Integer(), server_default="0", nullable=False),
        sa.UniqueConstraint("tenant_id", name="uq_subscription_tenant"),
        sa.UniqueConstraint("stripe_subscription_id"),
    )
    op.create_index("ix_subscriptions_tenant_id", "subscriptions", ["tenant_id"])

    # ── positions ─────────────────────────────────────────────────────────────
    op.create_table(
        "positions",
        sa.Column("id",                  sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("updated_at",          sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant_id",           UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("broker",              sa.String(32),  nullable=False),
        sa.Column("account",             sa.String(64),  nullable=False),
        sa.Column("symbol",              sa.String(32),  nullable=False),
        sa.Column("instrument_type",     sa.String(16),  server_default="forex", nullable=False),
        sa.Column("quantity",            sa.Float(), server_default="0",   nullable=False),
        sa.Column("avg_price",           sa.Float(), server_default="0",   nullable=False),
        sa.Column("multiplier",          sa.Float(), server_default="1.0", nullable=False),
        sa.Column("realized_pnl",        sa.Float(), server_default="0",   nullable=False),
        sa.Column("daily_realized_pnl",  sa.Float(), server_default="0",   nullable=False),
        sa.Column("daily_pnl_date",      sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_price",          sa.Float(), nullable=True),
        sa.Column("unrealized_pnl",      sa.Float(), nullable=True),
        sa.Column("last_price_at",       sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "broker", "account", "symbol", name="uq_position_tenant"),
    )
    op.create_index("ix_positions_tenant", "positions", ["tenant_id"])

    # ── orders ────────────────────────────────────────────────────────────────
    # Uses VARCHAR for enum columns (native_enum=False in SQLAlchemy models)
    # to avoid PostgreSQL enum type case sensitivity issues.
    op.create_table(
        "orders",
        sa.Column("id",               sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("created_at",       sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at",       sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant_id",        UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("broker",           sa.String(32), nullable=False),
        sa.Column("account",          sa.String(64), nullable=False),
        sa.Column("symbol",           sa.String(32), nullable=False),
        sa.Column("instrument_type",  sa.String(16), server_default="forex",   nullable=False),
        sa.Column("exchange",         sa.String(32), nullable=True),
        sa.Column("currency",         sa.String(8),  nullable=True),
        sa.Column("action",           sa.String(8),  nullable=False),
        sa.Column("order_type",       sa.String(8),  server_default="market",  nullable=False),
        sa.Column("quantity",         sa.Float(), nullable=False),
        sa.Column("price",            sa.Float(), nullable=True),
        sa.Column("time_in_force",    sa.String(4),  server_default="GTC",     nullable=False),
        sa.Column("expire_at",        sa.DateTime(timezone=True), nullable=True),
        sa.Column("multiplier",       sa.Float(), server_default="1.0",  nullable=False),
        sa.Column("extended_hours",   sa.Boolean(), server_default="false", nullable=False),
        sa.Column("option_expiry",    sa.String(16), nullable=True),
        sa.Column("option_strike",    sa.Float(), nullable=True),
        sa.Column("option_right",     sa.String(4),  nullable=True),
        sa.Column("option_multiplier",sa.Float(), server_default="100.0", nullable=False),
        sa.Column("stop_loss",        sa.Float(), nullable=True),
        sa.Column("take_profit",      sa.Float(), nullable=True),
        sa.Column("trailing_distance",sa.Float(), nullable=True),
        sa.Column("status",           sa.String(12), server_default="pending",  nullable=False),
        sa.Column("broker_order_id",  sa.String(128), nullable=True),
        sa.Column("filled_quantity",  sa.Float(), server_default="0", nullable=False),
        sa.Column("avg_fill_price",   sa.Float(), nullable=True),
        sa.Column("raw_payload",      sa.Text(), nullable=True),
        sa.Column("comment",          sa.String(256), nullable=True),
        sa.Column("error_message",    sa.Text(), nullable=True),
    )
    op.create_index("ix_orders_tenant_id",     "orders", ["tenant_id"])
    op.create_index("ix_orders_tenant_symbol", "orders", ["tenant_id", "symbol"])
    op.create_index("ix_orders_tenant_status", "orders", ["tenant_id", "status"])

    # ── webhook_deliveries ────────────────────────────────────────────────────
    op.create_table(
        "webhook_deliveries",
        sa.Column("id",           sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("created_at",   sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant_id",    UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("source_ip",    sa.String(64),  nullable=True),
        sa.Column("user_agent",   sa.String(256), nullable=True),
        sa.Column("raw_payload",  sa.Text(), nullable=True),
        sa.Column("http_status",  sa.Integer(), nullable=False),
        sa.Column("auth_passed",  sa.Boolean(), server_default="false", nullable=False),
        sa.Column("order_id",     sa.Integer(), sa.ForeignKey("orders.id"), nullable=True),
        sa.Column("outcome",      sa.String(32), nullable=False),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("duration_ms",  sa.Float(), nullable=True),
    )
    op.create_index("ix_deliveries_tenant_created",    "webhook_deliveries", ["tenant_id", "created_at"])
    op.create_index("ix_webhook_deliveries_created_at","webhook_deliveries", ["created_at"])


def downgrade() -> None:
    op.drop_table("webhook_deliveries")
    op.drop_table("orders")
    op.drop_table("positions")
    op.drop_table("subscriptions")
    op.drop_table("plans")
    op.drop_table("broker_accounts")
    op.drop_table("api_keys")
    op.drop_table("refresh_tokens")
    op.drop_table("tenants")
