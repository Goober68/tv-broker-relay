import pytest
from app.services.state import get_or_create_position, apply_fill_to_position
from app.models.order import Order, OrderAction, OrderType


def make_order(action: OrderAction, qty: float, price: float | None = None) -> Order:
    return Order(
        broker="oanda",
        account="primary",
        symbol="EUR_USD",
        action=action,
        order_type=OrderType.MARKET,
        quantity=qty,
        price=price,
    )


@pytest.mark.asyncio
async def test_position_created_on_first_fill(db_session):
    order = make_order(OrderAction.BUY, 1000)
    pos = await apply_fill_to_position(db_session, order, 1000, 1.0850)
    assert pos.quantity == 1000
    assert pos.avg_price == 1.0850


@pytest.mark.asyncio
async def test_average_price_updates_on_add(db_session):
    order1 = make_order(OrderAction.BUY, 1000)
    await apply_fill_to_position(db_session, order1, 1000, 1.0850)

    order2 = make_order(OrderAction.BUY, 1000)
    pos = await apply_fill_to_position(db_session, order2, 1000, 1.0900)

    assert pos.quantity == 2000
    assert abs(pos.avg_price - 1.0875) < 1e-6


@pytest.mark.asyncio
async def test_realized_pnl_on_sell(db_session):
    buy = make_order(OrderAction.BUY, 1000)
    await apply_fill_to_position(db_session, buy, 1000, 1.0800)

    sell = make_order(OrderAction.SELL, 1000)
    pos = await apply_fill_to_position(db_session, sell, 1000, 1.0900)

    assert pos.quantity == 0
    assert abs(pos.realized_pnl - 10.0) < 1e-6  # (1.09 - 1.08) * 1000


@pytest.mark.asyncio
async def test_close_resets_position(db_session):
    buy = make_order(OrderAction.BUY, 1000)
    await apply_fill_to_position(db_session, buy, 1000, 1.0800)

    close = make_order(OrderAction.CLOSE, 1000)
    pos = await apply_fill_to_position(db_session, close, 1000, 1.0900)

    assert pos.quantity == 0
    assert pos.avg_price == 0


@pytest.mark.asyncio
async def test_short_position(db_session):
    sell = make_order(OrderAction.SELL, 1000)
    pos = await apply_fill_to_position(db_session, sell, 1000, 1.0900)
    assert pos.quantity == -1000


@pytest.mark.asyncio
async def test_get_or_create_idempotent(db_session):
    pos1 = await get_or_create_position(db_session, "oanda", "primary", "GBP_USD")
    pos2 = await get_or_create_position(db_session, "oanda", "primary", "GBP_USD")
    assert pos1.id == pos2.id
