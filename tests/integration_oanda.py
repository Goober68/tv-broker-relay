#!/usr/bin/env python3
"""
Oanda Practice API Integration Tests
=====================================
Runs a series of real API calls against the Oanda practice environment.

Prerequisites:
  1. Copy .env.example to .env and fill in:
       OANDA_API_KEY=your-practice-api-key
       OANDA_ACCOUNT_ID=your-practice-account-id
       OANDA_BASE_URL=https://api-fxpractice.oanda.com/v3
  2. pip install -r requirements.txt python-dotenv

Usage:
  python tests/integration_oanda.py

Each test prints PASS / FAIL with details.
"""

import asyncio
import os
import sys
from pathlib import Path

# Allow running from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

# Validate env before importing app modules
required = ["OANDA_API_KEY", "OANDA_ACCOUNT_ID"]
missing = [k for k in required if not os.environ.get(k)]
if missing:
    print(f"ERROR: Missing env vars: {', '.join(missing)}")
    print("Copy .env.example to .env and fill in your Oanda practice credentials.")
    sys.exit(1)

os.environ.setdefault("WEBHOOK_SECRET", "integration-test")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("OANDA_BASE_URL", "https://api-fxpractice.oanda.com/v3")

from app.brokers.oanda import OandaBroker
from app.models.order import Order, OrderAction, OrderType

SYMBOL = "EUR_USD"
TEST_UNITS = 1000

passed = 0
failed = 0


def result(name: str, ok: bool, detail: str = ""):
    global passed, failed
    status = "✅ PASS" if ok else "❌ FAIL"
    print(f"  {status}  {name}")
    if detail:
        prefix = "       "
        for line in detail.splitlines():
            print(f"{prefix}{line}")
    if ok:
        passed += 1
    else:
        failed += 1


def make_order(action: OrderAction, qty: float = TEST_UNITS, order_type=OrderType.MARKET, price=None) -> Order:
    return Order(
        broker="oanda",
        account="primary",
        symbol=SYMBOL,
        action=action,
        order_type=order_type,
        quantity=qty,
        price=price,
        comment="integration-test",
    )


async def test_get_position(broker: OandaBroker) -> float:
    print("\n── Position Fetch ──")
    qty = await broker.get_position("primary", SYMBOL)
    result("get_position returns a float", isinstance(qty, float), f"qty={qty}")
    return qty


async def test_buy_market(broker: OandaBroker):
    print("\n── Market Buy ──")
    order = make_order(OrderAction.BUY)
    r = await broker.submit_order(order)
    result("submit_order success=True", r.success, r.error_message or "")
    result("broker_order_id set", bool(r.broker_order_id), f"id={r.broker_order_id}")
    result("filled_quantity > 0", r.filled_quantity > 0, f"filled={r.filled_quantity}")
    result("avg_fill_price set", r.avg_fill_price is not None, f"price={r.avg_fill_price}")
    return r


async def test_sell_market(broker: OandaBroker):
    print("\n── Market Sell ──")
    order = make_order(OrderAction.SELL)
    r = await broker.submit_order(order)
    result("submit_order success=True", r.success, r.error_message or "")
    result("broker_order_id set", bool(r.broker_order_id), f"id={r.broker_order_id}")
    return r


async def test_limit_order(broker: OandaBroker):
    print("\n── Limit Order (GTC) ──")
    # Place a limit buy well below market — should be accepted but not filled
    qty = await broker.get_position("primary", SYMBOL)
    # Use a price far below market to ensure it stays open
    limit_price = 0.9000
    order = make_order(OrderAction.BUY, order_type=OrderType.LIMIT, price=limit_price)
    r = await broker.submit_order(order)
    result("limit order accepted", r.success, r.error_message or "")
    result("not immediately filled", r.filled_quantity == 0, f"filled={r.filled_quantity}")
    result("broker_order_id set", bool(r.broker_order_id), f"id={r.broker_order_id}")

    if r.broker_order_id:
        cancelled = await broker.cancel_order(r.broker_order_id, "primary")
        result("cancel_order succeeds", cancelled, f"order_id={r.broker_order_id}")


async def test_close_position(broker: OandaBroker):
    print("\n── Close Position ──")
    # First ensure we have a position to close
    buy = make_order(OrderAction.BUY, TEST_UNITS)
    buy_result = await broker.submit_order(buy)
    if not buy_result.success:
        result("setup buy for close test", False, buy_result.error_message or "")
        return

    close = make_order(OrderAction.CLOSE, TEST_UNITS)
    r = await broker.submit_order(close)
    result("close position success", r.success, r.error_message or "")
    result("filled_quantity > 0", r.filled_quantity > 0, f"filled={r.filled_quantity}")

    # Verify position is now flat (or back to where it was)
    await asyncio.sleep(0.5)
    qty_after = await broker.get_position("primary", SYMBOL)
    result("position is flat after close", abs(qty_after) < 1, f"qty_after={qty_after}")


async def test_close_when_flat(broker: OandaBroker):
    print("\n── Close When Already Flat ──")
    # Ensure flat first
    qty = await broker.get_position("primary", SYMBOL)
    if abs(qty) > 0:
        close = make_order(OrderAction.CLOSE, abs(qty))
        await broker.submit_order(close)
        await asyncio.sleep(0.5)

    close = make_order(OrderAction.CLOSE, 0)
    r = await broker.submit_order(close)
    # Should succeed with a friendly message, not error
    result("close when flat returns success or friendly message",
           r.success or (r.error_message and "flat" in r.error_message.lower()),
           f"success={r.success} msg={r.error_message}")


async def test_buy_with_sl_and_tp(broker: OandaBroker):
    print("\n── Market Buy with Stop Loss + Take Profit ──")
    order = Order(
        broker="oanda", account="primary", symbol=SYMBOL,
        action=OrderAction.BUY, order_type=OrderType.MARKET,
        quantity=TEST_UNITS,
        stop_loss=0.9000,    # far below market — won't trigger
        take_profit=9.9000,  # far above market — won't trigger
        comment="integration-test-sl-tp",
    )
    r = await broker.submit_order(order)
    result("buy with SL+TP success", r.success, r.error_message or "")
    result("filled", r.filled_quantity > 0, f"filled={r.filled_quantity}")

    if r.success:
        # Clean up
        await asyncio.sleep(0.3)
        close = make_order(OrderAction.CLOSE, TEST_UNITS)
        cr = await broker.submit_order(close)
        result("cleanup close after SL+TP order", cr.success, cr.error_message or "")


async def test_buy_with_trailing_stop(broker: OandaBroker):
    print("\n── Market Buy with Trailing Stop ──")
    order = Order(
        broker="oanda", account="primary", symbol=SYMBOL,
        action=OrderAction.BUY, order_type=OrderType.MARKET,
        quantity=TEST_UNITS,
        trailing_distance=0.0500,  # 500 pip trail — won't trigger immediately
        take_profit=9.9000,
        comment="integration-test-tsl",
    )
    r = await broker.submit_order(order)
    result("buy with trailing stop success", r.success, r.error_message or "")
    result("filled", r.filled_quantity > 0, f"filled={r.filled_quantity}")

    if r.success:
        await asyncio.sleep(0.3)
        close = make_order(OrderAction.CLOSE, TEST_UNITS)
        cr = await broker.submit_order(close)
        result("cleanup close after TSL order", cr.success, cr.error_message or "")


async def test_limit_order_with_sl_tp(broker: OandaBroker):
    print("\n── Limit Order with Stop Loss + Take Profit (GTC) ──")
    order = Order(
        broker="oanda", account="primary", symbol=SYMBOL,
        action=OrderAction.BUY, order_type=OrderType.LIMIT,
        quantity=TEST_UNITS,
        price=0.9000,        # far below market — stays open
        stop_loss=0.8000,
        take_profit=9.9000,
        comment="integration-test-limit-sl-tp",
    )
    r = await broker.submit_order(order)
    result("limit order with SL+TP accepted", r.success, r.error_message or "")
    result("not immediately filled", r.filled_quantity == 0, f"filled={r.filled_quantity}")

    if r.broker_order_id:
        cancelled = await broker.cancel_order(r.broker_order_id, "primary")
        result("cancel limit+SL+TP order", cancelled, f"id={r.broker_order_id}")


async def main():
    print("=" * 50)
    print("  Oanda Practice API Integration Tests")
    print(f"  Account: {os.environ['OANDA_ACCOUNT_ID']}")
    print(f"  URL:     {os.environ.get('OANDA_BASE_URL', 'https://api-fxpractice.oanda.com/v3')}")
    print("=" * 50)

    broker = OandaBroker.from_settings()

    await test_get_position(broker)
    await test_buy_market(broker)
    await test_sell_market(broker)
    await test_limit_order(broker)
    await test_close_position(broker)
    await test_close_when_flat(broker)
    await test_buy_with_sl_and_tp(broker)
    await test_buy_with_trailing_stop(broker)
    await test_limit_order_with_sl_tp(broker)

    print("\n" + "=" * 50)
    print(f"  Results: {passed} passed, {failed} failed")
    print("=" * 50)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
