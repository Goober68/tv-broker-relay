"""Add live P&L columns to positions

Revision ID: 0003
Revises: 0002
Create Date: 2025-01-01 02:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("positions", sa.Column("last_price",     sa.Float(), nullable=True))
    op.add_column("positions", sa.Column("unrealized_pnl", sa.Float(), nullable=True))
    op.add_column("positions", sa.Column("last_price_at",  sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("positions", "last_price_at")
    op.drop_column("positions", "unrealized_pnl")
    op.drop_column("positions", "last_price")
