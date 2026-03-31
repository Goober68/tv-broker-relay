"""Initial schema — created by init_db create_all

Revision ID: 0001
Revises: None
Create Date: 2026-03-31

All tables created by SQLAlchemy create_all from models.
This migration exists only as a baseline for future migrations.
"""
from typing import Sequence, Union
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass  # tables already created by init_db


def downgrade() -> None:
    pass
