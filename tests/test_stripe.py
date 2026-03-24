"""
Tests for Step 5: Stripe integration.

Covers:
  - POST /billing/checkout → returns URL, requires paid plan
  - POST /billing/portal → requires existing Stripe customer
  - POST /billing/webhook → signature verification, all event handlers
  - checkout.session.completed → plan activated, Stripe IDs stored
  - customer.subscription.updated → status/period/plan updated
  - customer.subscription.deleted → downgraded to Free
  - invoice.paid → period counter reset
  - invoice.payment_failed → status set to past_due
  - Unhandled event types → 200 acknowledged silently
  - Missing/invalid signature → 400
  - Free plan blocks checkout
  - Plan without price_id blocks checkout
"""
import json
import time
import pytest
import stripe
from unittest.mock import patch, AsyncMock, MagicMock
from datetime import datetime, timezone, timedelta

from app.services.plans import get_subscription, assign_plan


# ── Fixtures ───────────────────────────────────────────────────────────────────

def make_stripe_event(event_type: str, data: dict) -> stripe.Event:
    """Construct a stripe.Event object from raw dict (bypasses signature check)."""
    return stripe.Event.construct_from(
        {
            "id": f"evt_test_{event_type.replace('.', '_')}",
            "type": event_type,
            "data": {"object": data},
            "livemode": False,
            "created": int(time.time()),
            "api_version": "2024-06-20",
            "object": "event",
        },
        stripe.api_key or "sk_test_fake",
    )


def mock_stripe_client(checkout_url: str = "https://checkout.stripe.com/test"):
    """Return a mock StripeClient with the most-used methods stubbed."""
    client = MagicMock()
    # customers.create
    mock_customer = MagicMock()
    mock_customer.id = "cus_test123"
    client.customers.create.return_value = mock_customer
    # checkout.sessions.create
    mock_session = MagicMock()
    mock_session.id = "cs_test123"
    mock_session.url = checkout_url
    client.checkout.sessions.create.return_value = mock_session
    # billing_portal.sessions.create
    mock_portal = MagicMock()
    mock_portal.url = "https://billing.stripe.com/test-portal"
    client.billing_portal.sessions.create.return_value = mock_portal
    return client


# ── Checkout ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_checkout_returns_url(client, auth_headers, registered_tenant, db_session):
    """Pro plan checkout should return a Stripe-hosted URL."""
    # Set a stripe_price_id on the pro plan
    from sqlalchemy import select
    from app.models.plan import Plan
    result = await db_session.execute(select(Plan).where(Plan.name == "pro"))
    plan = result.scalar_one()
    plan.stripe_price_id = "price_pro_test"
    await db_session.commit()

    mock_client = mock_stripe_client("https://checkout.stripe.com/pay/cs_test123")

    with patch("app.services.stripe_service._stripe_client", return_value=mock_client):
        resp = await client.post(
            "/billing/checkout",
            json={"plan_name": "pro"},
            headers=auth_headers,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert "url" in data
    assert "checkout.stripe.com" in data["url"]


@pytest.mark.asyncio
async def test_checkout_free_plan_rejected(client, auth_headers):
    """Cannot create a checkout session for the Free plan."""
    resp = await client.post(
        "/billing/checkout",
        json={"plan_name": "free"},
        headers=auth_headers,
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_checkout_plan_without_price_id_rejected(client, auth_headers):
    """Plans without a Stripe price ID configured should return 422."""
    # pro plan has no stripe_price_id by default in tests
    resp = await client.post(
        "/billing/checkout",
        json={"plan_name": "pro"},
        headers=auth_headers,
    )
    assert resp.status_code == 422
    assert "price" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_checkout_requires_auth(client):
    resp = await client.post("/billing/checkout", json={"plan_name": "pro"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_checkout_creates_stripe_customer(client, auth_headers, registered_tenant, db_session):
    """First checkout should create a Stripe customer and persist the ID."""
    from sqlalchemy import select
    from app.models.plan import Plan
    result = await db_session.execute(select(Plan).where(Plan.name == "pro"))
    plan = result.scalar_one()
    plan.stripe_price_id = "price_pro_test"
    await db_session.commit()

    mock_client = mock_stripe_client()

    with patch("app.services.stripe_service._stripe_client", return_value=mock_client):
        await client.post("/billing/checkout", json={"plan_name": "pro"}, headers=auth_headers)

    sub = await get_subscription(db_session, registered_tenant["id"])
    await db_session.refresh(sub)
    assert sub.stripe_customer_id == "cus_test123"


# ── Portal ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_portal_requires_existing_customer(client, auth_headers):
    """Tenants without a Stripe customer ID cannot access the portal."""
    resp = await client.post("/billing/portal", headers=auth_headers)
    assert resp.status_code == 422
    assert "checkout" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_portal_returns_url(client, auth_headers, registered_tenant, db_session):
    """With a customer ID, portal should return Stripe URL."""
    sub = await get_subscription(db_session, registered_tenant["id"])
    sub.stripe_customer_id = "cus_existing"
    await db_session.commit()

    mock_client = mock_stripe_client()

    with patch("app.services.stripe_service._stripe_client", return_value=mock_client):
        resp = await client.post("/billing/portal", headers=auth_headers)

    assert resp.status_code == 200
    assert "billing.stripe.com" in resp.json()["url"]


# ── Webhook Signature ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_webhook_missing_signature_returns_400(client):
    resp = await client.post(
        "/billing/webhook",
        content=b'{"type": "checkout.session.completed"}',
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_webhook_invalid_signature_returns_400(client):
    with patch(
        "app.services.stripe_service.verify_webhook_signature",
        side_effect=stripe.SignatureVerificationError("bad sig", "header"),
    ):
        resp = await client.post(
            "/billing/webhook",
            content=b'{}',
            headers={
                "Content-Type": "application/json",
                "stripe-signature": "t=bad,v1=badsig",
            },
        )
    assert resp.status_code == 400


# ── checkout.session.completed ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_webhook_checkout_completed_activates_plan(client, registered_tenant, db_session):
    """Stripe webhook should activate Pro plan after successful checkout."""
    tenant_id = registered_tenant["id"]

    event = make_stripe_event("checkout.session.completed", {
        "id": "cs_test",
        "subscription": "sub_test_pro",
        "customer": "cus_test",
        "metadata": {"tenant_id": str(tenant_id), "plan_name": "pro"},
        "object": "checkout.session",
    })

    with patch("app.services.stripe_service.verify_webhook_signature", return_value=event):
        resp = await client.post(
            "/billing/webhook",
            content=b"{}",
            headers={"stripe-signature": "t=1,v1=sig"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"received": True}

    # Subscription should now be on Pro plan
    sub = await get_subscription(db_session, tenant_id)
    await db_session.refresh(sub)
    assert sub.stripe_subscription_id == "sub_test_pro"
    assert sub.stripe_customer_id == "cus_test"
    assert sub.status == "active"

    from sqlalchemy.orm import selectinload
    from sqlalchemy import select
    from app.models.plan import Subscription as SubModel
    result = await db_session.execute(
        select(SubModel).where(SubModel.id == sub.id).options(selectinload(SubModel.plan))
    )
    sub_with_plan = result.scalar_one()
    assert sub_with_plan.plan.name == "pro"


@pytest.mark.asyncio
async def test_webhook_checkout_completed_missing_tenant_id(client):
    """Events with missing tenant_id in metadata should be handled gracefully."""
    event = make_stripe_event("checkout.session.completed", {
        "id": "cs_test",
        "subscription": "sub_test",
        "customer": "cus_test",
        "metadata": {},  # no tenant_id
        "object": "checkout.session",
    })

    with patch("app.services.stripe_service.verify_webhook_signature", return_value=event):
        resp = await client.post(
            "/billing/webhook",
            content=b"{}",
            headers={"stripe-signature": "t=1,v1=sig"},
        )

    # Should still 200 — we log the error but don't 500
    assert resp.status_code == 200


# ── customer.subscription.updated ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_webhook_subscription_updated_stores_period(client, registered_tenant, db_session):
    """subscription.updated should update period dates and status."""
    tenant_id = registered_tenant["id"]
    sub = await get_subscription(db_session, tenant_id)
    sub.stripe_subscription_id = "sub_existing"
    sub.stripe_customer_id = "cus_existing"
    await db_session.commit()

    period_start = int(datetime.now(timezone.utc).timestamp())
    period_end = int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp())

    event = make_stripe_event("customer.subscription.updated", {
        "id": "sub_existing",
        "customer": "cus_existing",
        "status": "active",
        "current_period_start": period_start,
        "current_period_end": period_end,
        "metadata": {"tenant_id": str(tenant_id)},
        "items": {"data": [{"price": {"id": "price_unknown"}}]},
        "object": "subscription",
    })

    with patch("app.services.stripe_service.verify_webhook_signature", return_value=event):
        resp = await client.post(
            "/billing/webhook",
            content=b"{}",
            headers={"stripe-signature": "t=1,v1=sig"},
        )

    assert resp.status_code == 200
    await db_session.refresh(sub)
    assert sub.status == "active"
    assert sub.current_period_end is not None


@pytest.mark.asyncio
async def test_webhook_subscription_updated_changes_plan(client, registered_tenant, db_session):
    """If the Stripe price matches a different plan, we should update plan_id."""
    tenant_id = registered_tenant["id"]
    sub = await get_subscription(db_session, tenant_id)
    sub.stripe_customer_id = "cus_plan_changer"
    await db_session.commit()

    # Set stripe_price_id on enterprise plan
    from sqlalchemy import select
    from app.models.plan import Plan
    result = await db_session.execute(select(Plan).where(Plan.name == "enterprise"))
    enterprise = result.scalar_one()
    enterprise.stripe_price_id = "price_enterprise_test"
    await db_session.commit()

    event = make_stripe_event("customer.subscription.updated", {
        "id": "sub_change",
        "customer": "cus_plan_changer",
        "status": "active",
        "current_period_start": int(time.time()),
        "current_period_end": int(time.time()) + 2592000,
        "metadata": {"tenant_id": str(tenant_id)},
        "items": {"data": [{"price": {"id": "price_enterprise_test"}}]},
        "object": "subscription",
    })

    with patch("app.services.stripe_service.verify_webhook_signature", return_value=event):
        await client.post("/billing/webhook", content=b"{}",
                          headers={"stripe-signature": "t=1,v1=sig"})

    from sqlalchemy.orm import selectinload
    from app.models.plan import Subscription as SubModel
    result = await db_session.execute(
        select(SubModel).where(SubModel.tenant_id == tenant_id)
        .options(selectinload(SubModel.plan))
    )
    sub_reloaded = result.scalar_one()
    assert sub_reloaded.plan.name == "enterprise"


# ── customer.subscription.deleted ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_webhook_subscription_deleted_downgrades_to_free(client, registered_tenant, db_session):
    """Cancellation should downgrade to Free, not delete the account."""
    tenant_id = registered_tenant["id"]

    # Promote to pro first
    await assign_plan(db_session, tenant_id, "pro",
                      stripe_subscription_id="sub_to_cancel",
                      stripe_customer_id="cus_canceller")
    await db_session.commit()

    event = make_stripe_event("customer.subscription.deleted", {
        "id": "sub_to_cancel",
        "customer": "cus_canceller",
        "status": "canceled",
        "metadata": {"tenant_id": str(tenant_id)},
        "object": "subscription",
    })

    with patch("app.services.stripe_service.verify_webhook_signature", return_value=event):
        resp = await client.post(
            "/billing/webhook",
            content=b"{}",
            headers={"stripe-signature": "t=1,v1=sig"},
        )

    assert resp.status_code == 200

    from sqlalchemy.orm import selectinload
    from sqlalchemy import select
    from app.models.plan import Subscription as SubModel
    result = await db_session.execute(
        select(SubModel).where(SubModel.tenant_id == tenant_id)
        .options(selectinload(SubModel.plan))
    )
    sub = result.scalar_one()
    assert sub.plan.name == "free"
    assert sub.stripe_subscription_id is None
    # Customer ID kept so they can re-subscribe
    assert sub.stripe_customer_id == "cus_canceller"


# ── invoice.paid ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_webhook_invoice_paid_resets_order_counter(client, registered_tenant, db_session):
    """invoice.paid should reset the monthly order counter."""
    tenant_id = registered_tenant["id"]
    sub = await get_subscription(db_session, tenant_id)
    sub.stripe_subscription_id = "sub_invoice_test"
    sub.orders_this_period = 42  # simulate some orders having been placed
    await db_session.commit()

    event = make_stripe_event("invoice.paid", {
        "id": "in_test",
        "subscription": "sub_invoice_test",
        "status": "paid",
        "object": "invoice",
    })

    with patch("app.services.stripe_service.verify_webhook_signature", return_value=event):
        resp = await client.post(
            "/billing/webhook",
            content=b"{}",
            headers={"stripe-signature": "t=1,v1=sig"},
        )

    assert resp.status_code == 200
    await db_session.refresh(sub)
    assert sub.orders_this_period == 0


@pytest.mark.asyncio
async def test_webhook_invoice_paid_one_off_charge_ignored(client, registered_tenant, db_session):
    """invoice.paid for a one-off charge (no subscription) should be a no-op."""
    tenant_id = registered_tenant["id"]
    sub = await get_subscription(db_session, tenant_id)
    sub.orders_this_period = 10
    await db_session.commit()

    event = make_stripe_event("invoice.paid", {
        "id": "in_oneoff",
        "subscription": None,  # one-off charge
        "status": "paid",
        "object": "invoice",
    })

    with patch("app.services.stripe_service.verify_webhook_signature", return_value=event):
        await client.post("/billing/webhook", content=b"{}",
                          headers={"stripe-signature": "t=1,v1=sig"})

    await db_session.refresh(sub)
    assert sub.orders_this_period == 10  # unchanged


# ── invoice.payment_failed ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_webhook_payment_failed_sets_past_due(client, registered_tenant, db_session):
    tenant_id = registered_tenant["id"]
    sub = await get_subscription(db_session, tenant_id)
    sub.stripe_subscription_id = "sub_failing"
    await db_session.commit()

    event = make_stripe_event("invoice.payment_failed", {
        "id": "in_fail",
        "subscription": "sub_failing",
        "object": "invoice",
    })

    with patch("app.services.stripe_service.verify_webhook_signature", return_value=event):
        resp = await client.post(
            "/billing/webhook",
            content=b"{}",
            headers={"stripe-signature": "t=1,v1=sig"},
        )

    assert resp.status_code == 200
    await db_session.refresh(sub)
    assert sub.status == "past_due"


# ── Unhandled events ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_webhook_unhandled_event_type_returns_200(client):
    """Unknown event types should be acknowledged silently."""
    event = make_stripe_event("payment_intent.created", {
        "id": "pi_test",
        "object": "payment_intent",
    })

    with patch("app.services.stripe_service.verify_webhook_signature", return_value=event):
        resp = await client.post(
            "/billing/webhook",
            content=b"{}",
            headers={"stripe-signature": "t=1,v1=sig"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"received": True}


# ── Handler errors don't 500 ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_webhook_handler_exception_still_returns_200(client, registered_tenant):
    """Even if a handler throws, we return 200 to prevent Stripe retrying forever."""
    event = make_stripe_event("checkout.session.completed", {
        "id": "cs_boom",
        "subscription": "sub_boom",
        "customer": "cus_boom",
        "metadata": {"tenant_id": "999999"},  # non-existent tenant
        "object": "checkout.session",
    })

    with patch("app.services.stripe_service.verify_webhook_signature", return_value=event):
        resp = await client.post(
            "/billing/webhook",
            content=b"{}",
            headers={"stripe-signature": "t=1,v1=sig"},
        )

    assert resp.status_code == 200
