"""
FastAPI dependencies for authentication.

Usage:
    @router.get("/me")
    async def get_me(tenant: Tenant = Depends(get_current_tenant)):
        ...

    @router.delete("/admin/tenant/{id}")
    async def delete_tenant(tenant: Tenant = Depends(require_admin)):
        ...
"""
import uuid
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import get_db
from app.models.tenant import Tenant
from app.services.auth import decode_access_token, get_tenant_by_id

bearer_scheme = HTTPBearer(auto_error=False)

_401 = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid or expired token",
    headers={"WWW-Authenticate": "Bearer"},
)
_403 = HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")


async def get_current_tenant(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> Tenant:
    if credentials is None:
        raise _401
    try:
        payload = decode_access_token(credentials.credentials)
    except JWTError:
        raise _401

    tenant_id = uuid.UUID(payload["sub"])
    tenant = await get_tenant_by_id(db, tenant_id)
    if tenant is None or not tenant.is_active:
        raise _401
    return tenant


async def require_admin(tenant: Tenant = Depends(get_current_tenant)) -> Tenant:
    if not tenant.is_admin:
        raise _403
    return tenant
