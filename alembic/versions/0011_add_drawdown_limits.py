"""Add drawdown limit fields to broker_accounts

Revision ID: 0011
Revises: 0010
Create Date: 2026-03-29 04:00:00.000000

Per-account prop firm drawdown limits for tracking max total
and daily trailing drawdown thresholds.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("broker_accounts", sa.Column("max_total_drawdown", sa.Float(), nullable=True))
    op.add_column("broker_accounts", sa.Column("max_daily_drawdown", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("broker_accounts", "max_daily_drawdown")
    op.drop_column("broker_accounts", "max_total_drawdown")
