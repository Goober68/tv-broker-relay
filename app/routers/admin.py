"""
Admin endpoints — require is_admin=True on the JWT.

These are operator-facing, not tenant-facing.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
import uuid
from pydantic import BaseModel
from datetime import datetime

from app.models.db import get_db
from app.models.tenant import Tenant
from app.models.plan import Plan, Subscription
from app.dependencies.auth import require_admin
from app.services.plans import assign_plan, seed_plans, get_plan_by_name

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ── Schemas ────────────────────────────────────────────────────────────────────

class PlanOut(BaseModel):
    id: int
    name: str
    display_name: str
    stripe_price_id: str | None
    max_broker_accounts: int
    max_monthly_orders: int
    max_open_orders: int
    requests_per_minute: int
    allowed_order_types: list | None
    max_position_size: float | None
    max_daily_loss: float | None
    is_active: bool

    class Config:
        from_attributes = True


class SubscriptionOut(BaseModel):
    id: int
    tenant_id: uuid.UUID
    plan_name: str
    status: str
    orders_this_period: int
    current_period_start: datetime | None
    current_period_end: datetime | None
    stripe_customer_id: str | None
    stripe_subscription_id: str | None

    class Config:
        from_attributes = True


class TenantAdminOut(BaseModel):
    id: uuid.UUID
    email: str
    is_active: bool
    is_admin: bool
    email_verified: bool
    created_at: datetime
    plan_name: str | None
    subscription_status: str | None
    orders_this_period: int

    class Config:
        from_attributes = True


class AssignPlanRequest(BaseModel):
    plan_name: str
    stripe_customer_id: str | None = None
    stripe_subscription_id: str | None = None


# ── Plans ──────────────────────────────────────────────────────────────────────

@router.get("/plans", response_model=list[PlanOut])
async def list_plans(
    _admin: Tenant = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Plan).order_by(Plan.id))
    return result.scalars().all()


@router.post("/plans/seed", status_code=204)
async def reseed_plans(
    _admin: Tenant = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Re-upsert canonical plan definitions. Safe to call multiple times."""
    await seed_plans(db)


@router.patch("/plans/{plan_id}/stripe-price")
async def set_stripe_price(
    plan_id: int,
    stripe_price_id: str,
    _admin: Tenant = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Plan).where(Plan.id == plan_id))
    plan = result.scalar_one_or_none()
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    plan.stripe_price_id = stripe_price_id
    await db.commit()
    return {"plan_id": plan_id, "stripe_price_id": stripe_price_id}


# ── Tenants ────────────────────────────────────────────────────────────────────

@router.get("/tenants", response_model=list[TenantAdminOut])
async def list_tenants(
    limit: int = Query(50, le=500),
    offset: int = Query(0),
    _admin: Tenant = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Tenant)
        .options(selectinload(Tenant.subscription).selectinload(Subscription.plan))
        .order_by(Tenant.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    tenants = result.scalars().all()

    out = []
    for t in tenants:
        sub = t.subscription
        out.append(TenantAdminOut(
            id=t.id,
            email=t.email,
            is_active=t.is_active,
            is_admin=t.is_admin,
            email_verified=t.email_verified,
            created_at=t.created_at,
            plan_name=sub.plan.name if sub else None,
            subscription_status=sub.status if sub else None,
            orders_this_period=sub.orders_this_period if sub else 0,
        ))
    return out


@router.get("/tenants/{tenant_id}", response_model=TenantAdminOut)
async def get_tenant(
    tenant_id: uuid.UUID,
    _admin: Tenant = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Tenant)
        .where(Tenant.id == tenant_id)
        .options(selectinload(Tenant.subscription).selectinload(Subscription.plan))
    )
    tenant = result.scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")

    sub = tenant.subscription
    return TenantAdminOut(
        id=tenant.id,
        email=tenant.email,
        is_active=tenant.is_active,
        is_admin=tenant.is_admin,
        email_verified=tenant.email_verified,
        created_at=tenant.created_at,
        plan_name=sub.plan.name if sub else None,
        subscription_status=sub.status if sub else None,
        orders_this_period=sub.orders_this_period if sub else 0,
    )


@router.post("/tenants/{tenant_id}/plan", response_model=SubscriptionOut)
async def assign_tenant_plan(
    tenant_id: uuid.UUID,
    body: AssignPlanRequest,
    _admin: Tenant = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Manually assign a plan to a tenant (e.g. for enterprise deals or support overrides)."""
    # Check tenant exists
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Tenant not found")

    try:
        sub = await assign_plan(
            db, tenant_id, body.plan_name,
            body.stripe_customer_id, body.stripe_subscription_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    await db.commit()
    await db.refresh(sub)

    # Reload with plan
    result = await db.execute(
        select(Subscription)
        .where(Subscription.id == sub.id)
        .options(selectinload(Subscription.plan))
    )
    sub = result.scalar_one()

    return SubscriptionOut(
        id=sub.id,
        tenant_id=sub.tenant_id,
        plan_name=sub.plan.name,
        status=sub.status,
        orders_this_period=sub.orders_this_period,
        current_period_start=sub.current_period_start,
        current_period_end=sub.current_period_end,
        stripe_customer_id=sub.stripe_customer_id,
        stripe_subscription_id=sub.stripe_subscription_id,
    )


@router.patch("/tenants/{tenant_id}/active")
async def set_tenant_active(
    tenant_id: uuid.UUID,
    is_active: bool,
    _admin: Tenant = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Enable or disable a tenant account."""
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    tenant.is_active = is_active
    await db.commit()
    return {"tenant_id": tenant_id, "is_active": is_active}


# ── Stats ──────────────────────────────────────────────────────────────────────

@router.get("/stats")
async def get_stats(
    _admin: Tenant = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """High-level platform stats."""
    total_tenants = (await db.execute(select(func.count(Tenant.id)))).scalar_one()
    active_tenants = (await db.execute(
        select(func.count(Tenant.id)).where(Tenant.is_active == True)  # noqa: E712
    )).scalar_one()

    plan_counts_result = await db.execute(
        select(Plan.name, func.count(Subscription.id))
        .join(Subscription, Subscription.plan_id == Plan.id)
        .group_by(Plan.name)
    )
    plan_counts = {name: count for name, count in plan_counts_result.all()}

    from app.models.order import Order
    total_orders = (await db.execute(select(func.count(Order.id)))).scalar_one()

    return {
        "total_tenants": total_tenants,
        "active_tenants": active_tenants,
        "tenants_by_plan": plan_counts,
        "total_orders": total_orders,
    }


@router.post("/trigger-reconcile")
async def trigger_reconcile(
    _admin: Tenant = Depends(require_admin),
):
    """Trigger an immediate reconciliation cycle (includes Tradovate fill sync)."""
    import asyncio
    from app.services.background_tasks import _reconcile_once
    asyncio.create_task(_reconcile_once())
    return {"status": "triggered"}
