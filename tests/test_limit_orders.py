"""
Tests for full limit order support:
  - time_in_force validation (GTC, GTD, DAY, IOC, FOK)
  - GTD requires expire_at
  - Market orders restricted to FOK/IOC
  - OPEN status for resting orders
  - cancel_replace_id flow
  - pending exposure included in risk check
  - /api/orders/open endpoint
  - Oanda body builder for all TIF variants
"""
import pytest
from unittest.mock import patch, AsyncMock
from datetime import datetime, timezone, timedelta
from pydantic import ValidationError

from app.schemas.webhook import WebhookPayload
from app.brokers.oanda import OandaBroker
from app.models.order import (
    Order, OrderAction, OrderType, TimeInForce, OrderStatus, InstrumentType
)
from app.brokers.base import BrokerOrderResult


def _broker():
    return OandaBroker(api_key="k", account_id="test-account",
                       base_url="https://api-fxpractice.oanda.com/v3")


# ── Helpers ────────────────────────────────────────────────────────────────────

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


def limit_payload(**overrides) -> dict:
    return base_payload(order_type="limit", price=1.0800, **overrides)


def make_order(**kwargs) -> Order:
    return Order(
        broker="oanda", account="primary", symbol="EUR_USD",
        instrument_type=InstrumentType.FOREX,
        action=OrderAction.BUY, order_type=OrderType.LIMIT,
        quantity=1000, price=1.0800,
        time_in_force=TimeInForce.GTC,
        **kwargs,
    )


# ── Schema Validation ──────────────────────────────────────────────────────────

def test_limit_defaults_to_gtc():
    p = WebhookPayload(**limit_payload())
    assert p.time_in_force == TimeInForce.GTC


def test_limit_day_tif():
    p = WebhookPayload(**limit_payload(time_in_force="DAY"))
    assert p.time_in_force == TimeInForce.DAY


def test_limit_ioc():
    p = WebhookPayload(**limit_payload(time_in_force="IOC"))
    assert p.time_in_force == TimeInForce.IOC


def test_limit_fok():
    p = WebhookPayload(**limit_payload(time_in_force="FOK"))
    assert p.time_in_force == TimeInForce.FOK


def test_gtd_requires_expire_at():
    with pytest.raises(ValidationError) as exc_info:
        WebhookPayload(**limit_payload(time_in_force="GTD"))
    assert "expire_at" in str(exc_info.value).lower()


def test_gtd_with_expire_at_accepted():
    future = datetime.now(timezone.utc) + timedelta(days=3)
    p = WebhookPayload(**limit_payload(time_in_force="GTD", expire_at=future.isoformat()))
    assert p.time_in_force == TimeInForce.GTD
    assert p.expire_at is not None


def test_market_order_fok_allowed():
    p = WebhookPayload(**base_payload(time_in_force="FOK"))
    assert p.time_in_force == TimeInForce.FOK


def test_market_order_ioc_allowed():
    p = WebhookPayload(**base_payload(time_in_force="IOC"))
    assert p.time_in_force == TimeInForce.IOC


def test_market_order_gtc_rejected():
    with pytest.raises(ValidationError):
        WebhookPayload(**base_payload(time_in_force="GTC"))


def test_market_order_day_rejected():
    with pytest.raises(ValidationError):
        WebhookPayload(**base_payload(time_in_force="DAY"))


def test_cancel_replace_id_accepted():
    p = WebhookPayload(**limit_payload(cancel_replace_id="oanda-order-999"))
    assert p.cancel_replace_id == "oanda-order-999"


# ── Oanda Body Builder ─────────────────────────────────────────────────────────

def test_oanda_limit_gtc_body():
    broker = _broker()
    order = make_order()
    body = broker._build_order_body(order)
    assert body["order"]["type"] == "LIMIT"
    assert body["order"]["timeInForce"] == TimeInForce.GTC
    assert body["order"]["price"] == "1.08000"
    assert "gtdTime" not in body["order"]


def test_oanda_limit_day_body():
    broker = _broker()
    order = make_order(time_in_force=TimeInForce.DAY)
    body = broker._build_order_body(order)
    assert body["order"]["timeInForce"] == TimeInForce.DAY


def test_oanda_limit_gtd_body():
    broker = _broker()
    expire = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    order = make_order(time_in_force=TimeInForce.GTD, expire_at=expire)
    body = broker._build_order_body(order)
    assert body["order"]["timeInForce"] == TimeInForce.GTD
    assert body["order"]["gtdTime"] == "2026-06-01T12:00:00.000000Z"


def test_oanda_limit_ioc_body():
    broker = _broker()
    order = make_order(time_in_force=TimeInForce.IOC)
    body = broker._build_order_body(order)
    assert body["order"]["timeInForce"] == TimeInForce.IOC


def test_oanda_market_fok_default():
    broker = _broker()
    order = Order(
        broker="oanda", account="primary", symbol="EUR_USD",
        instrument_type=InstrumentType.FOREX,
        action=OrderAction.BUY, order_type=OrderType.MARKET,
        quantity=1000, time_in_force=TimeInForce.FOK,
    )
    body = broker._build_order_body(order)
    assert body["order"]["type"] == "MARKET"
    assert body["order"]["timeInForce"] == TimeInForce.FOK


def test_oanda_limit_price_formatted_to_5dp():
    broker = _broker()
    order = make_order(price=1.08)
    body = broker._build_order_body(order)
    assert body["order"]["price"] == "1.08000"


def test_oanda_stop_order_body():
    broker = _broker()
    order = Order(
        broker="oanda", account="primary", symbol="EUR_USD",
        instrument_type=InstrumentType.FOREX,
        action=OrderAction.BUY, order_type=OrderType.STOP,
        quantity=1000, price=1.0900, time_in_force=TimeInForce.GTC,
    )
    body = broker._build_order_body(order)
    assert body["order"]["type"] == "STOP"
    assert body["order"]["price"] == "1.09000"
    assert body["order"]["timeInForce"] == TimeInForce.GTC


# ── Order Processor: OPEN status ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_limit_order_gets_open_status(client, api_key, oanda_broker_account):
    raw_key, tenant_id = api_key
    mock_result = BrokerOrderResult(
        success=True, broker_order_id="oanda-limit-111",
        filled_quantity=0.0, order_open=True,
    )
    with patch("app.brokers.oanda.OandaBroker.submit_order", new_callable=AsyncMock) as m:
        m.return_value = mock_result
        resp = await client.post(
            f"/webhook/{tenant_id}",
            json=limit_payload(),
            headers={"X-Webhook-Secret": raw_key},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "open"
    assert resp.json()["broker_order_id"] == "oanda-limit-111"


@pytest.mark.asyncio
async def test_limit_order_appears_in_open_orders(client, api_key, oanda_broker_account, auth_headers):
    raw_key, tenant_id = api_key
    mock_result = BrokerOrderResult(
        success=True, broker_order_id="oanda-limit-222",
        filled_quantity=0.0, order_open=True,
    )
    with patch("app.brokers.oanda.OandaBroker.submit_order", new_callable=AsyncMock) as m:
        m.return_value = mock_result
        await client.post(
            f"/webhook/{tenant_id}",
            json=limit_payload(),
            headers={"X-Webhook-Secret": raw_key},
        )

    resp = await client.get("/api/orders/open", headers=auth_headers)
    assert resp.status_code == 200
    assert any(o["broker_order_id"] == "oanda-limit-222" for o in resp.json())


@pytest.mark.asyncio
async def test_filled_order_not_in_open_orders(client, api_key, oanda_broker_account, auth_headers):
    raw_key, tenant_id = api_key
    mock_result = BrokerOrderResult(
        success=True, broker_order_id="oanda-market-333",
        filled_quantity=1000.0, avg_fill_price=1.085, order_open=False,
    )
    with patch("app.brokers.oanda.OandaBroker.submit_order", new_callable=AsyncMock) as m:
        m.return_value = mock_result
        await client.post(
            f"/webhook/{tenant_id}",
            json=base_payload(),
            headers={"X-Webhook-Secret": raw_key},
        )

    resp = await client.get("/api/orders/open", headers=auth_headers)
    assert resp.status_code == 200
    assert not any(o["broker_order_id"] == "oanda-market-333" for o in resp.json())


# ── Cancel-Replace ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_replace_updates_old_order(client, api_key, oanda_broker_account, db_session):
    raw_key, tenant_id = api_key
    from app.models.order import Order as OrderModel

    old_order = OrderModel(
        tenant_id=tenant_id,
        broker="oanda", account="primary", symbol="EUR_USD",
        instrument_type=InstrumentType.FOREX,
        action=OrderAction.BUY, order_type=OrderType.LIMIT,
        quantity=1000, price=1.0800, time_in_force=TimeInForce.GTC,
        status=OrderStatus.OPEN, broker_order_id="oanda-old-444",
    )
    db_session.add(old_order)
    await db_session.commit()

    mock_result = BrokerOrderResult(
        success=True, broker_order_id="oanda-new-445",
        filled_quantity=0.0, order_open=True,
    )
    with patch("app.brokers.oanda.OandaBroker.cancel_replace_order", new_callable=AsyncMock) as m:
        m.return_value = mock_result
        resp = await client.post(
            f"/webhook/{tenant_id}",
            json=limit_payload(cancel_replace_id="oanda-old-444", price=1.0850),
            headers={"X-Webhook-Secret": raw_key},
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "open"
    assert resp.json()["broker_order_id"] == "oanda-new-445"

    await db_session.refresh(old_order)
    assert old_order.status == OrderStatus.CANCELLED


@pytest.mark.asyncio
async def test_cancel_replace_unknown_id_rejected(client, api_key, oanda_broker_account):
    raw_key, tenant_id = api_key
    resp = await client.post(
        f"/webhook/{tenant_id}",
        json=limit_payload(cancel_replace_id="does-not-exist-999"),
        headers={"X-Webhook-Secret": raw_key},
    )
    assert resp.status_code == 422
    assert "not found" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_cancel_replace_terminal_order_rejected(client, api_key, oanda_broker_account, db_session):
    raw_key, tenant_id = api_key
    from app.models.order import Order as OrderModel

    filled_order = OrderModel(
        tenant_id=tenant_id,
        broker="oanda", account="primary", symbol="EUR_USD",
        instrument_type=InstrumentType.FOREX,
        action=OrderAction.BUY, order_type=OrderType.LIMIT,
        quantity=1000, price=1.0800, time_in_force=TimeInForce.GTC,
        status=OrderStatus.FILLED, broker_order_id="oanda-filled-500",
    )
    db_session.add(filled_order)
    await db_session.commit()

    resp = await client.post(
        f"/webhook/{tenant_id}",
        json=limit_payload(cancel_replace_id="oanda-filled-500"),
        headers={"X-Webhook-Secret": raw_key},
    )
    assert resp.status_code == 422
    assert "filled" in resp.json()["detail"].lower()


# ── Risk Check: Pending Exposure ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pending_exposure_counted_in_risk_check(client, api_key, oanda_broker_account, db_session):
    raw_key, tenant_id = api_key
    from app.models.order import Order as OrderModel

    open_order = OrderModel(
        tenant_id=tenant_id,
        broker="oanda", account="primary", symbol="EUR_USD",
        instrument_type=InstrumentType.FOREX,
        action=OrderAction.BUY, order_type=OrderType.LIMIT,
        quantity=99_500, price=1.0750, time_in_force=TimeInForce.GTC,
        status=OrderStatus.OPEN, broker_order_id="oanda-pending-600",
    )
    db_session.add(open_order)
    await db_session.commit()

    # Free plan: max_position_size=10_000, so even 1000 on top of 99_500 is way over
    resp = await client.post(
        f"/webhook/{tenant_id}",
        json=limit_payload(quantity=1000),
        headers={"X-Webhook-Secret": raw_key},
    )
    assert resp.status_code in (422, 429)  # rejected by risk or plan limit


@pytest.mark.asyncio
async def test_cancel_replace_doesnt_double_count_replaced_order(
    client, api_key, oanda_broker_account, db_session
):
    raw_key, tenant_id = api_key

    # Upgrade to pro so we have room for large positions
    from app.services.plans import assign_plan
    await assign_plan(db_session, tenant_id, "pro")
    await db_session.commit()

    from app.models.order import Order as OrderModel
    existing = OrderModel(
        tenant_id=tenant_id,
        broker="oanda", account="primary", symbol="EUR_USD",
        instrument_type=InstrumentType.FOREX,
        action=OrderAction.BUY, order_type=OrderType.LIMIT,
        quantity=400_000, price=1.0750, time_in_force=TimeInForce.GTC,
        status=OrderStatus.OPEN, broker_order_id="oanda-big-700",
    )
    db_session.add(existing)
    await db_session.commit()

    mock_result = BrokerOrderResult(
        success=True, broker_order_id="oanda-big-701",
        filled_quantity=0.0, order_open=True,
    )
    with patch("app.brokers.oanda.OandaBroker.cancel_replace_order", new_callable=AsyncMock) as m:
        m.return_value = mock_result
        resp = await client.post(
            f"/webhook/{tenant_id}",
            json=limit_payload(quantity=400_000, price=1.0760,
                               cancel_replace_id="oanda-big-700"),
            headers={"X-Webhook-Secret": raw_key},
        )

    assert resp.status_code == 200
