"""Add algo_id and algo_version to orders

Revision ID: 0010
Revises: 0009
Create Date: 2026-03-28 08:00:00.000000

Optional fields for tracking which algorithm/strategy generated each order.
Enables slicing P&L by algo_id in future reporting.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("algo_id", sa.String(64), nullable=True))
    op.add_column("orders", sa.Column("algo_version", sa.String(32), nullable=True))
    op.create_index("ix_orders_algo_id", "orders", ["algo_id"])


def downgrade() -> None:
    op.drop_index("ix_orders_algo_id", table_name="orders")
    op.drop_column("orders", "algo_version")
    op.drop_column("orders", "algo_id")
