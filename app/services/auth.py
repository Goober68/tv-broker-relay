"""
Auth service — password hashing, JWT issuance, refresh token rotation.

Token strategy:
  - Access token: short-lived JWT (15 min), stateless, carries tenant_id + is_admin
  - Refresh token: long-lived opaque token (7 days), stored as bcrypt hash in DB
    Rotation: each use issues a new refresh token and revokes the old one.
    If a revoked token is presented (replay attack), all tokens for that tenant
    are immediately revoked (token family invalidation).
"""
import hashlib
import secrets
from datetime import datetime, timezone, timedelta

from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from app.config import get_settings
from app.models.tenant import Tenant, RefreshToken

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ── Password ───────────────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ── Access Token ───────────────────────────────────────────────────────────────

def create_access_token(tenant_id: int, is_admin: bool = False) -> str:
    settings = get_settings()
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.jwt_access_token_expire_minutes
    )
    payload = {
        "sub": str(tenant_id),
        "is_admin": is_admin,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "type": "access",
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    """
    Decode and validate an access token.
    Raises JWTError on invalid/expired tokens.
    """
    settings = get_settings()
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    if payload.get("type") != "access":
        raise JWTError("Not an access token")
    return payload


# ── Refresh Token ──────────────────────────────────────────────────────────────

def _hash_token(raw: str) -> str:
    """SHA-256 hash of the raw token for DB storage (fast lookup, not bcrypt)."""
    return hashlib.sha256(raw.encode()).hexdigest()


async def create_refresh_token(
    db: AsyncSession,
    tenant_id: int,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> str:
    """Generate, store, and return a raw refresh token."""
    settings = get_settings()
    raw = secrets.token_urlsafe(48)
    expire = datetime.now(timezone.utc) + timedelta(days=settings.jwt_refresh_token_expire_days)

    rt = RefreshToken(
        tenant_id=tenant_id,
        token_hash=_hash_token(raw),
        expires_at=expire,
        user_agent=user_agent,
        ip_address=ip_address,
    )
    db.add(rt)
    await db.flush()
    return raw


async def rotate_refresh_token(
    db: AsyncSession,
    raw_token: str,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> tuple[str, int] | None:
    """
    Validate a refresh token, revoke it, and issue a new one.

    Returns (new_raw_token, tenant_id) on success, None if token is invalid.

    Token family invalidation: if a revoked token is presented, all tokens
    for that tenant are revoked immediately (detect replay/theft).
    """
    token_hash = _hash_token(raw_token)
    result = await db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    )
    rt = result.scalar_one_or_none()

    if rt is None:
        return None

    if rt.revoked:
        # Possible token theft — revoke the entire family for this tenant
        await db.execute(
            update(RefreshToken)
            .where(RefreshToken.tenant_id == rt.tenant_id, RefreshToken.revoked == False)  # noqa: E712
            .values(revoked=True)
        )
        await db.commit()
        return None

    if rt.is_expired:
        rt.revoked = True
        await db.commit()
        return None

    # Rotate: revoke old, issue new
    rt.revoked = True
    new_raw = await create_refresh_token(db, rt.tenant_id, user_agent, ip_address)
    await db.flush()
    return new_raw, rt.tenant_id


async def revoke_refresh_token(db: AsyncSession, raw_token: str) -> bool:
    """Revoke a specific refresh token (logout)."""
    token_hash = _hash_token(raw_token)
    result = await db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    )
    rt = result.scalar_one_or_none()
    if rt is None or rt.revoked:
        return False
    rt.revoked = True
    await db.commit()
    return True


async def revoke_all_refresh_tokens(db: AsyncSession, tenant_id: int) -> int:
    """Revoke all active refresh tokens for a tenant (logout everywhere)."""
    result = await db.execute(
        update(RefreshToken)
        .where(RefreshToken.tenant_id == tenant_id, RefreshToken.revoked == False)  # noqa: E712
        .values(revoked=True)
    )
    await db.commit()
    return result.rowcount


# ── Tenant Lookup ──────────────────────────────────────────────────────────────

async def get_tenant_by_email(db: AsyncSession, email: str) -> Tenant | None:
    result = await db.execute(
        select(Tenant).where(Tenant.email == email.lower().strip())
    )
    return result.scalar_one_or_none()


async def get_tenant_by_id(db: AsyncSession, tenant_id: int) -> Tenant | None:
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    return result.scalar_one_or_none()


async def authenticate_tenant(
    db: AsyncSession, email: str, password: str
) -> Tenant | None:
    """Return the tenant if credentials are valid, None otherwise."""
    tenant = await get_tenant_by_email(db, email)
    if tenant is None:
        # Run verify anyway to prevent timing attacks leaking valid emails
        pwd_context.dummy_verify()
        return None
    if not verify_password(password, tenant.password_hash):
        return None
    if not tenant.is_active:
        return None
    return tenant
