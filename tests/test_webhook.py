"""
Core webhook endpoint tests — updated for per-tenant routing.
"""
import pytest
from unittest.mock import patch, AsyncMock
from app.brokers.base import BrokerOrderResult


VALID_PAYLOAD = {
    "secret": "ignored",
    "broker": "oanda",
    "account": "primary",
    "action": "buy",
    "symbol": "EUR_USD",
    "instrument_type": "forex",
    "order_type": "market",
    "quantity": 1000,
}


@pytest.mark.asyncio
async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_missing_api_key_rejected(client, registered_tenant, api_key):
    _, tenant_id = api_key
    resp = await client.post(f"/webhook/{tenant_id}", json=VALID_PAYLOAD)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_wrong_api_key_rejected(client, registered_tenant, api_key):
    _, tenant_id = api_key
    resp = await client.post(
        f"/webhook/{tenant_id}", json=VALID_PAYLOAD,
        headers={"X-Webhook-Secret": "tvr_wrong_totally_fake"}
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_missing_fields_rejected(client, registered_tenant, api_key):
    raw_key, tenant_id = api_key
    resp = await client.post(
        f"/webhook/{tenant_id}",
        json={"broker": "oanda"},
        headers={"X-Webhook-Secret": raw_key},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_negative_quantity_rejected(client, registered_tenant, api_key):
    raw_key, tenant_id = api_key
    resp = await client.post(
        f"/webhook/{tenant_id}",
        json={**VALID_PAYLOAD, "quantity": -100},
        headers={"X-Webhook-Secret": raw_key},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_unknown_broker_rejected(client, registered_tenant, api_key):
    raw_key, tenant_id = api_key
    resp = await client.post(
        f"/webhook/{tenant_id}",
        json={**VALID_PAYLOAD, "broker": "robinhood"},
        headers={"X-Webhook-Secret": raw_key},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_successful_order(client, registered_tenant, api_key, oanda_broker_account):
    raw_key, tenant_id = api_key
    mock_result = BrokerOrderResult(
        success=True, broker_order_id="broker-123",
        filled_quantity=1000.0, avg_fill_price=1.0850,
    )
    with patch("app.brokers.oanda.OandaBroker.submit_order", new_callable=AsyncMock) as m:
        m.return_value = mock_result
        resp = await client.post(
            f"/webhook/{tenant_id}", json=VALID_PAYLOAD,
            headers={"X-Webhook-Secret": raw_key},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "filled"
    assert data["broker_order_id"] == "broker-123"


@pytest.mark.asyncio
async def test_broker_rejection_stored(client, registered_tenant, api_key, oanda_broker_account):
    raw_key, tenant_id = api_key
    mock_result = BrokerOrderResult(success=False, error_message="Insufficient margin")
    with patch("app.brokers.oanda.OandaBroker.submit_order", new_callable=AsyncMock) as m:
        m.return_value = mock_result
        resp = await client.post(
            f"/webhook/{tenant_id}", json=VALID_PAYLOAD,
            headers={"X-Webhook-Secret": raw_key},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "rejected"
    assert "margin" in data["message"]


@pytest.mark.asyncio
async def test_duplicate_signal_suppressed(client, registered_tenant, api_key, oanda_broker_account):
    raw_key, tenant_id = api_key
    mock_result = BrokerOrderResult(success=True, broker_order_id="x", filled_quantity=1000)

    from app.services import order_processor
    order_processor._recent_signals.clear()  # ensure clean state

    with patch("app.brokers.oanda.OandaBroker.submit_order", new_callable=AsyncMock) as m:
        m.return_value = mock_result
        resp1 = await client.post(
            f"/webhook/{tenant_id}", json=VALID_PAYLOAD,
            headers={"X-Webhook-Secret": raw_key},
        )
        assert resp1.status_code == 200

        resp2 = await client.post(
            f"/webhook/{tenant_id}", json=VALID_PAYLOAD,
            headers={"X-Webhook-Secret": raw_key},
        )
        assert resp2.status_code == 422
        assert "Duplicate" in resp2.json()["detail"]
