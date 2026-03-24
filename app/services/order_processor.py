import json
import logging
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.order import Order, OrderStatus, OrderType, InstrumentType, DEFAULT_FUTURES_MULTIPLIERS
from app.schemas.webhook import WebhookPayload
from app.brokers.registry import get_broker_for_tenant
from app.services.state import get_or_create_position, apply_fill_to_position
from app.services.plans import increment_order_count
from app.services.plan_enforcer import PlanEnforcer
from app.config import get_settings

logger = logging.getLogger(__name__)

_recent_signals: dict[str, datetime] = {}
_DEDUP_CLEANUP_AFTER = 1000


def _cleanup_dedup_cache(window_seconds: int):
    if len(_recent_signals) < _DEDUP_CLEANUP_AFTER:
        return
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
    expired = [k for k, v in _recent_signals.items() if v < cutoff]
    for k in expired:
        del _recent_signals[k]


def _dedup_key(tenant_id: int, payload: WebhookPayload) -> str:
    return f"{tenant_id}:{payload.broker}:{payload.account}:{payload.symbol}:{payload.action}:{payload.quantity}"


async def _get_pending_exposure(
    db: AsyncSession, tenant_id: int, broker: str, account: str, symbol: str
) -> float:
    result = await db.execute(
        select(Order).where(
            Order.tenant_id == tenant_id,
            Order.broker == broker,
            Order.account == account,
            Order.symbol == symbol,
            Order.status == OrderStatus.OPEN,
        )
    )
    open_orders = result.scalars().all()
    total = 0.0
    for o in open_orders:
        signed = o.quantity if o.action.value == "buy" else -o.quantity
        total += signed
    return total


async def process_webhook(
    db: AsyncSession,
    payload: WebhookPayload,
    tenant_id: int,
    enforcer: PlanEnforcer | None = None,
) -> Order:
    """
    Full pipeline:
      1.  Deduplication
      2.  Cancel-replace lookup
      3.  Plan enforcement (volume + open orders)
      4.  Risk checks (position size, daily loss)
      5.  Create Order record
      6.  Submit to broker
      7.  Update state + increment order counter
    """
    settings = get_settings()

    # --- 1. Deduplication ---
    dedup_key = _dedup_key(tenant_id, payload)
    now = datetime.now(timezone.utc)
    _cleanup_dedup_cache(settings.duplicate_window_seconds)
    last_seen = _recent_signals.get(dedup_key)
    if last_seen and (now - last_seen) < timedelta(seconds=settings.duplicate_window_seconds):
        logger.warning(f"Duplicate signal suppressed: {dedup_key}")
        raise ValueError(f"Duplicate signal suppressed (within {settings.duplicate_window_seconds}s window)")
    _recent_signals[dedup_key] = now

    # --- 2. Cancel-replace ---
    replaced_order: Order | None = None
    if payload.cancel_replace_id:
        result = await db.execute(
            select(Order).where(
                Order.tenant_id == tenant_id,
                Order.broker_order_id == payload.cancel_replace_id,
                Order.broker == payload.broker,
                Order.account == payload.account,
            )
        )
        replaced_order = result.scalar_one_or_none()
        if replaced_order is None:
            raise ValueError(f"cancel_replace_id {payload.cancel_replace_id!r} not found")
        if replaced_order.is_terminal:
            raise ValueError(
                f"Order {payload.cancel_replace_id} is already {replaced_order.status} "
                "and cannot be replaced"
            )

    # --- 3. Plan enforcement ---
    if enforcer is not None:
        enforcer.check_monthly_volume()
        # Only check open order limit for new limit/stop orders (not cancel-replace, not market)
        if payload.order_type.value != "market" and replaced_order is None:
            await enforcer.check_open_orders(db)

    # --- 4. Risk checks ---
    pos = await get_or_create_position(
        db, tenant_id, payload.broker, payload.account, payload.symbol
    )
    pending_exposure = await _get_pending_exposure(
        db, tenant_id, payload.broker, payload.account, payload.symbol
    )
    if replaced_order and replaced_order.status == OrderStatus.OPEN:
        replaced_signed = (
            replaced_order.quantity if replaced_order.action.value == "buy"
            else -replaced_order.quantity
        )
        pending_exposure -= replaced_signed

    new_signed = payload.quantity if payload.action.value == "buy" else -payload.quantity
    projected_qty = abs(pos.quantity + pending_exposure + new_signed)

    max_pos = enforcer.max_position_size if enforcer else settings.max_position_size
    max_loss = enforcer.max_daily_loss if enforcer else settings.max_daily_loss

    if projected_qty > max_pos:
        raise ValueError(
            f"Order rejected: projected exposure {projected_qty:.0f} exceeds max {max_pos:.0f}"
        )
    if pos.daily_realized_pnl < -abs(max_loss):
        raise ValueError(
            f"Order rejected: daily loss limit reached ({pos.daily_realized_pnl:.2f})"
        )

    # --- 5. Create Order record ---
    # Resolve multiplier: order.multiplier captures it at submission time for P&L accuracy
    root = payload.symbol[:2] if len(payload.symbol) >= 2 else payload.symbol
    multiplier = (
        DEFAULT_FUTURES_MULTIPLIERS.get(payload.symbol)
        or DEFAULT_FUTURES_MULTIPLIERS.get(root, 1.0)
    )

    order = Order(
        tenant_id=tenant_id,
        broker=payload.broker,
        account=payload.account,
        symbol=payload.symbol,
        instrument_type=payload.instrument_type,
        exchange=payload.exchange,
        currency=payload.currency,
        action=payload.action,
        order_type=payload.order_type,
        quantity=payload.quantity,
        price=payload.price,
        time_in_force=payload.time_in_force,
        expire_at=payload.expire_at,
        multiplier=multiplier,
        extended_hours=payload.extended_hours,
        option_expiry=payload.option_expiry,
        option_strike=payload.option_strike,
        option_right=payload.option_right,
        option_multiplier=payload.option_multiplier,
        stop_loss=payload.stop_loss,
        take_profit=payload.take_profit,
        trailing_distance=payload.trailing_distance,
        comment=payload.comment,
        status=OrderStatus.PENDING,
        raw_payload=json.dumps(payload.model_dump(exclude={"secret"}, mode="json")),
    )
    db.add(order)
    await db.flush()
    logger.info(f"Order created: {order}")

    # --- 6. Submit to broker ---
    broker = await get_broker_for_tenant(payload.broker, payload.account, tenant_id, db)
    order.status = OrderStatus.SUBMITTED

    try:
        if replaced_order is not None:
            result = await broker.cancel_replace_order(
                payload.cancel_replace_id, order.account, order
            )
        else:
            result = await broker.submit_order(order)
    except Exception as e:
        logger.exception(f"Exception during broker submission for order {order.id}")
        order.status = OrderStatus.ERROR
        order.error_message = str(e)
        await db.commit()
        return order

    if result.success:
        order.broker_order_id = result.broker_order_id
        order.filled_quantity = result.filled_quantity
        order.avg_fill_price = result.avg_fill_price

        if result.order_open:
            order.status = OrderStatus.OPEN
        elif result.filled_quantity > 0:
            order.status = OrderStatus.FILLED
            await apply_fill_to_position(db, order, result.filled_quantity, result.avg_fill_price)
        else:
            order.status = OrderStatus.SUBMITTED

        if replaced_order is not None:
            replaced_order.status = OrderStatus.CANCELLED
            logger.info(f"Order {replaced_order.id} cancelled (replaced by {order.id})")

        # --- 7. Increment monthly order counter ---
        await increment_order_count(db, tenant_id)
    else:
        order.status = OrderStatus.REJECTED
        order.error_message = result.error_message
        logger.warning(f"Order {order.id} rejected: {result.error_message}")

    await db.commit()
    logger.info(f"Order finalized: {order}")
    return order
