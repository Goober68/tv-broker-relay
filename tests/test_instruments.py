"""
Tests for equities and futures instrument support.

Covers:
  - Schema: instrument_type field, broker-instrument validation
  - Schema: futures quantity must be integer
  - Schema: extended_hours equity-only
  - Oanda: rejects non-forex instrument types
  - Tradovate: rejects non-future instrument types, accepts futures
  - IBKR: routes correctly by instrument type, handles conid resolution
  - E*Trade: rejects futures, accepts equities with extended_hours
  - State: multiplier-aware P&L for futures
  - State: equity P&L (multiplier=1)
  - Instrument map: CRUD endpoints on broker accounts
  - Webhook: full pipeline with equity and future payloads
"""
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from pydantic import ValidationError

from app.schemas.webhook import WebhookPayload
from app.models.order import (
    Order, OrderAction, OrderType, TimeInForce,
    InstrumentType, DEFAULT_FUTURES_MULTIPLIERS,
)
from app.brokers.base import BrokerOrderResult
from app.brokers.oanda import OandaBroker
from app.brokers.tradovate import TradovateBroker
from app.brokers.ibkr import IBKRBroker
from app.brokers.etrade import EtradeBroker


# ── Schema Validation ──────────────────────────────────────────────────────────

def base_payload(**overrides) -> dict:
    return {
        "secret": "x", "broker": "oanda", "account": "primary",
        "action": "buy", "symbol": "EUR_USD",
        "instrument_type": "forex", "order_type": "market", "quantity": 1000,
        **overrides,
    }


def test_default_instrument_type_is_forex():
    p = WebhookPayload(**base_payload())
    assert p.instrument_type == InstrumentType.FOREX


def test_ibkr_accepts_equity():
    p = WebhookPayload(**base_payload(broker="ibkr", instrument_type="equity", symbol="AAPL", quantity=10))
    assert p.instrument_type == InstrumentType.EQUITY


def test_ibkr_accepts_future():
    p = WebhookPayload(**base_payload(broker="ibkr", instrument_type="future", symbol="ES", quantity=2))
    assert p.instrument_type == InstrumentType.FUTURE


def test_tradovate_only_accepts_future():
    with pytest.raises(ValidationError) as exc:
        WebhookPayload(**base_payload(broker="tradovate", instrument_type="equity",
                                      symbol="AAPL", quantity=10))
    assert "does not support" in str(exc.value).lower()


def test_tradovate_accepts_future():
    p = WebhookPayload(**base_payload(broker="tradovate", instrument_type="future",
                                       symbol="ES", quantity=2))
    assert p.instrument_type == InstrumentType.FUTURE


def test_etrade_only_accepts_equity():
    with pytest.raises(ValidationError) as exc:
        WebhookPayload(**base_payload(broker="etrade", instrument_type="future",
                                      symbol="ES", quantity=2))
    assert "does not support" in str(exc.value).lower()


def test_etrade_accepts_equity():
    p = WebhookPayload(**base_payload(broker="etrade", instrument_type="equity",
                                       symbol="AAPL", quantity=10))
    assert p.broker == "etrade"


def test_oanda_only_accepts_forex_and_cfd():
    with pytest.raises(ValidationError):
        WebhookPayload(**base_payload(broker="oanda", instrument_type="equity",
                                      symbol="AAPL", quantity=10))
    with pytest.raises(ValidationError):
        WebhookPayload(**base_payload(broker="oanda", instrument_type="future",
                                      symbol="ES", quantity=2))


def test_oanda_accepts_cfd():
    p = WebhookPayload(**base_payload(instrument_type="cfd", symbol="OIL_USD"))
    assert p.instrument_type == InstrumentType.CFD


def test_futures_quantity_must_be_integer():
    with pytest.raises(ValidationError) as exc:
        WebhookPayload(**base_payload(broker="tradovate", instrument_type="future",
                                      symbol="ES", quantity=1.5))
    assert "whole number" in str(exc.value).lower()


def test_futures_integer_quantity_accepted():
    p = WebhookPayload(**base_payload(broker="tradovate", instrument_type="future",
                                       symbol="ES", quantity=3))
    assert p.quantity == 3.0


def test_extended_hours_equity_only():
    with pytest.raises(ValidationError) as exc:
        WebhookPayload(**base_payload(extended_hours=True))  # forex order
    assert "extended_hours" in str(exc.value).lower()


def test_extended_hours_accepted_for_equity():
    p = WebhookPayload(**base_payload(broker="etrade", instrument_type="equity",
                                       symbol="AAPL", quantity=10, extended_hours=True))
    assert p.extended_hours is True


def test_exchange_and_currency_normalized():
    p = WebhookPayload(**base_payload(broker="ibkr", instrument_type="equity",
                                       symbol="AAPL", quantity=10,
                                       exchange="nasdaq", currency="usd"))
    assert p.exchange == "NASDAQ"
    assert p.currency == "USD"


# ── Default Futures Multipliers ────────────────────────────────────────────────

def test_es_multiplier():
    assert DEFAULT_FUTURES_MULTIPLIERS["ES"] == 50.0


def test_nq_multiplier():
    assert DEFAULT_FUTURES_MULTIPLIERS["NQ"] == 20.0


def test_cl_multiplier():
    assert DEFAULT_FUTURES_MULTIPLIERS["CL"] == 1000.0


# ── Oanda Adapter Guard ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_oanda_rejects_equity_order():
    broker = OandaBroker(api_key="k", account_id="a", base_url="https://x.com")
    order = Order(
        tenant_id=1, broker="oanda", account="primary", symbol="AAPL",
        instrument_type=InstrumentType.EQUITY,
        action=OrderAction.BUY, order_type=OrderType.MARKET,
        quantity=10, time_in_force=TimeInForce.FOK,
    )
    result = await broker.submit_order(order)
    assert result.success is False
    assert "does not support" in result.error_message.lower()


@pytest.mark.asyncio
async def test_oanda_rejects_future_order():
    broker = OandaBroker(api_key="k", account_id="a", base_url="https://x.com")
    order = Order(
        tenant_id=1, broker="oanda", account="primary", symbol="ES",
        instrument_type=InstrumentType.FUTURE,
        action=OrderAction.BUY, order_type=OrderType.MARKET,
        quantity=1, time_in_force=TimeInForce.FOK,
    )
    result = await broker.submit_order(order)
    assert result.success is False
    assert "futures" in result.error_message.lower()


# ── Tradovate Adapter Guard ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tradovate_rejects_equity():
    broker = TradovateBroker(
        username="u", password="p", app_id="a", app_version="1.0",
        base_url="https://x.com"
    )
    order = Order(
        tenant_id=1, broker="tradovate", account="primary", symbol="AAPL",
        instrument_type=InstrumentType.EQUITY,
        action=OrderAction.BUY, order_type=OrderType.MARKET,
        quantity=10, time_in_force=TimeInForce.FOK,
    )
    result = await broker.submit_order(order)
    assert result.success is False
    assert "futures" in result.error_message.lower()


# ── E*Trade Adapter Guard ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_etrade_rejects_future():
    broker = EtradeBroker(
        consumer_key="k", consumer_secret="s",
        oauth_token="t", oauth_token_secret="ts",
        account_id="a", base_url="https://x.com"
    )
    order = Order(
        tenant_id=1, broker="etrade", account="primary", symbol="ES",
        instrument_type=InstrumentType.FUTURE,
        action=OrderAction.BUY, order_type=OrderType.MARKET,
        quantity=1, time_in_force=TimeInForce.FOK,
    )
    result = await broker.submit_order(order)
    assert result.success is False
    assert "futures" in result.error_message.lower()


# ── IBKR Adapter ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ibkr_uses_conid_from_instrument_map():
    instrument_map = {
        "AAPL": {"conid": 265598, "sec_type": "STK", "exchange": "NASDAQ"},
    }
    broker = IBKRBroker(
        gateway_url="https://localhost:5000/v1/api",
        account_id="DU123",
        instrument_map=instrument_map,
    )
    order = Order(
        tenant_id=1, broker="ibkr", account="primary", symbol="AAPL",
        instrument_type=InstrumentType.EQUITY,
        action=OrderAction.BUY, order_type=OrderType.MARKET,
        quantity=10, time_in_force=TimeInForce.DAY,
    )

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = [{"order_id": "ibkr-equity-1"}]

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client

        result = await broker.submit_order(order)

    assert result.success is True
    assert result.broker_order_id == "ibkr-equity-1"

    # Verify conid was sent in the request body
    call_body = mock_client.post.call_args.kwargs.get("json", {})
    assert call_body["orders"][0]["conid"] == 265598


@pytest.mark.asyncio
async def test_ibkr_futures_uses_int_quantity():
    instrument_map = {
        "ES": {"conid": 495512551, "sec_type": "FUT", "exchange": "CME", "multiplier": 50.0},
    }
    broker = IBKRBroker(
        gateway_url="https://localhost:5000/v1/api",
        account_id="DU123",
        instrument_map=instrument_map,
    )
    order = Order(
        tenant_id=1, broker="ibkr", account="primary", symbol="ES",
        instrument_type=InstrumentType.FUTURE,
        action=OrderAction.BUY, order_type=OrderType.MARKET,
        quantity=2, time_in_force=TimeInForce.DAY,
    )

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = [{"order_id": "ibkr-fut-1"}]

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client

        result = await broker.submit_order(order)

    assert result.success is True
    call_body = mock_client.post.call_args.kwargs.get("json", {})
    # Futures quantity must be an integer
    assert call_body["orders"][0]["quantity"] == 2
    assert isinstance(call_body["orders"][0]["quantity"], int)


@pytest.mark.asyncio
async def test_ibkr_equity_extended_hours_sets_outside_rth():
    instrument_map = {"AAPL": {"conid": 265598, "sec_type": "STK", "exchange": "NASDAQ"}}
    broker = IBKRBroker(gateway_url="https://localhost:5000", account_id="DU123",
                         instrument_map=instrument_map)
    order = Order(
        tenant_id=1, broker="ibkr", account="primary", symbol="AAPL",
        instrument_type=InstrumentType.EQUITY,
        action=OrderAction.BUY, order_type=OrderType.MARKET,
        quantity=5, time_in_force=TimeInForce.DAY,
        extended_hours=True,
    )
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = [{"order_id": "ext-1"}]

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client
        await broker.submit_order(order)

    call_body = mock_client.post.call_args.kwargs.get("json", {})
    assert call_body["orders"][0]["outsideRth"] is True


@pytest.mark.asyncio
async def test_ibkr_missing_conid_returns_error():
    """Without conid in map and no gateway search, should fail gracefully."""
    broker = IBKRBroker(gateway_url="https://localhost:5000", account_id="DU123",
                         instrument_map={})
    order = Order(
        tenant_id=1, broker="ibkr", account="primary", symbol="UNKNOWN",
        instrument_type=InstrumentType.EQUITY,
        action=OrderAction.BUY, order_type=OrderType.MARKET,
        quantity=10, time_in_force=TimeInForce.DAY,
    )

    # Mock gateway search returning nothing
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = []  # empty search result

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client

        result = await broker.submit_order(order)

    assert result.success is False
    assert "conid" in result.error_message.lower()


# ── Multiplier-Aware P&L ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_futures_pnl_uses_multiplier(db_session):
    """ES: 1 contract, buy at 5000, sell at 5010 = $500 P&L (10pts × $50/pt)."""
    from app.services.state import apply_fill_to_position

    buy = Order(
        tenant_id=1, broker="ibkr", account="primary", symbol="ES",
        instrument_type=InstrumentType.FUTURE,
        action=OrderAction.BUY, order_type=OrderType.MARKET,
        quantity=1, time_in_force=TimeInForce.DAY, multiplier=50.0,
    )
    await apply_fill_to_position(db_session, buy, 1.0, 5000.0)

    sell = Order(
        tenant_id=1, broker="ibkr", account="primary", symbol="ES",
        instrument_type=InstrumentType.FUTURE,
        action=OrderAction.SELL, order_type=OrderType.MARKET,
        quantity=1, time_in_force=TimeInForce.DAY, multiplier=50.0,
    )
    pos = await apply_fill_to_position(db_session, sell, 1.0, 5010.0)

    assert pos.quantity == 0.0
    assert abs(pos.realized_pnl - 500.0) < 0.01  # 10pts × $50


@pytest.mark.asyncio
async def test_equity_pnl_no_multiplier(db_session):
    """AAPL: 10 shares, buy at 180, sell at 185 = $50 P&L."""
    from app.services.state import apply_fill_to_position

    buy = Order(
        tenant_id=1, broker="etrade", account="primary", symbol="AAPL",
        instrument_type=InstrumentType.EQUITY,
        action=OrderAction.BUY, order_type=OrderType.MARKET,
        quantity=10, time_in_force=TimeInForce.DAY, multiplier=1.0,
    )
    await apply_fill_to_position(db_session, buy, 10.0, 180.0)

    sell = Order(
        tenant_id=1, broker="etrade", account="primary", symbol="AAPL",
        instrument_type=InstrumentType.EQUITY,
        action=OrderAction.SELL, order_type=OrderType.MARKET,
        quantity=10, time_in_force=TimeInForce.DAY, multiplier=1.0,
    )
    pos = await apply_fill_to_position(db_session, sell, 10.0, 185.0)

    assert pos.quantity == 0.0
    assert abs(pos.realized_pnl - 50.0) < 0.01  # 5pts × 10 shares × $1/pt


@pytest.mark.asyncio
async def test_nq_futures_pnl(db_session):
    """NQ: 2 contracts, buy at 18000, close at 18050 = $2000 P&L (50pts × $20 × 2)."""
    from app.services.state import apply_fill_to_position

    buy = Order(
        tenant_id=1, broker="ibkr", account="primary", symbol="NQ",
        instrument_type=InstrumentType.FUTURE,
        action=OrderAction.BUY, order_type=OrderType.MARKET,
        quantity=2, time_in_force=TimeInForce.DAY, multiplier=20.0,
    )
    await apply_fill_to_position(db_session, buy, 2.0, 18000.0)

    close = Order(
        tenant_id=1, broker="ibkr", account="primary", symbol="NQ",
        instrument_type=InstrumentType.FUTURE,
        action=OrderAction.CLOSE, order_type=OrderType.MARKET,
        quantity=2, time_in_force=TimeInForce.DAY, multiplier=20.0,
    )
    pos = await apply_fill_to_position(db_session, close, 2.0, 18050.0)

    assert pos.quantity == 0.0
    assert abs(pos.realized_pnl - 2000.0) < 0.01  # 50pts × $20 × 2 contracts


# ── Instrument Map CRUD ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_add_instrument_to_map(client, auth_headers, db_session, registered_tenant):
    """Create an IBKR account and add an instrument mapping."""
    # Create IBKR broker account
    create_resp = await client.post("/broker-accounts", json={
        "broker": "ibkr",
        "account_alias": "primary",
        "display_name": "IBKR Live",
        "credentials": {"gateway_url": "https://localhost:5000/v1/api", "account_id": "DU123"},
    }, headers=auth_headers)
    assert create_resp.status_code == 201
    account_id = create_resp.json()["id"]

    # Add AAPL mapping
    resp = await client.put(
        f"/broker-accounts/{account_id}/instruments/AAPL",
        json={"conid": 265598, "sec_type": "STK", "exchange": "NASDAQ"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["AAPL"]["conid"] == 265598

    # Add ES futures mapping
    resp = await client.put(
        f"/broker-accounts/{account_id}/instruments/ES",
        json={"conid": 495512551, "sec_type": "FUT", "exchange": "CME", "multiplier": 50.0},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["ES"]["multiplier"] == 50.0


@pytest.mark.asyncio
async def test_get_instrument_map(client, auth_headers):
    create_resp = await client.post("/broker-accounts", json={
        "broker": "ibkr", "account_alias": "primary",
        "credentials": {"gateway_url": "https://localhost:5000/v1/api", "account_id": "DU123"},
    }, headers=auth_headers)
    account_id = create_resp.json()["id"]

    await client.put(f"/broker-accounts/{account_id}/instruments/NQ",
                     json={"conid": 12345, "sec_type": "FUT", "multiplier": 20.0},
                     headers=auth_headers)

    resp = await client.get(f"/broker-accounts/{account_id}/instruments", headers=auth_headers)
    assert resp.status_code == 200
    assert "NQ" in resp.json()
    assert resp.json()["NQ"]["multiplier"] == 20.0


@pytest.mark.asyncio
async def test_delete_instrument_from_map(client, auth_headers):
    create_resp = await client.post("/broker-accounts", json={
        "broker": "ibkr", "account_alias": "primary",
        "credentials": {"gateway_url": "https://localhost:5000/v1/api", "account_id": "DU123"},
    }, headers=auth_headers)
    account_id = create_resp.json()["id"]

    await client.put(f"/broker-accounts/{account_id}/instruments/GC",
                     json={"conid": 99999, "sec_type": "FUT", "multiplier": 100.0},
                     headers=auth_headers)

    resp = await client.delete(f"/broker-accounts/{account_id}/instruments/GC",
                                headers=auth_headers)
    assert resp.status_code == 204

    map_resp = await client.get(f"/broker-accounts/{account_id}/instruments",
                                 headers=auth_headers)
    assert "GC" not in map_resp.json()


@pytest.mark.asyncio
async def test_delete_nonexistent_instrument_returns_404(client, auth_headers):
    create_resp = await client.post("/broker-accounts", json={
        "broker": "ibkr", "account_alias": "primary",
        "credentials": {"gateway_url": "https://localhost:5000/v1/api", "account_id": "DU123"},
    }, headers=auth_headers)
    account_id = create_resp.json()["id"]

    resp = await client.delete(f"/broker-accounts/{account_id}/instruments/MISSING",
                                headers=auth_headers)
    assert resp.status_code == 404


# ── Full Pipeline: Equity Webhook ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_equity_webhook_full_pipeline(client, registered_tenant, api_key, auth_headers, db_session):
    """End-to-end: IBKR equity order from webhook to filled state."""
    raw_key, tenant_id = api_key

    # Create IBKR broker account with AAPL in instrument map
    create_resp = await client.post("/broker-accounts", json={
        "broker": "ibkr", "account_alias": "primary",
        "credentials": {"gateway_url": "https://localhost:5000/v1/api", "account_id": "DU123"},
    }, headers=auth_headers)
    account_id = create_resp.json()["id"]

    await client.put(f"/broker-accounts/{account_id}/instruments/AAPL",
                     json={"conid": 265598, "sec_type": "STK", "exchange": "NASDAQ"},
                     headers=auth_headers)

    mock_result = BrokerOrderResult(
        success=True, broker_order_id="ibkr-eq-42",
        filled_quantity=10.0, avg_fill_price=185.0,
    )
    with patch("app.brokers.ibkr.IBKRBroker.submit_order", new_callable=AsyncMock) as m:
        m.return_value = mock_result
        resp = await client.post(
            f"/webhook/{tenant_id}",
            json={
                "secret": "x", "broker": "ibkr", "account": "primary",
                "action": "buy", "symbol": "AAPL",
                "instrument_type": "equity", "exchange": "NASDAQ",
                "order_type": "market", "quantity": 10,
            },
            headers={"X-Webhook-Secret": raw_key},
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "filled"

    # Check order stored correctly
    orders = (await client.get("/api/orders", headers=auth_headers)).json()
    order = orders[0]
    assert order["instrument_type"] == "equity"
    assert order["symbol"] == "AAPL"
    assert order["multiplier"] == 1.0

    # Check position created
    positions = (await client.get("/api/positions", headers=auth_headers)).json()
    assert any(p["symbol"] == "AAPL" for p in positions)


@pytest.mark.asyncio
async def test_futures_webhook_full_pipeline(client, registered_tenant, api_key, auth_headers, db_session):
    """End-to-end: Tradovate futures order from webhook to filled state."""
    raw_key, tenant_id = api_key

    # Assign pro plan so limit orders are available (not needed for market, but good practice)
    from app.services.plans import assign_plan
    await assign_plan(db_session, tenant_id, "pro")
    await db_session.commit()

    create_resp = await client.post("/broker-accounts", json={
        "broker": "tradovate", "account_alias": "primary",
        "credentials": {
            "username": "u", "password": "p",
            "app_id": "a", "app_version": "1.0",
            "base_url": "https://demo.tradovateapi.com/v1",
        },
    }, headers=auth_headers)
    assert create_resp.status_code == 201

    mock_result = BrokerOrderResult(
        success=True, broker_order_id="tv-fut-99",
        filled_quantity=2.0, avg_fill_price=5000.0,
    )
    with patch("app.brokers.tradovate.TradovateBroker.submit_order", new_callable=AsyncMock) as m:
        m.return_value = mock_result
        resp = await client.post(
            f"/webhook/{tenant_id}",
            json={
                "secret": "x", "broker": "tradovate", "account": "primary",
                "action": "buy", "symbol": "ES",
                "instrument_type": "future", "exchange": "CME",
                "order_type": "market", "quantity": 2,
            },
            headers={"X-Webhook-Secret": raw_key},
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "filled"

    orders = (await client.get("/api/orders", headers=auth_headers)).json()
    order = orders[0]
    assert order["instrument_type"] == "future"
    assert order["symbol"] == "ES"
    assert order["multiplier"] == 50.0  # resolved from DEFAULT_FUTURES_MULTIPLIERS


# ── Options Validation ─────────────────────────────────────────────────────────

def test_option_payload_requires_full_spec():
    """Options must have expiry, strike, and right."""
    with pytest.raises(ValidationError) as exc:
        WebhookPayload(**base_payload(
            broker="ibkr", instrument_type="option", symbol="AAPL", quantity=1,
            # Missing option_expiry, option_strike, option_right
        ))
    assert "option_expiry" in str(exc.value)


def test_option_payload_accepted_with_full_spec():
    p = WebhookPayload(**base_payload(
        broker="ibkr", instrument_type="option", symbol="AAPL", quantity=1,
        option_expiry="2025-06-20", option_strike=185.0, option_right="C",
    ))
    assert p.instrument_type == InstrumentType.OPTION
    assert p.option_right == "C"
    assert p.option_expiry == "2025-06-20"
    assert p.option_strike == 185.0
    assert p.option_multiplier == 100.0  # default


def test_option_right_normalized_to_uppercase():
    p = WebhookPayload(**base_payload(
        broker="ibkr", instrument_type="option", symbol="AAPL", quantity=2,
        option_expiry="2025-06-20", option_strike=185.0, option_right="p",
    ))
    assert p.option_right == "P"


def test_option_right_invalid_value_rejected():
    with pytest.raises(ValidationError) as exc:
        WebhookPayload(**base_payload(
            broker="ibkr", instrument_type="option", symbol="AAPL", quantity=1,
            option_expiry="2025-06-20", option_strike=185.0, option_right="X",
        ))
    assert "option_right" in str(exc.value).lower()


def test_option_tradovate_rejected():
    """Tradovate doesn't support options."""
    with pytest.raises(ValidationError) as exc:
        WebhookPayload(**base_payload(
            broker="tradovate", instrument_type="option", symbol="ES", quantity=1,
            option_expiry="2025-06-20", option_strike=5000.0, option_right="C",
        ))
    assert "does not support" in str(exc.value).lower()


def test_option_etrade_accepted():
    """E*Trade supports equity options."""
    p = WebhookPayload(**base_payload(
        broker="etrade", instrument_type="option", symbol="AAPL", quantity=1,
        option_expiry="2025-06-20", option_strike=185.0, option_right="C",
    ))
    assert p.instrument_type == InstrumentType.OPTION


def test_ibkr_option_order_body_uses_conidex():
    """IBKR options must use conidex format, not plain conid."""
    instrument_map = {
        "AAPL": {"conid": 265598, "sec_type": "STK", "exchange": "NASDAQ"},
    }
    broker = IBKRBroker(
        gateway_url="https://localhost:5000", account_id="DU123",
        instrument_map=instrument_map,
    )
    order = Order(
        tenant_id=1, broker="ibkr", account="primary", symbol="AAPL",
        instrument_type=InstrumentType.OPTION,
        action=OrderAction.BUY, order_type=OrderType.MARKET,
        quantity=2, time_in_force=TimeInForce.DAY,
        option_expiry="2025-06-20", option_strike=185.0, option_right="C",
        option_multiplier=100.0,
    )
    body = broker._build_order_body(order, "DU123", 265598)
    order_body = body["orders"][0]
    assert "conidex" in order_body
    assert "conid" not in order_body
    assert "265598" in order_body["conidex"]
    assert "OPT" in order_body["conidex"]
    assert "20250620" in order_body["conidex"]
    assert "185.0" in order_body["conidex"]
    assert "C" in order_body["conidex"]
