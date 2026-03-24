"""
Tests for Step 2: API keys + per-tenant webhook routing.

Covers:
  - API key creation, listing, revocation
  - Key format and prefix
  - Max keys enforcement
  - Webhook routing to correct tenant via URL + header
  - Secret validation (wrong key, revoked key, wrong tenant, missing header)
  - Tenant isolation (one tenant cannot see another's orders/positions)
  - Status endpoints now require auth and return only the tenant's data
"""
import pytest
from unittest.mock import patch, AsyncMock
from app.brokers.base import BrokerOrderResult


# ── API Key CRUD ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_api_key(client, auth_headers):
    resp = await client.post(
        "/api-keys",
        json={"name": "My TradingView Key"},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "My TradingView Key"
    assert data["is_active"] is True
    assert "raw_key" in data
    assert data["raw_key"].startswith("tvr_")
    assert data["key_prefix"] in data["raw_key"]  # prefix is a substring


@pytest.mark.asyncio
async def test_raw_key_not_returned_on_list(client, auth_headers, api_key):
    resp = await client.get("/api-keys", headers=auth_headers)
    assert resp.status_code == 200
    keys = resp.json()
    assert len(keys) >= 1
    for k in keys:
        assert "raw_key" not in k


@pytest.mark.asyncio
async def test_list_api_keys_requires_auth(client):
    resp = await client.get("/api-keys")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_revoke_api_key(client, auth_headers):
    create_resp = await client.post(
        "/api-keys", json={"name": "Temp Key"}, headers=auth_headers
    )
    key_id = create_resp.json()["id"]

    resp = await client.delete(f"/api-keys/{key_id}", headers=auth_headers)
    assert resp.status_code == 204

    # Key should show as inactive
    list_resp = await client.get("/api-keys", headers=auth_headers)
    key = next(k for k in list_resp.json() if k["id"] == key_id)
    assert key["is_active"] is False


@pytest.mark.asyncio
async def test_revoke_nonexistent_key(client, auth_headers):
    resp = await client.delete("/api-keys/99999", headers=auth_headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_cannot_revoke_another_tenants_key(client):
    """Tenant B cannot revoke Tenant A's key."""
    # Register tenant A and create a key
    await client.post("/auth/register", json={"email": "tenant_a@example.com", "password": "passw0rd"})
    login_a = await client.post("/auth/login", json={"email": "tenant_a@example.com", "password": "passw0rd"})
    headers_a = {"Authorization": f"Bearer {login_a.json()['access_token']}"}
    key_resp = await client.post("/api-keys", json={"name": "A key"}, headers=headers_a)
    key_id = key_resp.json()["id"]

    # Register tenant B
    await client.post("/auth/register", json={"email": "tenant_b@example.com", "password": "passw0rd"})
    login_b = await client.post("/auth/login", json={"email": "tenant_b@example.com", "password": "passw0rd"})
    headers_b = {"Authorization": f"Bearer {login_b.json()['access_token']}"}

    resp = await client.delete(f"/api-keys/{key_id}", headers=headers_b)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_max_keys_enforced(client, auth_headers):
    from app.routers.api_keys import MAX_KEYS_PER_TENANT
    # Create keys up to the limit
    for i in range(MAX_KEYS_PER_TENANT):
        r = await client.post("/api-keys", json={"name": f"Key {i}"}, headers=auth_headers)
        if r.status_code != 201:
            break

    # One more should fail
    resp = await client.post("/api-keys", json={"name": "One too many"}, headers=auth_headers)
    assert resp.status_code == 422


# ── Webhook Routing ────────────────────────────────────────────────────────────

def webhook_payload() -> dict:
    return {
        "secret": "ignored-now",
        "broker": "oanda",
        "account": "primary",
        "action": "buy",
        "symbol": "EUR_USD",
        "order_type": "market",
        "quantity": 1000,
    }


@pytest.mark.asyncio
async def test_webhook_routes_to_correct_tenant(client, registered_tenant, api_key, oanda_broker_account):
    raw_key, tenant_id = api_key
    mock_result = BrokerOrderResult(
        success=True, broker_order_id="oanda-123",
        filled_quantity=1000.0, avg_fill_price=1.085,
    )
    with patch("app.brokers.oanda.OandaBroker.submit_order", new_callable=AsyncMock) as mock_submit:
        mock_submit.return_value = mock_result

        resp = await client.post(
            f"/webhook/{tenant_id}",
            json=webhook_payload(),
            headers={"X-Webhook-Secret": raw_key},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "filled"


@pytest.mark.asyncio
async def test_webhook_missing_header_rejected(client, registered_tenant, api_key):
    _, tenant_id = api_key
    resp = await client.post(f"/webhook/{tenant_id}", json=webhook_payload())
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_webhook_wrong_key_rejected(client, registered_tenant, api_key):
    _, tenant_id = api_key
    resp = await client.post(
        f"/webhook/{tenant_id}",
        json=webhook_payload(),
        headers={"X-Webhook-Secret": "tvr_wrong_key_totally_fake"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_webhook_revoked_key_rejected(client, auth_headers, registered_tenant, api_key):
    raw_key, tenant_id = api_key

    # Find and revoke the key
    keys_resp = await client.get("/api-keys", headers=auth_headers)
    key_id = keys_resp.json()[0]["id"]
    await client.delete(f"/api-keys/{key_id}", headers=auth_headers)

    resp = await client.post(
        f"/webhook/{tenant_id}",
        json=webhook_payload(),
        headers={"X-Webhook-Secret": raw_key},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_webhook_wrong_tenant_id_rejected(client, registered_tenant, api_key):
    """A valid key cannot be used against a different tenant's URL."""
    raw_key, tenant_id = api_key
    wrong_tenant_id = tenant_id + 9999

    resp = await client.post(
        f"/webhook/{wrong_tenant_id}",
        json=webhook_payload(),
        headers={"X-Webhook-Secret": raw_key},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_webhook_inactive_tenant_rejected(client, registered_tenant, api_key, db_session):
    raw_key, tenant_id = api_key

    # Deactivate the tenant
    from app.services.auth import get_tenant_by_id
    tenant = await get_tenant_by_id(db_session, tenant_id)
    tenant.is_active = False
    await db_session.commit()

    resp = await client.post(
        f"/webhook/{tenant_id}",
        json=webhook_payload(),
        headers={"X-Webhook-Secret": raw_key},
    )
    assert resp.status_code == 403


# ── Tenant Isolation ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_orders_scoped_to_tenant(client):
    """Each tenant sees only their own orders."""
    # Tenant A
    await client.post("/auth/register", json={"email": "iso_a@example.com", "password": "passw0rd"})
    login_a = await client.post("/auth/login", json={"email": "iso_a@example.com", "password": "passw0rd"})
    headers_a = {"Authorization": f"Bearer {login_a.json()['access_token']}"}
    tenant_a_id = (await client.get("/auth/me", headers=headers_a)).json()["id"]
    key_a_resp = await client.post("/api-keys", json={"name": "A"}, headers=headers_a)
    key_a = key_a_resp.json()["raw_key"]
    await client.post("/broker-accounts", json={
        "broker": "oanda", "account_alias": "primary",
        "credentials": {"api_key": "k", "account_id": "test", "base_url": "https://api-fxpractice.oanda.com/v3"},
    }, headers=headers_a)

    # Tenant B
    await client.post("/auth/register", json={"email": "iso_b@example.com", "password": "passw0rd"})
    login_b = await client.post("/auth/login", json={"email": "iso_b@example.com", "password": "passw0rd"})
    headers_b = {"Authorization": f"Bearer {login_b.json()['access_token']}"}

    # Place an order as tenant A
    mock_result = BrokerOrderResult(success=True, broker_order_id="A-1", filled_quantity=1000.0)
    with patch("app.brokers.oanda.OandaBroker.submit_order", new_callable=AsyncMock) as mock_submit:
        mock_submit.return_value = mock_result
        await client.post(
            f"/webhook/{tenant_a_id}",
            json=webhook_payload(),
            headers={"X-Webhook-Secret": key_a},
        )

    # Tenant A sees their order
    orders_a = (await client.get("/api/orders", headers=headers_a)).json()
    assert len(orders_a) == 1

    # Tenant B sees nothing
    orders_b = (await client.get("/api/orders", headers=headers_b)).json()
    assert len(orders_b) == 0


@pytest.mark.asyncio
async def test_positions_scoped_to_tenant(client):
    """Each tenant sees only their own positions."""
    await client.post("/auth/register", json={"email": "pos_a@example.com", "password": "passw0rd"})
    login_a = await client.post("/auth/login", json={"email": "pos_a@example.com", "password": "passw0rd"})
    headers_a = {"Authorization": f"Bearer {login_a.json()['access_token']}"}
    tenant_a_id = (await client.get("/auth/me", headers=headers_a)).json()["id"]
    key_a = (await client.post("/api-keys", json={"name": "A"}, headers=headers_a)).json()["raw_key"]
    await client.post("/broker-accounts", json={
        "broker": "oanda", "account_alias": "primary",
        "credentials": {"api_key": "k", "account_id": "test", "base_url": "https://api-fxpractice.oanda.com/v3"},
    }, headers=headers_a)

    await client.post("/auth/register", json={"email": "pos_b@example.com", "password": "passw0rd"})
    login_b = await client.post("/auth/login", json={"email": "pos_b@example.com", "password": "passw0rd"})
    headers_b = {"Authorization": f"Bearer {login_b.json()['access_token']}"}

    mock_result = BrokerOrderResult(success=True, broker_order_id="A-pos-1",
                                    filled_quantity=1000.0, avg_fill_price=1.085)
    with patch("app.brokers.oanda.OandaBroker.submit_order", new_callable=AsyncMock) as mock_submit:
        mock_submit.return_value = mock_result
        await client.post(
            f"/webhook/{tenant_a_id}",
            json=webhook_payload(),
            headers={"X-Webhook-Secret": key_a},
        )

    positions_a = (await client.get("/api/positions", headers=headers_a)).json()
    assert len(positions_a) == 1
    assert positions_a[0]["symbol"] == "EUR_USD"

    positions_b = (await client.get("/api/positions", headers=headers_b)).json()
    assert len(positions_b) == 0


@pytest.mark.asyncio
async def test_status_endpoints_require_auth(client):
    resp = await client.get("/api/orders")
    assert resp.status_code == 401

    resp = await client.get("/api/positions")
    assert resp.status_code == 401

    resp = await client.get("/api/orders/open")
    assert resp.status_code == 401
