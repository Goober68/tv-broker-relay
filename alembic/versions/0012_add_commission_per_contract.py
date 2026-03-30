"""Add commission_per_contract to broker_accounts

Revision ID: 0012
Revises: 0011
Create Date: 2026-03-30 14:00:00.000000

Per-contract per-side commission rate for net P&L calculation.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("broker_accounts", sa.Column("commission_per_contract", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("broker_accounts", "commission_per_contract")
