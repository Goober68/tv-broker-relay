"""
API key service.

Key format:  32-character hex string (128 bits of entropy)
  - Clean, simple, easy to paste into TradingView webhook payloads
  - No prefix or embedded data — tenant routing via DB lookup

Storage: SHA-256 hash stored in DB. Raw key returned once at creation only.
Lookup: hash the presented key, query by hash. O(1), constant-time compare.
"""
import uuid
import hashlib
import secrets
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from app.models.api_key import ApiKey


def _generate_raw_key(tenant_id: uuid.UUID) -> str:
    return secrets.token_hex(16)  # 32-char hex string, 128 bits


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _key_prefix(raw: str) -> str:
    """First 8 chars — enough to identify the key in a list."""
    return raw[:8] + "..."


async def create_api_key(
    db: AsyncSession, tenant_id: uuid.UUID, name: str
) -> tuple[ApiKey, str]:
    """
    Create and persist a new API key.
    Returns (ApiKey record, raw_key).
    raw_key is shown to the user exactly once — not recoverable after this call.
    """
    raw = _generate_raw_key(tenant_id)
    key = ApiKey(
        tenant_id=tenant_id,
        name=name,
        key_hash=_hash_key(raw),
        key_prefix=_key_prefix(raw),
    )
    db.add(key)
    await db.flush()
    return key, raw


async def verify_api_key(
    db: AsyncSession, raw_key: str, tenant_id: uuid.UUID
) -> ApiKey | None:
    """
    Verify a raw key belongs to the given tenant and is active.
    Returns the ApiKey record or None.
    last_used_at is updated in the background by the caller to avoid
    adding a round-trip to the hot path.
    """
    key_hash = _hash_key(raw_key)
    result = await db.execute(
        select(ApiKey).where(
            ApiKey.key_hash == key_hash,
            ApiKey.tenant_id == tenant_id,
            ApiKey.is_active == True,  # noqa: E712
        )
    )
    return result.scalar_one_or_none()


async def touch_api_key_last_used(db: AsyncSession, key_id: int) -> None:
    """Update last_used_at timestamp. Called in background after response."""
    await db.execute(
        update(ApiKey)
        .where(ApiKey.id == key_id)
        .values(last_used_at=datetime.now(timezone.utc))
    )


async def list_api_keys(db: AsyncSession, tenant_id: uuid.UUID) -> list[ApiKey]:
    result = await db.execute(
        select(ApiKey)
        .where(ApiKey.tenant_id == tenant_id)
        .order_by(ApiKey.created_at.desc())
    )
    return list(result.scalars().all())


async def revoke_api_key(db: AsyncSession, key_id: int, tenant_id: uuid.UUID) -> bool:
    """Revoke a key. Returns False if key not found or doesn't belong to tenant."""
    result = await db.execute(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.tenant_id == tenant_id)
    )
    key = result.scalar_one_or_none()
    if key is None:
        return False
    key.is_active = False
    await db.flush()
    return True
