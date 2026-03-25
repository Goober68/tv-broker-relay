"""
Stripe service.

Wraps the Stripe Python SDK for:
  - Customer creation / retrieval
  - Checkout session creation (hosted payment page)
  - Customer portal session creation (manage subscription)
  - Webhook event verification and routing

We use metadata.tenant_id on all Stripe objects to link them back to
our tenants without relying on email matching (which is fragile if
a tenant changes their email).

Stripe API docs: https://stripe.com/docs/api
"""
import uuid
import stripe
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.plan import Plan, Subscription
from app.services.plans import (
    get_subscription,
    get_plan_by_name,
    assign_plan,
    reset_period_counter,
)

import logging
logger = logging.getLogger(__name__)


def _stripe_client() -> stripe.StripeClient:
    settings = get_settings()
    if not settings.stripe_secret_key:
        raise RuntimeError(
            "STRIPE_SECRET_KEY is not configured. "
            "Set it in your .env file before using billing features."
        )
    return stripe.StripeClient(settings.stripe_secret_key)


# ── Customer ───────────────────────────────────────────────────────────────────

async def get_or_create_stripe_customer(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    email: str,
) -> str:
    """
    Return the Stripe customer ID for this tenant, creating one if needed.
    Stores the customer ID on the Subscription row.
    """
    sub = await get_subscription(db, tenant_id)
    if sub and sub.stripe_customer_id:
        return sub.stripe_customer_id

    client = _stripe_client()
    customer = client.customers.create(params={
        "email": email,
        "metadata": {"tenant_id": str(tenant_id)},
    })

    # Persist immediately so we don't create duplicates on retry
    if sub:
        sub.stripe_customer_id = customer.id
        await db.flush()

    logger.info(f"Created Stripe customer {customer.id} for tenant {tenant_id}")
    return customer.id


# ── Checkout ───────────────────────────────────────────────────────────────────

async def create_checkout_session(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    email: str,
    plan_name: str,
) -> str:
    """
    Create a Stripe Checkout session for upgrading to the given plan.
    Returns the hosted checkout URL to redirect the tenant to.
    """
    settings = get_settings()
    plan = await get_plan_by_name(db, plan_name)

    if plan is None:
        raise ValueError(f"Plan {plan_name!r} not found")
    if plan.name == "free":
        raise ValueError("Cannot create a checkout session for the Free plan")
    if not plan.stripe_price_id:
        raise ValueError(
            f"Plan {plan_name!r} has no Stripe price ID configured. "
            "Set it via PATCH /admin/plans/{id}/stripe-price."
        )

    customer_id = await get_or_create_stripe_customer(db, tenant_id, email)
    await db.commit()  # persist customer_id before redirect

    client = _stripe_client()
    session = client.checkout.sessions.create(params={
        "customer": customer_id,
        "mode": "subscription",
        "line_items": [{"price": plan.stripe_price_id, "quantity": 1}],
        "success_url": settings.stripe_success_url + "?session_id={CHECKOUT_SESSION_ID}",
        "cancel_url": settings.stripe_cancel_url,
        # tenant_id in metadata lets the webhook handler identify who subscribed
        "metadata": {
            "tenant_id": str(tenant_id),
            "plan_name": plan_name,
        },
        "subscription_data": {
            "metadata": {
                "tenant_id": str(tenant_id),
                "plan_name": plan_name,
            }
        },
        # Pre-fill email so the tenant doesn't have to type it again
        "customer_email": None,  # customer object already has it
        # Allow promo codes
        "allow_promotion_codes": True,
    })

    logger.info(
        f"Created checkout session {session.id} for tenant {tenant_id} → plan {plan_name}"
    )
    return session.url


# ── Customer Portal ────────────────────────────────────────────────────────────

async def create_portal_session(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    email: str,
    return_url: str,
) -> str:
    """
    Create a Stripe Customer Portal session.
    The portal lets tenants manage their subscription, update payment methods,
    download invoices, and cancel.
    Returns the portal URL to redirect the tenant to.
    """
    customer_id = await get_or_create_stripe_customer(db, tenant_id, email)
    await db.commit()

    client = _stripe_client()
    session = client.billing_portal.sessions.create(params={
        "customer": customer_id,
        "return_url": return_url,
    })

    logger.info(f"Created portal session for tenant {tenant_id} customer {customer_id}")
    return session.url


# ── Webhook Event Verification ─────────────────────────────────────────────────

def verify_webhook_signature(payload: bytes, sig_header: str) -> stripe.Event:
    """
    Verify the Stripe-Signature header and return the parsed event.
    Raises stripe.SignatureVerificationError on failure.
    """
    settings = get_settings()
    if not settings.stripe_webhook_secret:
        raise RuntimeError("STRIPE_WEBHOOK_SECRET is not configured")

    return stripe.Webhook.construct_event(
        payload=payload,
        sig_header=sig_header,
        secret=settings.stripe_webhook_secret,
    )


# ── Webhook Event Handlers ─────────────────────────────────────────────────────

async def handle_checkout_completed(db: AsyncSession, event: stripe.Event) -> None:
    """
    checkout.session.completed — tenant completed payment, activate their plan.
    """
    session = event.data.object
    tenant_id = int(session.metadata.get("tenant_id", 0))
    plan_name = session.metadata.get("plan_name", "pro")

    if not tenant_id:
        logger.error(f"checkout.session.completed missing tenant_id in metadata: {session.id}")
        return

    stripe_subscription_id = session.subscription
    stripe_customer_id = session.customer

    await assign_plan(
        db, tenant_id, plan_name,
        stripe_customer_id=stripe_customer_id,
        stripe_subscription_id=stripe_subscription_id,
    )
    await db.commit()
    logger.info(
        f"Checkout completed: tenant {tenant_id} → plan {plan_name} "
        f"(sub={stripe_subscription_id})"
    )


async def handle_subscription_updated(db: AsyncSession, event: stripe.Event) -> None:
    """
    customer.subscription.updated — plan change, renewal, or status change.
    Updates status, period dates, and plan if changed.
    """
    stripe_sub = event.data.object
    tenant_id_str = stripe_sub.metadata.get("tenant_id")

    if not tenant_id_str:
        # Try to find tenant by customer ID
        sub = await _find_sub_by_stripe_customer(db, stripe_sub.customer)
        if sub is None:
            logger.warning(f"subscription.updated: cannot find tenant for customer {stripe_sub.customer}")
            return
        tenant_id = sub.tenant_id
    else:
        tenant_id = int(tenant_id_str)

    sub = await get_subscription(db, tenant_id)
    if sub is None:
        logger.warning(f"subscription.updated: no subscription found for tenant {tenant_id}")
        return

    # Update status
    sub.status = stripe_sub.status  # active, past_due, canceled, trialing, etc.
    sub.stripe_subscription_id = stripe_sub.id
    sub.stripe_customer_id = stripe_sub.customer

    # Update billing period dates
    if stripe_sub.current_period_start:
        sub.current_period_start = datetime.fromtimestamp(
            stripe_sub.current_period_start, tz=timezone.utc
        )
    if stripe_sub.current_period_end:
        sub.current_period_end = datetime.fromtimestamp(
            stripe_sub.current_period_end, tz=timezone.utc
        )

    # If the plan changed (e.g. upgrade/downgrade via portal), update it
    new_price_id = None
    if stripe_sub.items and stripe_sub.items.data:
        new_price_id = stripe_sub.items.data[0].price.id

    if new_price_id:
        from sqlalchemy import select
        result = await db.execute(
            select(Plan).where(Plan.stripe_price_id == new_price_id)
        )
        new_plan = result.scalar_one_or_none()
        if new_plan and new_plan.id != sub.plan_id:
            logger.info(
                f"Plan change for tenant {tenant_id}: "
                f"plan_id {sub.plan_id} → {new_plan.id} ({new_plan.name})"
            )
            sub.plan_id = new_plan.id

    await db.commit()
    logger.info(
        f"subscription.updated: tenant {tenant_id} status={sub.status} "
        f"period_end={sub.current_period_end}"
    )


async def handle_subscription_deleted(db: AsyncSession, event: stripe.Event) -> None:
    """
    customer.subscription.deleted — subscription cancelled.
    Downgrade to Free rather than deleting the account.
    """
    stripe_sub = event.data.object
    tenant_id_str = stripe_sub.metadata.get("tenant_id")

    if not tenant_id_str:
        sub = await _find_sub_by_stripe_customer(db, stripe_sub.customer)
        if sub is None:
            logger.warning(f"subscription.deleted: cannot find tenant for customer {stripe_sub.customer}")
            return
        tenant_id = sub.tenant_id
    else:
        tenant_id = int(tenant_id_str)

    await assign_plan(db, tenant_id, "free")

    # Clear Stripe subscription ID — customer ID stays for re-subscribing
    sub = await get_subscription(db, tenant_id)
    if sub:
        sub.stripe_subscription_id = None
        sub.current_period_end = None

    await db.commit()
    logger.info(f"subscription.deleted: tenant {tenant_id} downgraded to free")


async def handle_invoice_paid(db: AsyncSession, event: stripe.Event) -> None:
    """
    invoice.paid — new billing period started, reset the order counter.
    Only reset on subscription invoices (not one-off charges).
    """
    invoice = event.data.object
    if invoice.subscription is None:
        return  # one-off charge, not a subscription renewal

    # Find tenant by stripe subscription ID
    from sqlalchemy import select
    result = await db.execute(
        select(Subscription).where(
            Subscription.stripe_subscription_id == invoice.subscription
        )
    )
    sub = result.scalar_one_or_none()
    if sub is None:
        logger.warning(f"invoice.paid: no subscription found for stripe_sub {invoice.subscription}")
        return

    await reset_period_counter(db, sub.tenant_id)
    await db.commit()
    logger.info(f"invoice.paid: reset order counter for tenant {sub.tenant_id}")


async def handle_invoice_payment_failed(db: AsyncSession, event: stripe.Event) -> None:
    """
    invoice.payment_failed — payment failed, Stripe will retry.
    Updates status to past_due and sends an email alert.
    """
    invoice = event.data.object
    if not invoice.subscription:
        return

    from sqlalchemy import select
    from app.models.tenant import Tenant
    result = await db.execute(
        select(Subscription).where(
            Subscription.stripe_subscription_id == invoice.subscription
        )
    )
    sub = result.scalar_one_or_none()
    if sub:
        sub.status = "past_due"
        await db.commit()
        logger.warning(f"invoice.payment_failed: tenant {sub.tenant_id} is past_due")

        # Send payment failed email
        from sqlalchemy.orm import selectinload
        result2 = await db.execute(
            select(Tenant).where(Tenant.id == sub.tenant_id)
        )
        tenant = result2.scalar_one_or_none()
        if tenant:
            from app.services.email_service import send_payment_failed
            result3 = await db.execute(
                select(Subscription).where(Subscription.id == sub.id)
                .options(selectinload(Subscription.plan))
            )
            sub_with_plan = result3.scalar_one_or_none()
            plan_name = sub_with_plan.plan.display_name if sub_with_plan else "Pro"
            await send_payment_failed(tenant.email, plan_name)


# ── Internal helpers ───────────────────────────────────────────────────────────

async def _find_sub_by_stripe_customer(
    db: AsyncSession, customer_id: str
) -> Subscription | None:
    from sqlalchemy import select
    result = await db.execute(
        select(Subscription).where(Subscription.stripe_customer_id == customer_id)
    )
    return result.scalar_one_or_none()
