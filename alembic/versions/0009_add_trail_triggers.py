"""Add trail_triggers table

Revision ID: 0009
Revises: 0008
Create Date: 2025-01-01 08:00:00.000000

Persists pending Oanda trailing stop triggers for crash recovery.
The price stream monitors these and fires native trailing stop orders
when the trigger price is hit.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "trail_triggers",
        sa.Column("id",                sa.Integer(),                  primary_key=True, autoincrement=True),
        sa.Column("created_at",        sa.DateTime(timezone=True),    nullable=False),
        sa.Column("updated_at",        sa.DateTime(timezone=True),    nullable=False),
        sa.Column("tenant_id",         UUID(as_uuid=True),            nullable=False),
        sa.Column("broker_account_id", sa.Integer(),                  sa.ForeignKey("broker_accounts.id"), nullable=False),
        sa.Column("order_id",          sa.Integer(),                  sa.ForeignKey("orders.id"), nullable=True),
        sa.Column("broker",            sa.String(32),                 nullable=False),
        sa.Column("account",           sa.String(128),                nullable=False),
        sa.Column("symbol",            sa.String(32),                 nullable=False),
        sa.Column("direction",         sa.String(8),                  nullable=False),
        sa.Column("trigger_price",     sa.Float(),                    nullable=False),
        sa.Column("trail_distance",    sa.Float(),                    nullable=False),
        sa.Column("trade_id",          sa.String(128),                nullable=True),
        sa.Column("status",            sa.String(16),                 nullable=False, server_default="pending"),
        sa.Column("fired_at",          sa.DateTime(timezone=True),    nullable=True),
        sa.Column("error_detail",      sa.Text(),                     nullable=True),
    )
    op.create_index("ix_trail_triggers_status_broker", "trail_triggers",
                    ["status", "broker", "account"])


def downgrade() -> None:
    op.drop_index("ix_trail_triggers_status_broker", "trail_triggers")
    op.drop_table("trail_triggers")
