"""Add client_trade_id to orders

Revision ID: 0002
Revises: 0001
Create Date: 2025-01-01 01:00:00.000000

Adds client_trade_id column to orders table for Oanda FIFO avoidance.
The clientTradeID tags each pyramid leg so individual trades can be
identified and closed specifically rather than relying on FIFO ordering.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("orders",
        sa.Column("client_trade_id", sa.String(128), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("orders", "client_trade_id")
