import uuid
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.position import Position
from app.models.order import Order, OrderAction, DEFAULT_FUTURES_MULTIPLIERS
import logging

logger = logging.getLogger(__name__)


def _resolve_multiplier(order: Order) -> float:
    """
    Determine the point value multiplier for this order's instrument.
    Order of precedence:
      1. order.multiplier if explicitly set (non-default)
      2. DEFAULT_FUTURES_MULTIPLIERS for known futures roots
      3. 1.0 (equities, forex — P&L is price × quantity)
    """
    if order.multiplier and order.multiplier != 1.0:
        return order.multiplier
    # Strip contract suffix for futures (e.g. "ESZ24" → "ES")
    root = order.symbol[:2] if len(order.symbol) >= 2 else order.symbol
    return DEFAULT_FUTURES_MULTIPLIERS.get(order.symbol) or DEFAULT_FUTURES_MULTIPLIERS.get(root, 1.0)


async def get_or_create_position(
    db: AsyncSession, tenant_id: uuid.UUID, broker: str, account: str, symbol: str,
    broker_account_id: int | None = None,
) -> Position:
    result = await db.execute(
        select(Position).where(
            Position.tenant_id == tenant_id,
            Position.broker == broker,
            Position.account == account,
            Position.symbol == symbol,
        )
    )
    pos = result.scalar_one_or_none()
    if pos is None:
        pos = Position(
            tenant_id=tenant_id, broker=broker, account=account, symbol=symbol,
            broker_account_id=broker_account_id,
        )
        db.add(pos)
        await db.flush()
    elif broker_account_id and not pos.broker_account_id:
        pos.broker_account_id = broker_account_id
    return pos


async def apply_fill_to_position(
    db: AsyncSession,
    order: Order,
    filled_qty: float,
    fill_price: float | None,
    position: Position | None = None,
) -> Position:
    """
    Update internal position state after a confirmed fill.

    P&L formula:
      - Equities/forex (multiplier=1):  realized = (exit - entry) × qty
      - Futures (multiplier>1):         realized = (exit - entry) × qty × multiplier

    The multiplier converts price points into account currency.
    E.g. for ES: 1 point × 50 ($/pt) × N contracts = $N×50
    """
    pos = position or await get_or_create_position(
        db, order.tenant_id, order.broker, order.account, order.symbol
    )
    fill_price = fill_price or 0.0
    multiplier = _resolve_multiplier(order)

    # Propagate instrument metadata to position on first fill
    if pos.instrument_type == "forex" and order.instrument_type.value != "forex":
        pos.instrument_type = order.instrument_type.value
    if pos.multiplier == 1.0 and multiplier != 1.0:
        pos.multiplier = multiplier

    old_qty = pos.quantity
    old_avg = pos.avg_price

    if order.action == OrderAction.CLOSE:
        if fill_price and pos.avg_price:
            realized = (fill_price - pos.avg_price) * pos.quantity * multiplier
            pos.realized_pnl += realized
            _update_daily_pnl(pos, realized)
        pos.quantity = 0.0
        pos.avg_price = 0.0

    elif order.action == OrderAction.BUY:
        new_qty = old_qty + filled_qty
        if new_qty != 0 and fill_price:
            pos.avg_price = ((old_qty * old_avg) + (filled_qty * fill_price)) / new_qty
        pos.quantity = new_qty
        # Covering a short position — realize P&L on covered portion
        if old_qty < 0 and fill_price and old_avg:
            covered = min(filled_qty, abs(old_qty))
            realized = (old_avg - fill_price) * covered * multiplier
            pos.realized_pnl += realized
            _update_daily_pnl(pos, realized)

    elif order.action == OrderAction.SELL:
        new_qty = old_qty - filled_qty
        if new_qty != 0 and fill_price:
            pos.avg_price = ((old_qty * old_avg) - (filled_qty * fill_price)) / new_qty
        pos.quantity = new_qty
        # Reducing/closing a long position — realize P&L on sold portion
        if old_qty > 0 and fill_price and old_avg:
            sold = min(filled_qty, old_qty)
            realized = (fill_price - old_avg) * sold * multiplier
            pos.realized_pnl += realized
            _update_daily_pnl(pos, realized)

    pos.updated_at = datetime.now(timezone.utc)
    await db.flush()
    logger.info(f"Position updated: {pos}")
    return pos


def _update_daily_pnl(pos: Position, realized: float):
    today = datetime.now(timezone.utc).date()
    if pos.daily_pnl_date and pos.daily_pnl_date.date() == today:
        pos.daily_realized_pnl += realized
    else:
        pos.daily_realized_pnl = realized
        pos.daily_pnl_date = datetime.now(timezone.utc)
