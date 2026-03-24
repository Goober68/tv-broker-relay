"""
Risk enforcement tests — position size, daily loss limit.
"""
import pytest
from unittest.mock import patch, AsyncMock
from app.brokers.base import BrokerOrderResult


def payload(**overrides):
    return {
        "secret": "ignored", "broker": "oanda", "account": "primary",
        "action": "buy", "symbol": "EUR_USD",
        "instrument_type": "forex", "order_type": "market", "quantity": 1000,
        **overrides,
    }


@pytest.mark.asyncio
async def test_max_position_size_enforced(client, registered_tenant, api_key, oanda_broker_account):
    raw_key, tenant_id = api_key
    resp = await client.post(
        f"/webhook/{tenant_id}",
        json=payload(quantity=999_999_999),
        headers={"X-Webhook-Secret": raw_key},
    )
    assert resp.status_code == 422
    assert "max" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_daily_loss_limit_enforced(client, registered_tenant, api_key,
                                          oanda_broker_account, db_session):
    raw_key, tenant_id = api_key

    from app.services.state import get_or_create_position
    from datetime import datetime, timezone
    pos = await get_or_create_position(db_session, tenant_id, "oanda", "primary", "EUR_USD")
    pos.daily_realized_pnl = -10_000
    pos.daily_pnl_date = datetime.now(timezone.utc)
    await db_session.commit()

    resp = await client.post(
        f"/webhook/{tenant_id}",
        json=payload(),
        headers={"X-Webhook-Secret": raw_key},
    )
    assert resp.status_code == 422
    assert "daily loss" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_status_endpoints_require_auth(client):
    resp = await client.get("/api/orders")
    assert resp.status_code == 401

    resp = await client.get("/api/positions")
    assert resp.status_code == 401

    resp = await client.get("/api/orders/open")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_orders_filterable_by_broker(client, auth_headers):
    resp = await client.get("/api/orders?broker=oanda", headers=auth_headers)
    assert resp.status_code == 200

    resp = await client.get("/api/orders?status=filled", headers=auth_headers)
    assert resp.status_code == 200
