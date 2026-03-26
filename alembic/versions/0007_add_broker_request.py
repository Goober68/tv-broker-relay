"""Add broker_request to orders

Revision ID: 0007
Revises: 0006
Create Date: 2025-01-01 06:00:00.000000

Stores the outbound JSON body sent to the broker on failed orders,
visible in the dashboard delivery log for debugging.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("orders",
        sa.Column("broker_request", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("orders", "broker_request")
