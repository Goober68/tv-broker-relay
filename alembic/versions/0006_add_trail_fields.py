"""Add trail_trigger, trail_dist, trail_update to orders

Revision ID: 0006
Revises: 0005
Create Date: 2025-01-01 05:00:00.000000

Adds Tradovate native trailing stop fields to the orders table.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("trail_trigger", sa.Float(), nullable=True))
    op.add_column("orders", sa.Column("trail_dist",    sa.Float(), nullable=True))
    op.add_column("orders", sa.Column("trail_update",  sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "trail_update")
    op.drop_column("orders", "trail_dist")
    op.drop_column("orders", "trail_trigger")
