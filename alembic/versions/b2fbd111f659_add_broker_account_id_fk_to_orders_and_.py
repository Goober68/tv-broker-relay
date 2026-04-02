"""Add broker_account_id FK to orders and positions

Revision ID: b2fbd111f659
Revises: 0001
Create Date: 2026-04-02 22:35:04.678259

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b2fbd111f659'
down_revision: Union[str, None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add broker_account_id column to orders
    op.add_column('orders', sa.Column('broker_account_id', sa.Integer(), nullable=True))
    op.create_index('ix_orders_broker_account_id', 'orders', ['broker_account_id'])
    op.create_foreign_key(
        'fk_orders_broker_account_id', 'orders', 'broker_accounts',
        ['broker_account_id'], ['id'],
    )

    # Add broker_account_id column to positions
    op.add_column('positions', sa.Column('broker_account_id', sa.Integer(), nullable=True))
    op.create_index('ix_positions_broker_account_id', 'positions', ['broker_account_id'])
    op.create_foreign_key(
        'fk_positions_broker_account_id', 'positions', 'broker_accounts',
        ['broker_account_id'], ['id'],
    )

    # Backfill from broker_accounts using tenant_id + broker + account_alias match
    op.execute("""
        UPDATE orders o
        SET broker_account_id = ba.id
        FROM broker_accounts ba
        WHERE o.tenant_id = ba.tenant_id
          AND o.broker = ba.broker
          AND o.account = ba.account_alias
          AND o.broker_account_id IS NULL
    """)

    op.execute("""
        UPDATE positions p
        SET broker_account_id = ba.id
        FROM broker_accounts ba
        WHERE p.tenant_id = ba.tenant_id
          AND p.broker = ba.broker
          AND p.account = ba.account_alias
          AND p.broker_account_id IS NULL
    """)


def downgrade() -> None:
    op.drop_constraint('fk_positions_broker_account_id', 'positions', type_='foreignkey')
    op.drop_index('ix_positions_broker_account_id', table_name='positions')
    op.drop_column('positions', 'broker_account_id')

    op.drop_constraint('fk_orders_broker_account_id', 'orders', type_='foreignkey')
    op.drop_index('ix_orders_broker_account_id', table_name='orders')
    op.drop_column('orders', 'broker_account_id')
