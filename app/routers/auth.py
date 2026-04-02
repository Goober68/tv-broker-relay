from fastapi import APIRouter, Depends, HTTPException, Request, status, Cookie
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import uuid
from pydantic import BaseModel, EmailStr, field_validator
from typing import Annotated

from app.models.db import get_db
from app.models.tenant import Tenant
from app.services.auth import (
    hash_password,
    create_access_token,
    create_refresh_token,
    rotate_refresh_token,
    revoke_refresh_token,
    revoke_all_refresh_tokens,
    authenticate_tenant,
    get_tenant_by_email,
)
from app.dependencies.auth import get_current_tenant

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Refresh token is sent as an HttpOnly cookie — never in the response body.
REFRESH_COOKIE = "refresh_token"
COOKIE_OPTS = dict(httponly=True, secure=True, samesite="lax", path="/api/auth")


# ── Schemas ────────────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one digit")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds


class TenantOut(BaseModel):
    id: uuid.UUID
    email: str
    is_admin: bool
    email_verified: bool
    # Eagerly included so callers don't need a second request
    plan_name: str | None = None

    class Config:
        from_attributes = True


# ── Helpers ────────────────────────────────────────────────────────────────────

def _token_response(access_token: str, expires_in_minutes: int) -> TokenResponse:
    return TokenResponse(
        access_token=access_token,
        expires_in=expires_in_minutes * 60,
    )


def _set_refresh_cookie(response: JSONResponse, raw_token: str) -> None:
    response.set_cookie(REFRESH_COOKIE, raw_token, **COOKIE_OPTS)


def _clear_refresh_cookie(response: JSONResponse) -> None:
    response.delete_cookie(REFRESH_COOKIE, path="/api/auth")


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.post("/register", response_model=TenantOut, status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest,
    db: AsyncSession = Depends(get_db),
):
    from app.services.plans import get_or_create_subscription

    existing = await get_tenant_by_email(db, body.email)
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")

    tenant = Tenant(
        email=body.email.lower().strip(),
        password_hash=hash_password(body.password),
    )
    db.add(tenant)
    await db.flush()  # get tenant.id before creating subscription

    # Eagerly create Free subscription so /billing/subscription works immediately
    await get_or_create_subscription(db, tenant.id)

    await db.commit()
    await db.refresh(tenant)
    return tenant


@router.post("/login")
async def login(
    body: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    from app.config import get_settings
    settings = get_settings()

    tenant = await authenticate_tenant(db, body.email, body.password)
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    access_token = create_access_token(tenant.id, tenant.is_admin)
    raw_refresh = await create_refresh_token(
        db, tenant.id,
        user_agent=request.headers.get("user-agent"),
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()

    data = _token_response(access_token, settings.jwt_access_token_expire_minutes)
    response = JSONResponse(content=data.model_dump())
    _set_refresh_cookie(response, raw_refresh)
    return response


@router.post("/refresh")
async def refresh(
    request: Request,
    refresh_token: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
    db: AsyncSession = Depends(get_db),
):
    from app.config import get_settings
    settings = get_settings()

    if not refresh_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No refresh token")

    result = await rotate_refresh_token(
        db, refresh_token,
        user_agent=request.headers.get("user-agent"),
        ip_address=request.client.host if request.client else None,
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token invalid or expired",
        )
    await db.commit()

    new_raw, tenant_id = result
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None or not tenant.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account inactive")

    access_token = create_access_token(tenant_id, tenant.is_admin)
    data = _token_response(access_token, settings.jwt_access_token_expire_minutes)
    response = JSONResponse(content=data.model_dump())
    _set_refresh_cookie(response, new_raw)
    return response


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    refresh_token: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
    db: AsyncSession = Depends(get_db),
):
    """Revoke the current session's refresh token."""
    if refresh_token:
        await revoke_refresh_token(db, refresh_token)
    response = Response(status_code=204)
    _clear_refresh_cookie(response)
    return response


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Revoke all refresh tokens for this tenant (logout from all devices)."""
    await revoke_all_refresh_tokens(db, tenant.id)
    response = Response(status_code=204)
    _clear_refresh_cookie(response)
    return response


@router.get("/me", response_model=TenantOut)
async def get_me(
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    Return the current tenant profile.
    Also ensures a Free subscription exists for tenants who registered
    before the auto-create change (idempotent — no-op if already present).
    """
    from app.services.plans import get_or_create_subscription
    from sqlalchemy.orm import selectinload
    from sqlalchemy import select as sa_select
    from app.models.plan import Subscription

    sub = await get_or_create_subscription(db, tenant.id)
    await db.commit()

    # Load plan name without a second round-trip
    result = await db.execute(
        sa_select(Subscription)
        .where(Subscription.id == sub.id)
        .options(selectinload(Subscription.plan))
    )
    sub = result.scalar_one()

    return TenantOut(
        id=tenant.id,
        email=tenant.email,
        is_admin=tenant.is_admin,
        email_verified=tenant.email_verified,
        plan_name=sub.plan.name,
    )


@router.put("/me/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: LoginRequest,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Change password — requires current password for confirmation."""
    from app.services.auth import verify_password
    if not verify_password(body.password, tenant.password_hash):
        raise HTTPException(status_code=400, detail="Current password incorrect")
    tenant.password_hash = hash_password(body.password)
    # Invalidate all sessions after a password change
    await revoke_all_refresh_tokens(db, tenant.id)
    await db.commit()
