import uuid
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.broker_account import BrokerAccount, BROKER_CREDENTIAL_FIELDS
from app.services.credentials import encrypt_credentials, decrypt_credentials

import logging
logger = logging.getLogger(__name__)


def _validate_credentials(broker: str, creds: dict) -> None:
    """Raise ValueError if required fields are missing for the given broker."""
    required = BROKER_CREDENTIAL_FIELDS.get(broker)
    if required is None:
        raise ValueError(f"Unknown broker: {broker!r}")
    missing = [f for f in required if not creds.get(f)]
    if missing:
        raise ValueError(f"Missing required credential fields for {broker}: {missing}")


async def create_broker_account(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    broker: str,
    account_alias: str,
    credentials: dict,
    display_name: str | None = None,
) -> BrokerAccount:
    _validate_credentials(broker, credentials)

    # Check for duplicate alias
    existing = await db.execute(
        select(BrokerAccount).where(
            BrokerAccount.tenant_id == tenant_id,
            BrokerAccount.broker == broker,
            BrokerAccount.account_alias == account_alias,
        )
    )
    if existing.scalar_one_or_none():
        raise ValueError(
            f"A broker account already exists for broker={broker!r}, alias={account_alias!r}. "
            "Use PATCH to update credentials or DELETE and re-create."
        )

    account = BrokerAccount(
        tenant_id=tenant_id,
        broker=broker,
        account_alias=account_alias,
        display_name=display_name,
        credentials_encrypted=encrypt_credentials(credentials),
    )
    db.add(account)
    await db.flush()
    return account


async def list_broker_accounts(db: AsyncSession, tenant_id: uuid.UUID) -> list[BrokerAccount]:
    result = await db.execute(
        select(BrokerAccount)
        .where(BrokerAccount.tenant_id == tenant_id)
        .order_by(BrokerAccount.broker, BrokerAccount.account_alias)
    )
    return list(result.scalars().all())


async def get_broker_account(
    db: AsyncSession, account_id: int, tenant_id: uuid.UUID
) -> BrokerAccount | None:
    result = await db.execute(
        select(BrokerAccount).where(
            BrokerAccount.id == account_id,
            BrokerAccount.tenant_id == tenant_id,
        )
    )
    return result.scalar_one_or_none()


async def update_broker_account_credentials(
    db: AsyncSession,
    account_id: int,
    tenant_id: uuid.UUID,
    credentials: dict,
    display_name: str | None = None,
) -> BrokerAccount | None:
    account = await get_broker_account(db, account_id, tenant_id)
    if account is None:
        return None
    _validate_credentials(account.broker, credentials)
    account.credentials_encrypted = encrypt_credentials(credentials)
    if display_name is not None:
        account.display_name = display_name
    await db.flush()
    return account


async def delete_broker_account(
    db: AsyncSession, account_id: int, tenant_id: uuid.UUID
) -> bool:
    account = await get_broker_account(db, account_id, tenant_id)
    if account is None:
        return False
    await db.delete(account)
    await db.flush()
    return True


def safe_credential_summary(broker: str, credentials: dict) -> dict:
    """
    Return a redacted view of credentials safe to show in API responses.
    Masks all values except the last 4 chars and the base_url / account_id.
    """
    visible_fields = {"base_url", "account_id", "app_version", "app_id"}
    summary = {}
    for k, v in credentials.items():
        if k in visible_fields:
            summary[k] = v
        elif isinstance(v, str) and len(v) > 4:
            summary[k] = "****" + v[-4:]
        else:
            summary[k] = "****"
    return summary
