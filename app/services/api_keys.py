"""
API key service.

Key format:  tvr_{tenant_id}_{48 url-safe random bytes}
  - "tvr_" prefix — identifiable in configs/logs
  - tenant_id embedded — lets us route to the right tenant without a DB lookup
    (we still verify against the DB; this is just for routing convenience)
  - 48 random bytes — 384 bits of entropy, unguessable

Storage: SHA-256 hash stored in DB. Raw key returned once at creation only.
Lookup: hash the presented key, query by hash. O(1), constant-time compare.
"""
import hashlib
import secrets
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from app.models.api_key import ApiKey


def _generate_raw_key(tenant_id: int) -> str:
    random_part = secrets.token_urlsafe(48)
    return f"tvr_{tenant_id}_{random_part}"


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _key_prefix(raw: str) -> str:
    """First 16 chars — enough to identify the key in a list, not enough to brute-force."""
    return raw[:16] + "..."


async def create_api_key(
    db: AsyncSession, tenant_id: int, name: str
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
    db: AsyncSession, raw_key: str, tenant_id: int
) -> ApiKey | None:
    """
    Verify a raw key belongs to the given tenant and is active.
    Updates last_used_at on success.
    Returns the ApiKey record or None.
    """
    key_hash = _hash_key(raw_key)
    result = await db.execute(
        select(ApiKey).where(
            ApiKey.key_hash == key_hash,
            ApiKey.tenant_id == tenant_id,
            ApiKey.is_active == True,  # noqa: E712
        )
    )
    key = result.scalar_one_or_none()
    if key is None:
        return None

    # Touch last_used_at without loading the full object
    await db.execute(
        update(ApiKey)
        .where(ApiKey.id == key.id)
        .values(last_used_at=datetime.now(timezone.utc))
    )
    return key


async def list_api_keys(db: AsyncSession, tenant_id: int) -> list[ApiKey]:
    result = await db.execute(
        select(ApiKey)
        .where(ApiKey.tenant_id == tenant_id)
        .order_by(ApiKey.created_at.desc())
    )
    return list(result.scalars().all())


async def revoke_api_key(db: AsyncSession, key_id: int, tenant_id: int) -> bool:
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
