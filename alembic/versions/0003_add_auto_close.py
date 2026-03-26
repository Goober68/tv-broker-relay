"""Add auto_close columns to broker_accounts

Revision ID: 0003
Revises: 0002
Create Date: 2025-01-01 02:00:00.000000

Adds auto_close_enabled and auto_close_time to broker_accounts
for prop firm session-end compliance (automatic position close).
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("broker_accounts",
        sa.Column("auto_close_enabled", sa.Boolean(),
                  server_default="false", nullable=False)
    )
    op.add_column("broker_accounts",
        sa.Column("auto_close_time", sa.String(5), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("broker_accounts", "auto_close_time")
    op.drop_column("broker_accounts", "auto_close_enabled")
