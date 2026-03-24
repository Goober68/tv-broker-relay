"""
Billing router — tenant-facing subscription, plan, checkout and portal endpoints.
Also handles the inbound Stripe webhook.
"""
import logging
from fastapi import APIRouter, Depends, HTTPException, Request, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from pydantic import BaseModel
from datetime import datetime
from typing import Literal
import stripe

from app.models.db import get_db
from app.models.tenant import Tenant
from app.models.plan import Plan, Subscription
from app.dependencies.auth import get_current_tenant
from app.services.plans import get_or_create_subscription
from app.services.stripe_service import (
    create_checkout_session,
    create_portal_session,
    verify_webhook_signature,
    handle_checkout_completed,
    handle_subscription_updated,
    handle_subscription_deleted,
    handle_invoice_paid,
    handle_invoice_payment_failed,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/billing", tags=["billing"])


# ── Schemas ────────────────────────────────────────────────────────────────────

class PlanPublicOut(BaseModel):
    name: str
    display_name: str
    max_broker_accounts: int
    max_monthly_orders: int
    max_open_orders: int
    requests_per_minute: int
    allowed_order_types: list | None

    class Config:
        from_attributes = True


class SubscriptionPublicOut(BaseModel):
    plan: PlanPublicOut
    status: str
    orders_this_period: int
    orders_remaining: int | None  # None = unlimited
    current_period_start: datetime | None
    current_period_end: datetime | None
    stripe_customer_id: str | None  # exposed so frontend can show "managed by Stripe"


class CheckoutRequest(BaseModel):
    plan_name: Literal["pro", "enterprise"]


class CheckoutResponse(BaseModel):
    url: str  # Stripe-hosted checkout URL — redirect the tenant here


class PortalResponse(BaseModel):
    url: str  # Stripe customer portal URL


# ── Subscription info ──────────────────────────────────────────────────────────

@router.get("/subscription", response_model=SubscriptionPublicOut)
async def get_my_subscription(
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Return the current tenant's plan, usage, and billing status."""
    sub = await get_or_create_subscription(db, tenant.id)

    result = await db.execute(
        select(Subscription)
        .where(Subscription.id == sub.id)
        .options(selectinload(Subscription.plan))
    )
    sub = result.scalar_one()
    plan = sub.plan

    limit = plan.max_monthly_orders
    remaining = None if limit == -1 else max(0, limit - sub.orders_this_period)

    return SubscriptionPublicOut(
        plan=PlanPublicOut(
            name=plan.name,
            display_name=plan.display_name,
            max_broker_accounts=plan.max_broker_accounts,
            max_monthly_orders=plan.max_monthly_orders,
            max_open_orders=plan.max_open_orders,
            requests_per_minute=plan.requests_per_minute,
            allowed_order_types=plan.allowed_order_types,
        ),
        status=sub.status,
        orders_this_period=sub.orders_this_period,
        orders_remaining=remaining,
        current_period_start=sub.current_period_start,
        current_period_end=sub.current_period_end,
        stripe_customer_id=sub.stripe_customer_id,
    )


@router.get("/plans", response_model=list[PlanPublicOut])
async def list_available_plans(
    _tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """List all plans. Use this to render an upgrade/pricing page."""
    result = await db.execute(
        select(Plan).where(Plan.is_active == True).order_by(Plan.id)  # noqa: E712
    )
    return result.scalars().all()


# ── Checkout ───────────────────────────────────────────────────────────────────

@router.post("/checkout", response_model=CheckoutResponse)
async def create_checkout(
    body: CheckoutRequest,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    Create a Stripe Checkout session for upgrading to Pro or Enterprise.
    Returns a URL — the client should redirect to it.

    Flow:
      1. Client POSTs here with {"plan_name": "pro"}
      2. This returns {"url": "https://checkout.stripe.com/..."}
      3. Client redirects to that URL
      4. Tenant completes payment on Stripe's hosted page
      5. Stripe redirects to STRIPE_SUCCESS_URL
      6. Stripe sends webhook → POST /billing/webhook → plan activated
    """
    try:
        url = await create_checkout_session(
            db, tenant.id, tenant.email, body.plan_name
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    return CheckoutResponse(url=url)


# ── Customer Portal ────────────────────────────────────────────────────────────

@router.post("/portal", response_model=PortalResponse)
async def create_portal(
    request: Request,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    Create a Stripe Customer Portal session.
    The portal lets tenants update payment methods, view invoices, and cancel.
    Returns a URL — the client should redirect to it.

    Requires the tenant to already have a Stripe customer ID
    (i.e. they've completed at least one checkout).
    """
    from app.services.plans import get_subscription
    sub = await get_subscription(db, tenant.id)
    if not sub or not sub.stripe_customer_id:
        raise HTTPException(
            status_code=422,
            detail="No active Stripe subscription found. "
                   "Complete a checkout first before accessing the billing portal."
        )

    # Return URL: wherever the tenant came from, or a sensible default
    return_url = str(request.base_url) + "billing"

    try:
        url = await create_portal_session(db, tenant.id, tenant.email, return_url)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    return PortalResponse(url=url)


# ── Stripe Webhook ─────────────────────────────────────────────────────────────

@router.post("/webhook", include_in_schema=False)
async def stripe_webhook(
    request: Request,
    stripe_signature: str | None = Header(default=None, alias="stripe-signature"),
    db: AsyncSession = Depends(get_db),
):
    """
    Inbound webhook from Stripe. NOT authenticated via JWT — verified via
    stripe-signature header instead (HMAC-SHA256 of the raw payload).

    This endpoint must receive the raw bytes — do NOT let FastAPI parse the body
    as JSON first. We read request.body() directly.

    Registered events (configure in Stripe dashboard or via CLI):
      - checkout.session.completed
      - customer.subscription.updated
      - customer.subscription.deleted
      - invoice.paid
      - invoice.payment_failed
    """
    if not stripe_signature:
        raise HTTPException(status_code=400, detail="Missing stripe-signature header")

    raw_body = await request.body()

    try:
        event = verify_webhook_signature(raw_body, stripe_signature)
    except stripe.SignatureVerificationError as e:
        logger.warning(f"Stripe webhook signature verification failed: {e}")
        raise HTTPException(status_code=400, detail="Invalid webhook signature")
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    event_type = event.type
    logger.info(f"Stripe webhook received: {event_type} (id={event.id})")

    try:
        match event_type:
            case "checkout.session.completed":
                await handle_checkout_completed(db, event)
            case "customer.subscription.updated":
                await handle_subscription_updated(db, event)
            case "customer.subscription.deleted":
                await handle_subscription_deleted(db, event)
            case "invoice.paid":
                await handle_invoice_paid(db, event)
            case "invoice.payment_failed":
                await handle_invoice_payment_failed(db, event)
            case _:
                # Acknowledge unhandled events so Stripe doesn't retry them
                logger.debug(f"Unhandled Stripe event type: {event_type}")
    except Exception:
        logger.exception(f"Error handling Stripe event {event_type} (id={event.id})")
        # Return 200 anyway — returning 5xx causes Stripe to retry indefinitely.
        # Log the error and handle manually if needed.

    # Stripe requires a 200 response to acknowledge receipt
    return {"received": True}
