"""Add broker_quantity to orders

Revision ID: 0005
Revises: 0004
Create Date: 2025-01-01 04:00:00.000000

Stores the actual quantity sent to the broker, which may differ
from the requested quantity due to FIFO randomization.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("orders",
        sa.Column("broker_quantity", sa.Float(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("orders", "broker_quantity")
