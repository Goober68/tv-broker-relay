"""
Plan enforcement.

All limit checks funnel through PlanEnforcer so enforcement logic
is in one place and easy to audit.

Usage in webhook handler:
    enforcer = await PlanEnforcer.load(tenant_id, db)
    enforcer.check_order_type(payload.order_type)  # raises PlanLimitExceeded
    enforcer.check_rate_limit(tenant_id)           # raises PlanLimitExceeded

Usage in order processor:
    await enforcer.check_monthly_volume()          # raises PlanLimitExceeded
    await enforcer.check_open_orders(db)           # raises PlanLimitExceeded

Usage in broker accounts router:
    enforcer.check_broker_account_limit(current_count)
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.models.plan import Plan, Subscription
from app.models.order import Order, OrderStatus
from app.models.broker_account import BrokerAccount
from app.services.plans import get_or_create_subscription
from app.config import get_settings

import logging
logger = logging.getLogger(__name__)

# Simple in-memory rate limiter per tenant
# For production, replace with Redis INCR + EXPIRE
_rate_counters: dict[int, list[float]] = {}


class PlanLimitExceeded(Exception):
    """Raised when a plan limit is hit. Message is safe to return to the client."""
    pass


class PlanEnforcer:
    """
    Loaded once per request with the tenant's current plan limits.
    All check_* methods raise PlanLimitExceeded with a user-facing message.
    """

    def __init__(self, plan: Plan, subscription: Subscription, tenant_id: uuid.UUID):
        self.plan = plan
        self.subscription = subscription
        self.tenant_id = tenant_id
        settings = get_settings()
        # Resolve effective limits — plan overrides global config where set
        self.max_position_size = plan.max_position_size or settings.max_position_size
        self.max_daily_loss = plan.max_daily_loss or settings.max_daily_loss

    @classmethod
    async def load(cls, tenant_id: uuid.UUID, db: AsyncSession) -> "PlanEnforcer":
        """Load the tenant's current subscription and plan. Creates Free sub if none exists."""
        sub = await get_or_create_subscription(db, tenant_id)
        # Eagerly load the plan
        from sqlalchemy.orm import selectinload
        result = await db.execute(
            select(Subscription)
            .where(Subscription.id == sub.id)
            .options(selectinload(Subscription.plan))
        )
        sub = result.scalar_one()
        return cls(sub.plan, sub, tenant_id)

    # ── Order type ─────────────────────────────────────────────────────────────

    def check_order_type(self, order_type: str) -> None:
        allowed = self.plan.allowed_order_types
        if allowed is None:
            return  # all types allowed
        if order_type not in allowed:
            raise PlanLimitExceeded(
                f"Order type {order_type!r} is not available on the {self.plan.display_name} plan. "
                f"Allowed types: {allowed}. Upgrade to access limit and stop orders."
            )

    # ── Rate limit ────────────────────────────────────────────────────────────

    def check_rate_limit(self) -> None:
        """
        Sliding window rate limiter — requests_per_minute limit.
        Uses in-memory counters; swap for Redis in production.
        """
        import time
        limit = self.plan.requests_per_minute
        if limit <= 0:
            return  # unlimited

        now = time.monotonic()
        window_start = now - 60.0

        bucket = _rate_counters.setdefault(self.tenant_id, [])
        # Purge old entries outside the window
        _rate_counters[self.tenant_id] = [t for t in bucket if t > window_start]

        if len(_rate_counters[self.tenant_id]) >= limit:
            raise PlanLimitExceeded(
                f"Rate limit exceeded: {limit} webhook requests per minute on the "
                f"{self.plan.display_name} plan. Slow down or upgrade."
            )
        _rate_counters[self.tenant_id].append(now)

    # ── Monthly volume ────────────────────────────────────────────────────────

    def check_monthly_volume(self) -> None:
        limit = self.plan.max_monthly_orders
        if limit == -1:
            return  # unlimited
        used = self.subscription.orders_this_period
        if used >= limit:
            raise PlanLimitExceeded(
                f"Monthly order limit reached: {used}/{limit} orders used this period "
                f"on the {self.plan.display_name} plan. Upgrade for more orders."
            )

    # ── Open orders ────────────────────────────────────────────────────────────

    async def check_open_orders(self, db: AsyncSession) -> None:
        limit = self.plan.max_open_orders
        if limit == -1:
            return  # unlimited

        result = await db.execute(
            select(func.count(Order.id)).where(
                Order.tenant_id == self.tenant_id,
                Order.status == "open",
            )
        )
        count = result.scalar_one()
        if count >= limit:
            raise PlanLimitExceeded(
                f"Open order limit reached: {count}/{limit} open orders on the "
                f"{self.plan.display_name} plan. Cancel an open order or upgrade."
            )

    # ── Broker accounts ───────────────────────────────────────────────────────

    async def check_broker_account_limit(self, db: AsyncSession) -> None:
        limit = self.plan.max_broker_accounts
        if limit == -1:
            return  # unlimited

        result = await db.execute(
            select(func.count(BrokerAccount.id)).where(
                BrokerAccount.tenant_id == self.tenant_id,
                BrokerAccount.is_active == True,  # noqa: E712
            )
        )
        count = result.scalar_one()
        if count >= limit:
            raise PlanLimitExceeded(
                f"Broker account limit reached: {count}/{limit} accounts on the "
                f"{self.plan.display_name} plan. Upgrade to connect more brokers."
            )
