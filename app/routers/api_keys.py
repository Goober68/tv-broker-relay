from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from datetime import datetime

from app.models.db import get_db
from app.models.tenant import Tenant
from app.dependencies.auth import get_current_tenant
from app.services.api_keys import create_api_key, list_api_keys, revoke_api_key

router = APIRouter(prefix="/api/api-keys", tags=["api-keys"])

MAX_KEYS_PER_TENANT = 10  # reasonable ceiling; plan enforcement comes in Step 4


# ── Schemas ────────────────────────────────────────────────────────────────────

class CreateKeyRequest(BaseModel):
    name: str

    class Config:
        json_schema_extra = {"example": {"name": "TradingView Production"}}


class ApiKeyOut(BaseModel):
    id: int
    name: str
    key_prefix: str
    is_active: bool
    created_at: datetime
    last_used_at: datetime | None

    class Config:
        from_attributes = True


class ApiKeyCreatedOut(ApiKeyOut):
    """Returned only at creation time — includes the raw key."""
    raw_key: str


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.get("", response_model=list[ApiKeyOut])
async def get_api_keys(
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """List all API keys for the authenticated tenant."""
    return await list_api_keys(db, tenant.id)


@router.post("", response_model=ApiKeyCreatedOut, status_code=status.HTTP_201_CREATED)
async def create_key(
    body: CreateKeyRequest,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    Create a new API key.
    The raw key is returned exactly once — store it immediately.
    """
    existing = await list_api_keys(db, tenant.id)
    active_count = sum(1 for k in existing if k.is_active)
    if active_count >= MAX_KEYS_PER_TENANT:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Maximum of {MAX_KEYS_PER_TENANT} active API keys allowed",
        )

    key, raw = await create_api_key(db, tenant.id, body.name)
    await db.commit()
    await db.refresh(key)

    return ApiKeyCreatedOut(
        id=key.id,
        name=key.name,
        key_prefix=key.key_prefix,
        is_active=key.is_active,
        created_at=key.created_at,
        last_used_at=key.last_used_at,
        raw_key=raw,
    )


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_key(
    key_id: int,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Revoke (deactivate) an API key. The key cannot be re-activated."""
    revoked = await revoke_api_key(db, key_id, tenant.id)
    if not revoked:
        raise HTTPException(status_code=404, detail="API key not found")
    await db.commit()
