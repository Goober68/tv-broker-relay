"""
Plan and subscription service.

Plans are seeded at startup — not configurable by tenants.
Every new tenant is automatically assigned the Free plan.

Plan limits:
  Free:       1 broker, 50 orders/month, 3 open orders, 5 req/min, market only
  Pro:        4 brokers, 2000 orders/month, 50 open orders, 60 req/min, all types
  Enterprise: unlimited brokers, unlimited orders, unlimited open, 300 req/min, all types
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.plan import Plan, Subscription

import logging
logger = logging.getLogger(__name__)

# Canonical plan definitions — source of truth
PLAN_DEFINITIONS = [
    {
        "name": "free",
        "display_name": "Free",
        "stripe_price_id": None,
        "max_broker_accounts": 1,
        "max_monthly_orders": 50,
        "max_open_orders": 3,
        "requests_per_minute": 5,
        "allowed_order_types": ["market"],  # free only gets market orders
        "max_position_size": 10_000.0,
        "max_daily_loss": 1_000.0,
    },
    {
        "name": "pro",
        "display_name": "Pro",
        "stripe_price_id": None,  # set from env / admin UI
        "max_broker_accounts": 4,
        "max_monthly_orders": 2_000,
        "max_open_orders": 50,
        "requests_per_minute": 60,
        "allowed_order_types": None,  # all types
        "max_position_size": 500_000.0,
        "max_daily_loss": 25_000.0,
    },
    {
        "name": "enterprise",
        "display_name": "Enterprise",
        "stripe_price_id": None,
        "max_broker_accounts": -1,   # unlimited
        "max_monthly_orders": -1,    # unlimited
        "max_open_orders": -1,       # unlimited
        "requests_per_minute": 300,
        "allowed_order_types": None, # all types
        "max_position_size": None,   # uses global config default
        "max_daily_loss": None,      # uses global config default
    },
]


async def seed_plans(db: AsyncSession) -> None:
    """
    Upsert canonical plan definitions.
    Called at application startup — safe to run multiple times.
    """
    for defn in PLAN_DEFINITIONS:
        result = await db.execute(select(Plan).where(Plan.name == defn["name"]))
        plan = result.scalar_one_or_none()
        if plan is None:
            plan = Plan(**defn)
            db.add(plan)
            logger.info(f"Seeded plan: {defn['name']}")
        else:
            # Update limits in case they changed in code
            for k, v in defn.items():
                setattr(plan, k, v)
    await db.commit()


async def get_plan_by_name(db: AsyncSession, name: str) -> Plan | None:
    result = await db.execute(select(Plan).where(Plan.name == name, Plan.is_active == True))  # noqa: E712
    return result.scalar_one_or_none()


async def get_subscription(db: AsyncSession, tenant_id: uuid.UUID) -> Subscription | None:
    result = await db.execute(
        select(Subscription).where(Subscription.tenant_id == tenant_id)
    )
    return result.scalar_one_or_none()


async def get_or_create_subscription(db: AsyncSession, tenant_id: uuid.UUID) -> Subscription:
    """
    Return the tenant's subscription, creating a Free plan subscription if none exists.
    Called on every webhook request — must be fast.
    """
    sub = await get_subscription(db, tenant_id)
    if sub is not None:
        return sub

    free_plan = await get_plan_by_name(db, "free")
    if free_plan is None:
        raise RuntimeError("Free plan not found — run seed_plans() at startup")

    sub = Subscription(
        tenant_id=tenant_id,
        plan_id=free_plan.id,
        status="active",
        current_period_start=datetime.now(timezone.utc),
    )
    db.add(sub)
    await db.flush()
    logger.info(f"Created free subscription for tenant {tenant_id}")
    return sub


async def assign_plan(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    plan_name: str,
    stripe_customer_id: str | None = None,
    stripe_subscription_id: str | None = None,
) -> Subscription:
    """Assign or change a tenant's plan. Creates subscription if none exists."""
    plan = await get_plan_by_name(db, plan_name)
    if plan is None:
        raise ValueError(f"Plan {plan_name!r} not found")

    sub = await get_subscription(db, tenant_id)
    if sub is None:
        sub = Subscription(tenant_id=tenant_id)
        db.add(sub)

    sub.plan_id = plan.id
    sub.status = "active"
    if stripe_customer_id:
        sub.stripe_customer_id = stripe_customer_id
    if stripe_subscription_id:
        sub.stripe_subscription_id = stripe_subscription_id
    sub.current_period_start = datetime.now(timezone.utc)

    await db.flush()
    return sub


async def increment_order_count(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Increment the monthly order counter. Called after a successful order submission."""
    sub = await get_subscription(db, tenant_id)
    if sub:
        sub.orders_this_period += 1
        await db.flush()


async def reset_period_counter(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Reset the monthly counter. Called by Stripe billing cycle webhook."""
    sub = await get_subscription(db, tenant_id)
    if sub:
        sub.orders_this_period = 0
        sub.current_period_start = datetime.now(timezone.utc)
        await db.flush()
