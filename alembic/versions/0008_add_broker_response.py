"""Add broker_response to orders

Revision ID: 0008
Revises: 0007
Create Date: 2025-01-01 07:00:00.000000

Stores the broker's response body on failed orders.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("orders",
        sa.Column("broker_response", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("orders", "broker_response")
