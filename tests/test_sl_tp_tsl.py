"""
Tests for stop loss, take profit, and trailing stop loss support.
Covers schema validation, Oanda order body construction, and end-to-end webhook flow.
"""
import pytest
from unittest.mock import patch, AsyncMock
from pydantic import ValidationError

from app.schemas.webhook import WebhookPayload
from app.brokers.oanda import OandaBroker
from app.models.order import Order, OrderAction, OrderType, TimeInForce, InstrumentType
from app.brokers.base import BrokerOrderResult


def _broker():
    return OandaBroker(api_key="k", account_id="test-account", base_url="https://api-fxpractice.oanda.com/v3")


# ── Schema Validation ──────────────────────────────────────────────────────────

def base_payload(**overrides) -> dict:
    return {
        "secret": "ignored",
        "broker": "oanda",
        "account": "primary",
        "action": "buy",
        "symbol": "EUR_USD",
        "instrument_type": "forex",
        "order_type": "market",
        "quantity": 1000,
        **overrides,
    }


def test_sl_tp_accepted():
    p = WebhookPayload(**base_payload(stop_loss=1.0700, take_profit=1.1000))
    assert p.stop_loss == 1.0700
    assert p.take_profit == 1.1000
    assert p.trailing_distance is None


def test_trailing_stop_accepted():
    p = WebhookPayload(**base_payload(trailing_distance=0.0050))
    assert p.trailing_distance == 0.0050
    assert p.stop_loss is None


def test_tp_only_accepted():
    p = WebhookPayload(**base_payload(take_profit=1.1000))
    assert p.take_profit == 1.1000
    assert p.stop_loss is None
    assert p.trailing_distance is None


def test_sl_only_accepted():
    p = WebhookPayload(**base_payload(stop_loss=1.0700))
    assert p.stop_loss == 1.0700


def test_sl_and_tsl_mutually_exclusive():
    with pytest.raises(ValidationError) as exc_info:
        WebhookPayload(**base_payload(stop_loss=1.0700, trailing_distance=0.0050))
    assert "mutually exclusive" in str(exc_info.value).lower()


def test_tsl_with_tp_accepted():
    p = WebhookPayload(**base_payload(trailing_distance=0.0050, take_profit=1.1000))
    assert p.trailing_distance == 0.0050
    assert p.take_profit == 1.1000


def test_buy_sl_must_be_below_tp():
    with pytest.raises(ValidationError) as exc_info:
        WebhookPayload(**base_payload(stop_loss=1.1000, take_profit=1.0700))
    assert "stop_loss" in str(exc_info.value).lower()


def test_sell_sl_must_be_above_tp():
    with pytest.raises(ValidationError) as exc_info:
        WebhookPayload(**base_payload(
            action="sell",
            stop_loss=1.0700,
            take_profit=1.1000,
        ))
    assert "stop_loss" in str(exc_info.value).lower()


def test_sell_sl_above_tp_accepted():
    p = WebhookPayload(**base_payload(
        action="sell",
        stop_loss=1.1000,
        take_profit=1.0700,
    ))
    assert p.stop_loss == 1.1000
    assert p.take_profit == 1.0700


def test_sl_tp_not_validated_for_limit_orders():
    p = WebhookPayload(**base_payload(
        order_type="limit",
        price=1.0800,
        stop_loss=1.1000,
        take_profit=1.0700,
    ))
    assert p.stop_loss == 1.1000


def test_no_risk_fields_is_fine():
    p = WebhookPayload(**base_payload())
    assert p.stop_loss is None
    assert p.take_profit is None
    assert p.trailing_distance is None


# ── Oanda Order Body Builder ───────────────────────────────────────────────────

def make_order(action=OrderAction.BUY, **kwargs) -> Order:
    return Order(
        broker="oanda", account="primary", symbol="EUR_USD",
        instrument_type=InstrumentType.FOREX,
        action=action, order_type=OrderType.MARKET,
        quantity=1000, time_in_force=TimeInForce.FOK,
        **kwargs,
    )


def test_oanda_body_with_sl_and_tp():
    broker = _broker()
    order = make_order(stop_loss=1.0700, take_profit=1.1000)
    body = broker._build_order_body(order)
    assert "stopLossOnFill" in body["order"]
    assert body["order"]["stopLossOnFill"]["price"] == "1.07000"
    assert body["order"]["stopLossOnFill"]["timeInForce"] == "GTC"
    assert "takeProfitOnFill" in body["order"]
    assert body["order"]["takeProfitOnFill"]["price"] == "1.10000"
    assert "trailingStopLossOnFill" not in body["order"]


def test_oanda_body_with_trailing_stop():
    broker = _broker()
    order = make_order(trailing_distance=0.0050)
    body = broker._build_order_body(order)
    assert "trailingStopLossOnFill" in body["order"]
    assert body["order"]["trailingStopLossOnFill"]["distance"] == "0.00500"
    assert body["order"]["trailingStopLossOnFill"]["timeInForce"] == "GTC"
    assert "stopLossOnFill" not in body["order"]


def test_oanda_body_tsl_with_tp():
    broker = _broker()
    order = make_order(trailing_distance=0.0030, take_profit=1.1200)
    body = broker._build_order_body(order)
    assert "trailingStopLossOnFill" in body["order"]
    assert "takeProfitOnFill" in body["order"]
    assert body["order"]["takeProfitOnFill"]["price"] == "1.12000"
    assert "stopLossOnFill" not in body["order"]


def test_oanda_body_tp_only():
    broker = _broker()
    order = make_order(take_profit=1.1000)
    body = broker._build_order_body(order)
    assert "takeProfitOnFill" in body["order"]
    assert "stopLossOnFill" not in body["order"]
    assert "trailingStopLossOnFill" not in body["order"]


def test_oanda_body_no_risk_fields():
    broker = _broker()
    order = make_order()
    body = broker._build_order_body(order)
    assert "stopLossOnFill" not in body["order"]
    assert "takeProfitOnFill" not in body["order"]
    assert "trailingStopLossOnFill" not in body["order"]


def test_oanda_body_price_precision():
    broker = _broker()
    order = make_order(stop_loss=1.07, take_profit=1.1, trailing_distance=0.005)
    body = broker._build_order_body(order)
    assert body["order"]["trailingStopLossOnFill"]["distance"] == "0.00500"
    assert body["order"]["takeProfitOnFill"]["price"] == "1.10000"


def test_oanda_body_limit_order_with_sl_tp():
    broker = _broker()
    order = Order(
        broker="oanda", account="primary", symbol="EUR_USD",
        instrument_type=InstrumentType.FOREX,
        action=OrderAction.BUY, order_type=OrderType.LIMIT,
        quantity=1000, price=1.0800, time_in_force=TimeInForce.GTC,
        stop_loss=1.0700, take_profit=1.1000,
    )
    body = broker._build_order_body(order)
    assert body["order"]["type"] == "LIMIT"
    assert body["order"]["price"] == "1.08000"
    assert "stopLossOnFill" in body["order"]
    assert "takeProfitOnFill" in body["order"]


# ── End-to-End Webhook Flow ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_webhook_sl_tp_stored_on_order(client, api_key, oanda_broker_account, auth_headers):
    raw_key, tenant_id = api_key
    mock_result = BrokerOrderResult(
        success=True, broker_order_id="oanda-777",
        filled_quantity=1000.0, avg_fill_price=1.0850,
    )
    with patch("app.brokers.oanda.OandaBroker.submit_order", new_callable=AsyncMock) as m:
        m.return_value = mock_result
        resp = await client.post(
            f"/webhook/{tenant_id}",
            json=base_payload(stop_loss=1.0700, take_profit=1.1000),
            headers={"X-Webhook-Secret": raw_key},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "filled"

    orders = (await client.get("/api/orders", headers=auth_headers)).json()
    assert len(orders) >= 1
    order = orders[0]
    assert order["stop_loss"] == 1.0700
    assert order["take_profit"] == 1.1000
    assert order["trailing_distance"] is None


@pytest.mark.asyncio
async def test_webhook_trailing_stop_stored(client, api_key, oanda_broker_account, auth_headers):
    raw_key, tenant_id = api_key
    mock_result = BrokerOrderResult(
        success=True, broker_order_id="oanda-888",
        filled_quantity=1000.0, avg_fill_price=1.0850,
    )
    with patch("app.brokers.oanda.OandaBroker.submit_order", new_callable=AsyncMock) as m:
        m.return_value = mock_result
        resp = await client.post(
            f"/webhook/{tenant_id}",
            json=base_payload(trailing_distance=0.0050, take_profit=1.1200),
            headers={"X-Webhook-Secret": raw_key},
        )
    assert resp.status_code == 200

    orders = (await client.get("/api/orders", headers=auth_headers)).json()
    order = orders[0]
    assert order["trailing_distance"] == 0.0050
    assert order["take_profit"] == 1.1200
    assert order["stop_loss"] is None


@pytest.mark.asyncio
async def test_webhook_sl_tsl_conflict_rejected(client, api_key, oanda_broker_account):
    raw_key, tenant_id = api_key
    resp = await client.post(
        f"/webhook/{tenant_id}",
        json=base_payload(stop_loss=1.0700, trailing_distance=0.0050),
        headers={"X-Webhook-Secret": raw_key},
    )
    assert resp.status_code == 422
