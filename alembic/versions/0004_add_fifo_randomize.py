"""Add FIFO randomization columns to broker_accounts

Revision ID: 0004
Revises: 0003
Create Date: 2025-01-01 03:00:00.000000

Adds fifo_randomize and fifo_max_offset to broker_accounts for
US Oanda accounts subject to NFA FIFO rules.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("broker_accounts",
        sa.Column("fifo_randomize", sa.Boolean(),
                  server_default="false", nullable=False)
    )
    op.add_column("broker_accounts",
        sa.Column("fifo_max_offset", sa.Integer(),
                  server_default="3", nullable=False)
    )


def downgrade() -> None:
    op.drop_column("broker_accounts", "fifo_max_offset")
    op.drop_column("broker_accounts", "fifo_randomize")
