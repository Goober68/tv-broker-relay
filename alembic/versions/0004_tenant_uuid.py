"""Convert tenant ID from integer to UUID

Revision ID: 0004
Revises: 0003
Create Date: 2025-01-01 03:00:00.000000

This migration:
1. Adds a uuid column to tenants
2. Populates it with gen_random_uuid() for existing rows
3. Updates all child table FK columns to uuid
4. Drops the old integer primary key and promotes uuid to PK

All existing webhook URLs (/webhook/{int}) will become invalid.
Tenants must update their TradingView alerts to use the new UUID.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Child tables with tenant_id FK referencing tenants.id
CHILD_TABLES = [
    "refresh_tokens",
    "api_keys",
    "broker_accounts",
    "subscriptions",
    "positions",
    "orders",
    "webhook_deliveries",
]


def upgrade() -> None:
    # Enable pgcrypto for gen_random_uuid()
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # ── Step 1: Add uuid column to tenants ────────────────────────────────────
    op.add_column("tenants",
        sa.Column("uuid", UUID(as_uuid=True), nullable=True)
    )
    # Populate uuid for all existing tenants
    op.execute("UPDATE tenants SET uuid = gen_random_uuid()")
    # Make it non-nullable
    op.alter_column("tenants", "uuid", nullable=False)
    # Add unique constraint
    op.create_unique_constraint("uq_tenants_uuid", "tenants", ["uuid"])

    # ── Step 2: Add uuid FK columns to all child tables ───────────────────────
    for table in CHILD_TABLES:
        op.add_column(table,
            sa.Column("tenant_uuid", UUID(as_uuid=True), nullable=True)
        )
        # Populate by joining to tenants
        op.execute(f"""
            UPDATE {table} t
            SET tenant_uuid = ten.uuid
            FROM tenants ten
            WHERE t.tenant_id = ten.id
        """)
        op.alter_column(table, "tenant_uuid", nullable=False)

    # ── Step 3: Drop old FK constraints ───────────────────────────────────────
    # Drop existing FK constraints on tenant_id columns
    # Names may vary — use IF EXISTS via raw SQL
    for table in CHILD_TABLES:
        op.execute(f"""
            DO $$
            DECLARE r RECORD;
            BEGIN
                FOR r IN (
                    SELECT constraint_name
                    FROM information_schema.table_constraints
                    WHERE table_name = '{table}'
                    AND constraint_type = 'FOREIGN KEY'
                    AND constraint_name LIKE '%tenant%'
                ) LOOP
                    EXECUTE 'ALTER TABLE {table} DROP CONSTRAINT ' || r.constraint_name;
                END LOOP;
            END $$;
        """)
        # Drop old tenant_id column
        op.drop_column(table, "tenant_id")
        # Rename tenant_uuid to tenant_id
        op.alter_column(table, "tenant_uuid", new_column_name="tenant_id")

    # ── Step 4: Drop old integer PK on tenants, promote uuid to PK ───────────
    op.drop_constraint("uq_tenants_uuid", "tenants")
    op.drop_column("tenants", "id")
    op.alter_column("tenants", "uuid", new_column_name="id")
    op.create_primary_key("pk_tenants", "tenants", ["id"])

    # ── Step 5: Re-add FK constraints on child tables ─────────────────────────
    for table in CHILD_TABLES:
        op.create_foreign_key(
            f"fk_{table}_tenant_id",
            table, "tenants",
            ["tenant_id"], ["id"],
        )
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])

    # Re-add orders composite indexes
    op.create_index("ix_orders_tenant_symbol", "orders", ["tenant_id", "symbol"])
    op.create_index("ix_orders_tenant_status", "orders", ["tenant_id", "status"])


def downgrade() -> None:
    # Downgrade is complex and destructive — not implemented.
    # To downgrade: restore from a backup taken before running this migration.
    raise NotImplementedError(
        "Downgrade from UUID tenant IDs is not supported. "
        "Restore from a pre-migration backup."
    )
