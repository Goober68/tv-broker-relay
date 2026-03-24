"""
Tests for Step 3: Broker accounts + credential encryption.

Covers:
  - Create/list/get/update/delete broker accounts
  - Required field validation per broker
  - Credentials never returned in plain text in responses
  - Credential summary shows redacted values
  - Encryption round-trip
  - Cross-tenant isolation (can't read/modify another tenant's accounts)
  - Webhook uses DB credentials (not env vars)
  - Missing broker account returns a clear error
  - Duplicate alias rejected
"""
import pytest
from unittest.mock import patch, AsyncMock
from app.brokers.base import BrokerOrderResult
from app.services.credentials import encrypt_credentials, decrypt_credentials


# ── Encryption Unit Tests ──────────────────────────────────────────────────────

def test_encrypt_decrypt_roundtrip():
    creds = {"api_key": "secret123", "account_id": "ACC-001", "base_url": "https://example.com"}
    encrypted = encrypt_credentials(creds)
    assert encrypted != str(creds)
    decrypted = decrypt_credentials(encrypted)
    assert decrypted == creds


def test_each_encryption_is_unique():
    """Fernet uses a random IV — same input produces different ciphertext each time."""
    creds = {"api_key": "secret"}
    enc1 = encrypt_credentials(creds)
    enc2 = encrypt_credentials(creds)
    assert enc1 != enc2
    # Both decrypt to the same value
    assert decrypt_credentials(enc1) == decrypt_credentials(enc2)


def test_tampered_ciphertext_raises():
    from cryptography.fernet import InvalidToken
    encrypted = encrypt_credentials({"api_key": "x"})
    tampered = encrypted[:-4] + "XXXX"
    with pytest.raises(InvalidToken):
        decrypt_credentials(tampered)


# ── Broker Account CRUD ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_oanda_account(client, auth_headers):
    resp = await client.post("/broker-accounts", json={
        "broker": "oanda",
        "account_alias": "primary",
        "credentials": {
            "api_key": "my-oanda-key",
            "account_id": "101-001-123",
            "base_url": "https://api-fxpractice.oanda.com/v3",
        },
    }, headers=auth_headers)
    assert resp.status_code == 201
    data = resp.json()
    assert data["broker"] == "oanda"
    assert data["account_alias"] == "primary"
    assert data["is_active"] is True
    # Credentials must NOT appear in plain text
    assert "api_key" not in str(data)
    assert "my-oanda-key" not in str(data)


@pytest.mark.asyncio
async def test_credential_summary_is_redacted(client, auth_headers):
    resp = await client.post("/broker-accounts", json={
        "broker": "oanda",
        "account_alias": "live",
        "credentials": {
            "api_key": "supersecretkey12345",
            "account_id": "101-001-999",
            "base_url": "https://api-fxtrade.oanda.com/v3",
        },
    }, headers=auth_headers)
    summary = resp.json()["credential_summary"]
    # api_key should be masked
    assert summary["api_key"].startswith("****")
    assert "supersecretkey12345" not in str(summary)
    # base_url and account_id are visible
    assert summary["base_url"] == "https://api-fxtrade.oanda.com/v3"
    assert summary["account_id"] == "101-001-999"


@pytest.mark.asyncio
async def test_list_broker_accounts(client, auth_headers, oanda_broker_account):
    resp = await client.get("/broker-accounts", headers=auth_headers)
    assert resp.status_code == 200
    accounts = resp.json()
    assert len(accounts) >= 1
    assert any(a["broker"] == "oanda" for a in accounts)


@pytest.mark.asyncio
async def test_get_broker_account(client, auth_headers, oanda_broker_account):
    account_id = oanda_broker_account["id"]
    resp = await client.get(f"/broker-accounts/{account_id}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["id"] == account_id


@pytest.mark.asyncio
async def test_update_credentials(client, auth_headers, oanda_broker_account):
    account_id = oanda_broker_account["id"]
    resp = await client.patch(f"/broker-accounts/{account_id}", json={
        "credentials": {
            "api_key": "new-oanda-key-xyz",
            "account_id": "101-001-updated",
            "base_url": "https://api-fxtrade.oanda.com/v3",
        },
        "display_name": "Updated Oanda",
    }, headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["display_name"] == "Updated Oanda"
    assert "new-oanda-key-xyz" not in str(data)
    assert data["credential_summary"]["account_id"] == "101-001-updated"


@pytest.mark.asyncio
async def test_delete_broker_account(client, auth_headers, oanda_broker_account):
    account_id = oanda_broker_account["id"]
    resp = await client.delete(f"/broker-accounts/{account_id}", headers=auth_headers)
    assert resp.status_code == 204

    resp = await client.get(f"/broker-accounts/{account_id}", headers=auth_headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_duplicate_alias_rejected(client, auth_headers, oanda_broker_account):
    resp = await client.post("/broker-accounts", json={
        "broker": "oanda",
        "account_alias": "primary",  # same alias as oanda_broker_account fixture
        "credentials": {
            "api_key": "different-key",
            "account_id": "101-001-different",
            "base_url": "https://api-fxtrade.oanda.com/v3",
        },
    }, headers=auth_headers)
    assert resp.status_code == 422
    assert "already exists" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_missing_required_field_rejected(client, auth_headers):
    resp = await client.post("/broker-accounts", json={
        "broker": "oanda",
        "account_alias": "incomplete",
        "credentials": {
            "account_id": "101-001-123",
            # Missing: api_key and base_url
        },
    }, headers=auth_headers)
    assert resp.status_code == 422
    assert "api_key" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_get_required_fields_endpoint(client, auth_headers):
    for broker in ("oanda", "ibkr", "tradovate", "etrade"):
        resp = await client.get(f"/broker-accounts/fields/{broker}", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["broker"] == broker
        assert isinstance(data["required_fields"], list)
        assert len(data["required_fields"]) > 0


# ── Broker isolation ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cannot_access_another_tenants_broker_account(client):
    # Tenant A creates a broker account
    await client.post("/auth/register", json={"email": "ba_a@example.com", "password": "passw0rd"})
    login_a = await client.post("/auth/login", json={"email": "ba_a@example.com", "password": "passw0rd"})
    headers_a = {"Authorization": f"Bearer {login_a.json()['access_token']}"}
    create_resp = await client.post("/broker-accounts", json={
        "broker": "oanda", "account_alias": "primary",
        "credentials": {"api_key": "a-key", "account_id": "a-acct", "base_url": "https://api-fxtrade.oanda.com/v3"},
    }, headers=headers_a)
    account_id = create_resp.json()["id"]

    # Tenant B tries to access it
    await client.post("/auth/register", json={"email": "ba_b@example.com", "password": "passw0rd"})
    login_b = await client.post("/auth/login", json={"email": "ba_b@example.com", "password": "passw0rd"})
    headers_b = {"Authorization": f"Bearer {login_b.json()['access_token']}"}

    resp = await client.get(f"/broker-accounts/{account_id}", headers=headers_b)
    assert resp.status_code == 404

    resp = await client.delete(f"/broker-accounts/{account_id}", headers=headers_b)
    assert resp.status_code == 404


# ── Webhook uses DB credentials ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_webhook_uses_db_credentials(client, registered_tenant, api_key, oanda_broker_account):
    """
    The webhook should succeed using broker credentials stored in DB,
    not from environment variables.
    """
    raw_key, tenant_id = api_key
    mock_result = BrokerOrderResult(
        success=True, broker_order_id="db-creds-test",
        filled_quantity=1000.0, avg_fill_price=1.085,
    )
    with patch("app.brokers.oanda.OandaBroker.submit_order", new_callable=AsyncMock) as mock_submit:
        mock_submit.return_value = mock_result
        resp = await client.post(
            f"/webhook/{tenant_id}",
            json={
                "secret": "ignored",
                "broker": "oanda",
                "account": "primary",
                "action": "buy",
                "symbol": "EUR_USD",
                "order_type": "market",
                "quantity": 1000,
            },
            headers={"X-Webhook-Secret": raw_key},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "filled"


@pytest.mark.asyncio
async def test_webhook_missing_broker_account_returns_422(client, registered_tenant, api_key):
    """
    If no broker account is configured for the broker/alias combo,
    the webhook should return 422 with a clear message.
    """
    raw_key, tenant_id = api_key
    # No broker account created — should fail clearly
    resp = await client.post(
        f"/webhook/{tenant_id}",
        json={
            "secret": "ignored",
            "broker": "oanda",
            "account": "primary",
            "action": "buy",
            "symbol": "EUR_USD",
            "order_type": "market",
            "quantity": 1000,
        },
        headers={"X-Webhook-Secret": raw_key},
    )
    assert resp.status_code == 422
    assert "broker account" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_accounts_require_auth(client):
    resp = await client.get("/broker-accounts")
    assert resp.status_code == 401
    resp = await client.post("/broker-accounts", json={})
    assert resp.status_code == 401
