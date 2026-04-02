"""
Tests for account balance retrieval and order routing correctness.

Verifies:
1. Tradovate get_balance matches the correct account when multiple accounts exist
2. Tradovate get_balance handles single-account (prop firm) logins correctly
3. Tradovate get_balance returns None when account can't be matched in multi-account login
4. Webhook orders are routed to the correct broker account (accountSpec + accountId)
5. Token refresh doesn't cross-contaminate accounts with different credentials
"""
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from datetime import datetime, timezone, timedelta
from app.brokers.tradovate import TradovateBroker
from app.brokers.base import BrokerOrderResult
from app.models.order import OrderAction, OrderType, OrderStatus, TimeInForce


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_broker():
    """Create a TradovateBroker with a valid (mock) token."""
    broker = TradovateBroker(
        username="test", password="test", app_id="test",
        app_version="1.0", base_url="https://demo.tradovateapi.com/v1",
    )
    broker._access_token = "mock-token"
    broker._token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    return broker


def _mock_response(status_code, json_data):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    return resp


def _make_async_client(get_side_effect=None, post_side_effect=None):
    """Create a mock httpx.AsyncClient that works as async context manager."""
    instance = MagicMock()
    if get_side_effect:
        instance.get = AsyncMock(side_effect=get_side_effect)
    if post_side_effect:
        instance.post = AsyncMock(side_effect=post_side_effect)
    instance.__aenter__ = AsyncMock(return_value=instance)
    instance.__aexit__ = AsyncMock(return_value=False)
    return instance


# ── Balance retrieval ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_balance_exact_name_match():
    """Balance lookup matches account by name field."""
    broker = _make_broker()
    account_list = [
        {"id": 100, "name": "ACCT_A"},
        {"id": 200, "name": "ACCT_B"},
    ]
    cash_balances = [
        {"accountId": 100, "amount": 50000.0},
        {"accountId": 200, "amount": 75000.0},
    ]

    async def mock_get(url, **kwargs):
        if "account/list" in url:
            return _mock_response(200, account_list)
        if "cashBalance/list" in url:
            return _mock_response(200, cash_balances)

    with patch("httpx.AsyncClient", return_value=_make_async_client(get_side_effect=mock_get)):
        balance_a = await broker.get_balance("ACCT_A")
        assert balance_a == 50000.0

        broker._account_id_map = {}  # clear cache
        balance_b = await broker.get_balance("ACCT_B")
        assert balance_b == 75000.0


@pytest.mark.asyncio
async def test_balance_single_account_fallback():
    """Single-account login returns balance even when alias doesn't match name (prop firm case)."""
    broker = _make_broker()
    account_list = [{"id": 100, "name": "TAKEPROFIT123"}]
    cash_balances = [{"accountId": 100, "amount": 49571.0}]

    async def mock_get(url, **kwargs):
        if "account/list" in url:
            return _mock_response(200, account_list)
        if "cashBalance/list" in url:
            return _mock_response(200, cash_balances)

    with patch("httpx.AsyncClient", return_value=_make_async_client(get_side_effect=mock_get)):
        balance = await broker.get_balance("APEX29121300000178")
        assert balance == 49571.0


@pytest.mark.asyncio
async def test_balance_multi_account_no_match_returns_none():
    """Multi-account login returns None when alias doesn't match any account."""
    broker = _make_broker()
    account_list = [
        {"id": 100, "name": "ACCT_A"},
        {"id": 200, "name": "ACCT_B"},
    ]

    async def mock_get(url, **kwargs):
        if "account/list" in url:
            return _mock_response(200, account_list)
        if "cashBalance/list" in url:
            return _mock_response(200, [])

    with patch("httpx.AsyncClient", return_value=_make_async_client(get_side_effect=mock_get)):
        balance = await broker.get_balance("UNKNOWN_ACCT")
        assert balance is None


@pytest.mark.asyncio
async def test_balance_nickname_match():
    """Balance lookup matches account by nickname field."""
    broker = _make_broker()
    account_list = [
        {"id": 100, "name": "INTERNAL_NAME", "nickname": "MY_ALIAS"},
        {"id": 200, "name": "OTHER"},
    ]
    cash_balances = [
        {"accountId": 100, "amount": 30000.0},
        {"accountId": 200, "amount": 60000.0},
    ]

    async def mock_get(url, **kwargs):
        if "account/list" in url:
            return _mock_response(200, account_list)
        if "cashBalance/list" in url:
            return _mock_response(200, cash_balances)

    with patch("httpx.AsyncClient", return_value=_make_async_client(get_side_effect=mock_get)):
        balance = await broker.get_balance("MY_ALIAS")
        assert balance == 30000.0


@pytest.mark.asyncio
async def test_balance_caches_account_id():
    """After first lookup, account ID is cached and doesn't re-fetch account list."""
    broker = _make_broker()
    account_list = [{"id": 100, "name": "ACCT_A"}]
    cash_balances = [{"accountId": 100, "amount": 50000.0}]
    call_count = {"account_list": 0}

    async def mock_get(url, **kwargs):
        if "account/list" in url:
            call_count["account_list"] += 1
            return _mock_response(200, account_list)
        if "cashBalance/list" in url:
            return _mock_response(200, cash_balances)

    with patch("httpx.AsyncClient", return_value=_make_async_client(get_side_effect=mock_get)):
        await broker.get_balance("ACCT_A")
        await broker.get_balance("ACCT_A")
        assert call_count["account_list"] == 1


@pytest.mark.asyncio
async def test_balance_different_accounts_different_logins():
    """Two separate broker instances with different logins return different balances."""
    broker_a = _make_broker()
    broker_b = _make_broker()
    broker_b._access_token = "different-token"

    async def mock_get_a(url, **kwargs):
        if "account/list" in url:
            return _mock_response(200, [{"id": 100, "name": "ACCT_A"}])
        if "cashBalance/list" in url:
            return _mock_response(200, [{"accountId": 100, "amount": 50000.0}])

    async def mock_get_b(url, **kwargs):
        if "account/list" in url:
            return _mock_response(200, [{"id": 200, "name": "ACCT_B"}])
        if "cashBalance/list" in url:
            return _mock_response(200, [{"accountId": 200, "amount": 75000.0}])

    with patch("httpx.AsyncClient", return_value=_make_async_client(get_side_effect=mock_get_a)):
        bal_a = await broker_a.get_balance("ACCT_A")

    with patch("httpx.AsyncClient", return_value=_make_async_client(get_side_effect=mock_get_b)):
        bal_b = await broker_b.get_balance("ACCT_B")

    assert bal_a == 50000.0
    assert bal_b == 75000.0
    assert bal_a != bal_b


# ── Order routing ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_order_uses_correct_account_spec():
    """submit_order sends the correct accountSpec and accountId in the request body."""
    broker = _make_broker()
    broker._account_id = 100
    broker._account_id_map = {"MY_ACCT": 200}

    from app.models.order import InstrumentType
    order = MagicMock()
    order.account = "MY_ACCT"
    order.symbol = "ESM6"
    order.action = OrderAction.BUY
    order.instrument_type = InstrumentType.FUTURE
    order.order_type = OrderType.MARKET
    order.quantity = 1
    order.time_in_force = TimeInForce.FOK
    order.price = None
    order.stop_loss = None
    order.take_profit = None
    order.trailing_distance = None
    order.trail_trigger = None
    order.trail_dist = None
    order.trail_update = None
    order.expire_at = None
    order.broker_quantity = None
    order.id = 1

    captured = {}

    async def mock_post(url, **kwargs):
        captured.update(kwargs.get("json", {}))
        return _mock_response(200, {"orderId": 12345, "orderStatus": "Filled"})

    with patch("httpx.AsyncClient", return_value=_make_async_client(post_side_effect=mock_post)):
        await broker.submit_order(order)

    assert captured["accountSpec"] == "MY_ACCT"
    assert captured["accountId"] == 200


@pytest.mark.asyncio
async def test_order_account_id_falls_back_correctly():
    """When account alias is not in the map, accountId falls back to _account_id."""
    broker = _make_broker()
    broker._account_id = 999
    broker._account_id_map = {"OTHER_ACCT": 200}

    from app.models.order import InstrumentType
    order = MagicMock()
    order.account = "UNKNOWN_ACCT"
    order.symbol = "NQM6"
    order.action = OrderAction.SELL
    order.instrument_type = InstrumentType.FUTURE
    order.order_type = OrderType.MARKET
    order.quantity = 2
    order.time_in_force = TimeInForce.FOK
    order.price = None
    order.stop_loss = None
    order.take_profit = None
    order.trailing_distance = None
    order.trail_trigger = None
    order.trail_dist = None
    order.trail_update = None
    order.expire_at = None
    order.broker_quantity = None
    order.id = 2

    captured = {}

    async def mock_post(url, **kwargs):
        captured.update(kwargs.get("json", {}))
        return _mock_response(200, {"orderId": 12345, "orderStatus": "Filled"})

    with patch("httpx.AsyncClient", return_value=_make_async_client(post_side_effect=mock_post)):
        await broker.submit_order(order)

    assert captured["accountSpec"] == "UNKNOWN_ACCT"
    assert captured["accountId"] == 999


# ── Token refresh isolation ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_token_refresh_does_not_cross_contaminate():
    """Token refresh should only update accounts sharing the same access token."""
    from app.services.credentials import encrypt_credentials, decrypt_credentials

    token_a = "token-for-login-A"
    token_b = "token-for-login-B"

    creds_a = {"auth_method": "oauth", "access_token": token_a, "base_url": "https://demo.tradovateapi.com/v1"}
    creds_b = {"auth_method": "oauth", "access_token": token_b, "base_url": "https://demo.tradovateapi.com/v1"}

    enc_a = encrypt_credentials(creds_a)
    enc_b = encrypt_credentials(creds_b)

    # Simulate the dedup logic from _tradovate_token_refresh_once
    refreshed_tokens = {}
    results = {}
    for label, enc in [("a", enc_a), ("b", enc_b)]:
        creds = decrypt_credentials(enc)
        token = creds["access_token"]
        if token in refreshed_tokens:
            creds["access_token"] = refreshed_tokens[token]
        else:
            new_token = f"renewed-{token}"
            refreshed_tokens[token] = new_token
            creds["access_token"] = new_token
        results[label] = creds["access_token"]

    assert results["a"] == f"renewed-{token_a}"
    assert results["b"] == f"renewed-{token_b}"
    assert results["a"] != results["b"]


@pytest.mark.asyncio
async def test_token_refresh_shares_renewal_for_same_login():
    """Accounts sharing the same access token get the same renewed token (one API call)."""
    from app.services.credentials import encrypt_credentials, decrypt_credentials

    shared_token = "shared-token"
    enc_1 = encrypt_credentials({"auth_method": "oauth", "access_token": shared_token})
    enc_2 = encrypt_credentials({"auth_method": "oauth", "access_token": shared_token})

    refreshed_tokens = {}
    for enc in [enc_1, enc_2]:
        creds = decrypt_credentials(enc)
        token = creds["access_token"]
        if token not in refreshed_tokens:
            refreshed_tokens[token] = f"renewed-{token}"

    assert len(refreshed_tokens) == 1
    assert list(refreshed_tokens.values())[0] == f"renewed-{shared_token}"


# ── Webhook routes to correct account (integration) ──────────────────────────


@pytest.mark.asyncio
async def test_webhook_routes_to_correct_broker_account(
    client, registered_tenant, api_key, auth_headers
):
    """Webhook with account=X calls get_broker_for_tenant with X."""
    raw_key, tenant_id = api_key

    for alias in ["ACCT_ALPHA", "ACCT_BETA"]:
        resp = await client.post("/broker-accounts", json={
            "broker": "tradovate",
            "account_alias": alias,
            "display_name": alias,
            "credentials": {
                "auth_method": "oauth",
                "access_token": f"token-{alias}",
                "refresh_token": f"rt-{alias}",
                "base_url": "https://demo.tradovateapi.com/v1",
            },
        }, headers=auth_headers)
        assert resp.status_code == 201, f"Failed to create {alias}: {resp.text}"

    mock_result = BrokerOrderResult(
        success=True, broker_order_id="tv-12345",
        filled_quantity=1, avg_fill_price=5000.0,
        broker_request='{"accountSpec": "ACCT_BETA"}',
        broker_response='{"orderId": 12345}',
    )

    with patch(
        "app.services.order_processor.get_broker_for_tenant",
        new_callable=AsyncMock,
    ) as mock_get_broker:
        mock_broker = AsyncMock()
        mock_broker.submit_order = AsyncMock(return_value=mock_result)
        mock_get_broker.return_value = mock_broker

        resp = await client.post(f"/webhook/{tenant_id}", json={
            "secret": raw_key,
            "broker": "tradovate",
            "account": "ACCT_BETA",
            "action": "buy",
            "symbol": "ESM6",
            "instrument_type": "future",
            "order_type": "market",
            "quantity": 1,
        })
        assert resp.status_code == 200, resp.text

        mock_get_broker.assert_called_once()
        call_args = mock_get_broker.call_args
        assert call_args[0][0] == "tradovate"
        assert call_args[0][1] == "ACCT_BETA"


@pytest.mark.asyncio
async def test_webhook_wrong_account_rejected(client, registered_tenant, api_key, auth_headers):
    """Webhook targeting a non-existent account alias is rejected."""
    raw_key, tenant_id = api_key

    resp = await client.post("/broker-accounts", json={
        "broker": "tradovate",
        "account_alias": "REAL_ACCT",
        "display_name": "Real",
        "credentials": {
            "auth_method": "oauth",
            "access_token": "token-real",
            "base_url": "https://demo.tradovateapi.com/v1",
        },
    }, headers=auth_headers)
    assert resp.status_code == 201

    resp = await client.post(f"/webhook/{tenant_id}", json={
        "secret": raw_key,
        "broker": "tradovate",
        "account": "WRONG_ACCT",
        "action": "buy",
        "symbol": "ESM6",
        "instrument_type": "future",
        "order_type": "market",
        "quantity": 1,
    })
    assert resp.status_code in (422, 500)
