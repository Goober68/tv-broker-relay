"""
Unit tests for the Oanda broker adapter.
All HTTP calls are mocked — no real API credentials needed.
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import httpx

from app.brokers.oanda import OandaBroker
from app.models.order import Order, OrderAction, OrderType


def make_order(action=OrderAction.BUY, qty=1000.0, order_type=OrderType.MARKET, price=None):
    return Order(
        broker="oanda",
        account="primary",
        symbol="EUR_USD",
        action=action,
        order_type=order_type,
        quantity=qty,
        price=price,
    )


def mock_response(status_code: int, json_body: dict) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_body
    resp.text = str(json_body)
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=resp
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


# ── Market Orders ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_market_buy_fill():
    broker = OandaBroker(api_key="k", account_id="test-account", base_url="https://api-fxpractice.oanda.com/v3")
    fill_resp = mock_response(201, {
        "orderFillTransaction": {
            "id": "999",
            "orderID": "888",
            "units": "1000",
            "price": "1.08500",
            "type": "ORDER_FILL",
        }
    })
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=fill_resp)
        mock_client_cls.return_value = mock_client

        result = await broker.submit_order(make_order(OrderAction.BUY))

    assert result.success is True
    assert result.broker_order_id == "888"
    assert result.filled_quantity == 1000.0
    assert result.avg_fill_price == pytest.approx(1.085)


@pytest.mark.asyncio
async def test_market_sell_units_are_negative():
    """Oanda requires negative units for sell orders."""
    broker = OandaBroker(api_key="k", account_id="test-account", base_url="https://api-fxpractice.oanda.com/v3")
    order = make_order(OrderAction.SELL, 500)
    body = broker._build_order_body(order)
    assert body["order"]["units"] == "-500"


@pytest.mark.asyncio
async def test_market_order_fok_timeinforce():
    broker = OandaBroker(api_key="k", account_id="test-account", base_url="https://api-fxpractice.oanda.com/v3")
    order = make_order(OrderAction.BUY)
    body = broker._build_order_body(order)
    assert body["order"]["timeInForce"] == "FOK"


@pytest.mark.asyncio
async def test_fok_cancel_returns_failure():
    """If a FOK market order is cancelled (no liquidity), return failure."""
    broker = OandaBroker(api_key="k", account_id="test-account", base_url="https://api-fxpractice.oanda.com/v3")
    cancel_resp = mock_response(201, {
        "orderCancelTransaction": {
            "id": "100",
            "orderID": "99",
            "reason": "MARKET_HALTED",
        }
    })
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=cancel_resp)
        mock_client_cls.return_value = mock_client

        result = await broker.submit_order(make_order())

    assert result.success is False
    assert "MARKET_HALTED" in result.error_message


# ── Limit / Stop Orders ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_limit_order_gtc():
    broker = OandaBroker(api_key="k", account_id="test-account", base_url="https://api-fxpractice.oanda.com/v3")
    order = make_order(OrderAction.BUY, order_type=OrderType.LIMIT, price=1.0800)
    body = broker._build_order_body(order)
    assert body["order"]["type"] == "LIMIT"
    assert body["order"]["timeInForce"] == "GTC"
    assert body["order"]["price"] == "1.08"


@pytest.mark.asyncio
async def test_limit_order_accepted_not_filled():
    broker = OandaBroker(api_key="k", account_id="test-account", base_url="https://api-fxpractice.oanda.com/v3")
    create_resp = mock_response(201, {
        "orderCreateTransaction": {
            "id": "200",
            "orderID": "199",
            "type": "LIMIT_ORDER",
        }
    })
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=create_resp)
        mock_client_cls.return_value = mock_client

        order = make_order(OrderAction.BUY, order_type=OrderType.LIMIT, price=1.08)
        result = await broker.submit_order(order)

    assert result.success is True
    assert result.filled_quantity == 0.0
    assert result.broker_order_id == "199"


# ── Close Position ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_close_long_position():
    broker = OandaBroker(api_key="k", account_id="test-account", base_url="https://api-fxpractice.oanda.com/v3")

    pos_resp = mock_response(200, {
        "position": {
            "long": {"units": "2000"},
            "short": {"units": "0"},
        }
    })
    close_resp = mock_response(200, {
        "longOrderFillTransaction": {
            "id": "300",
            "orderID": "299",
            "units": "-2000",
            "price": "1.09000",
        }
    })

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=pos_resp)
        mock_client.put = AsyncMock(return_value=close_resp)
        mock_client_cls.return_value = mock_client

        result = await broker.submit_order(make_order(OrderAction.CLOSE))

    assert result.success is True
    assert result.filled_quantity == 2000.0
    assert result.avg_fill_price == pytest.approx(1.09)

    # Verify close body only sent longUnits=ALL, shortUnits=NONE
    call_kwargs = mock_client.put.call_args
    body = call_kwargs.kwargs.get("json") or call_kwargs.args[1] if len(call_kwargs.args) > 1 else {}
    assert body.get("longUnits") == "ALL"
    assert body.get("shortUnits") == "NONE"


@pytest.mark.asyncio
async def test_close_short_position():
    broker = OandaBroker(api_key="k", account_id="test-account", base_url="https://api-fxpractice.oanda.com/v3")

    pos_resp = mock_response(200, {
        "position": {
            "long": {"units": "0"},
            "short": {"units": "-1000"},
        }
    })
    close_resp = mock_response(200, {
        "shortOrderFillTransaction": {
            "id": "400",
            "orderID": "399",
            "units": "1000",
            "price": "1.07500",
        }
    })

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=pos_resp)
        mock_client.put = AsyncMock(return_value=close_resp)
        mock_client_cls.return_value = mock_client

        result = await broker.submit_order(make_order(OrderAction.CLOSE))

    assert result.success is True
    assert result.filled_quantity == 1000.0

    call_kwargs = mock_client.put.call_args
    body = call_kwargs.kwargs.get("json") or {}
    assert body.get("shortUnits") == "ALL"
    assert body.get("longUnits") == "NONE"


@pytest.mark.asyncio
async def test_close_when_flat_returns_success():
    broker = OandaBroker(api_key="k", account_id="test-account", base_url="https://api-fxpractice.oanda.com/v3")
    pos_resp = mock_response(200, {
        "position": {
            "long": {"units": "0"},
            "short": {"units": "0"},
        }
    })

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=pos_resp)
        mock_client_cls.return_value = mock_client

        result = await broker.submit_order(make_order(OrderAction.CLOSE))

    assert result.success is True
    assert result.filled_quantity == 0.0


@pytest.mark.asyncio
async def test_close_404_returns_success():
    broker = OandaBroker(api_key="k", account_id="test-account", base_url="https://api-fxpractice.oanda.com/v3")
    pos_resp = mock_response(404, {})
    pos_resp.raise_for_status.return_value = None  # 404 doesn't raise in our handler

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=pos_resp)
        mock_client_cls.return_value = mock_client

        result = await broker.submit_order(make_order(OrderAction.CLOSE))

    assert result.success is True


# ── Get Position ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_position_long():
    broker = OandaBroker(api_key="k", account_id="test-account", base_url="https://api-fxpractice.oanda.com/v3")
    resp = mock_response(200, {
        "position": {
            "long": {"units": "5000"},
            "short": {"units": "0"},
        }
    })
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=resp)
        mock_client_cls.return_value = mock_client

        qty = await broker.get_position("primary", "EUR_USD")

    assert qty == 5000.0


@pytest.mark.asyncio
async def test_get_position_short():
    broker = OandaBroker(api_key="k", account_id="test-account", base_url="https://api-fxpractice.oanda.com/v3")
    resp = mock_response(200, {
        "position": {
            "long": {"units": "0"},
            "short": {"units": "-3000"},
        }
    })
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=resp)
        mock_client_cls.return_value = mock_client

        qty = await broker.get_position("primary", "EUR_USD")

    assert qty == -3000.0


@pytest.mark.asyncio
async def test_get_position_404_returns_zero():
    broker = OandaBroker(api_key="k", account_id="test-account", base_url="https://api-fxpractice.oanda.com/v3")
    resp = mock_response(404, {})
    resp.raise_for_status.return_value = None

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=resp)
        mock_client_cls.return_value = mock_client

        qty = await broker.get_position("primary", "EUR_USD")

    assert qty == 0.0


# ── Cancel Order ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_order_success():
    broker = OandaBroker(api_key="k", account_id="test-account", base_url="https://api-fxpractice.oanda.com/v3")
    resp = mock_response(200, {"orderCancelTransaction": {"id": "500"}})

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.put = AsyncMock(return_value=resp)
        mock_client_cls.return_value = mock_client

        ok = await broker.cancel_order("123", "primary")

    assert ok is True


@pytest.mark.asyncio
async def test_cancel_order_404_returns_false():
    broker = OandaBroker(api_key="k", account_id="test-account", base_url="https://api-fxpractice.oanda.com/v3")
    resp = mock_response(404, {"errorMessage": "Order not found"})

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.put = AsyncMock(return_value=resp)
        mock_client_cls.return_value = mock_client

        ok = await broker.cancel_order("999", "primary")

    assert ok is False


# ── Error Handling ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_http_error_returns_failure():
    broker = OandaBroker(api_key="k", account_id="test-account", base_url="https://api-fxpractice.oanda.com/v3")
    error_resp = mock_response(400, {"errorMessage": "Invalid order", "errorCode": "INVALID_UNITS"})

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=error_resp)
        mock_client_cls.return_value = mock_client

        result = await broker.submit_order(make_order())

    assert result.success is False
    assert result.error_message is not None


@pytest.mark.asyncio
async def test_network_error_returns_failure():
    broker = OandaBroker(api_key="k", account_id="test-account", base_url="https://api-fxpractice.oanda.com/v3")

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
        mock_client_cls.return_value = mock_client

        result = await broker.submit_order(make_order())

    assert result.success is False
    assert result.error_message is not None


# ── poll_order_status ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_poll_filled_order():
    broker = OandaBroker(api_key="k", account_id="a", base_url="https://x.com")

    order_resp = mock_response(200, {
        "order": {
            "state": "FILLED",
            "units": "1000",
            "fillingTransactionID": "tx-999",
        }
    })
    tx_resp = mock_response(200, {
        "transaction": {"price": "1.08500", "units": "1000"}
    })

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=[order_resp, tx_resp])
        mock_cls.return_value = mock_client

        result = await broker.poll_order_status("order-123", "primary")

    assert result.found is True
    assert result.is_filled is True
    assert result.filled_quantity == 1000.0
    assert result.avg_fill_price == pytest.approx(1.085)


@pytest.mark.asyncio
async def test_poll_open_order():
    broker = OandaBroker(api_key="k", account_id="a", base_url="https://x.com")

    order_resp = mock_response(200, {"order": {"state": "PENDING", "units": "1000"}})

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=order_resp)
        mock_cls.return_value = mock_client

        result = await broker.poll_order_status("order-456", "primary")

    assert result.found is True
    assert result.is_open is True
    assert result.is_filled is False


@pytest.mark.asyncio
async def test_poll_cancelled_order():
    broker = OandaBroker(api_key="k", account_id="a", base_url="https://x.com")

    order_resp = mock_response(200, {"order": {"state": "CANCELLED"}})

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=order_resp)
        mock_cls.return_value = mock_client

        result = await broker.poll_order_status("order-789", "primary")

    assert result.found is True
    assert result.is_cancelled is True


@pytest.mark.asyncio
async def test_poll_not_found_returns_not_found():
    broker = OandaBroker(api_key="k", account_id="a", base_url="https://x.com")

    not_found_resp = mock_response(404, {"errorMessage": "Order not found"})
    not_found_resp.raise_for_status.return_value = None

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=not_found_resp)
        mock_cls.return_value = mock_client

        result = await broker.poll_order_status("order-gone", "primary")

    assert result.found is False
