import uuid
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.order import Order, OrderStatus, OrderType, InstrumentType, DEFAULT_FUTURES_MULTIPLIERS, OrderAction
from app.models.broker_account import BrokerAccount
from app.schemas.webhook import WebhookPayload
from app.brokers.registry import get_broker_for_tenant, build_broker_from_account
from app.services.state import get_or_create_position, apply_fill_to_position
from app.services.plans import increment_order_count
from app.services.plan_enforcer import PlanEnforcer
from app.config import get_settings
from app.services.offset_converter import convert_sl_tp

logger = logging.getLogger(__name__)

# In-memory fallback for dedup when Redis is unavailable
_recent_signals: dict[str, datetime] = {}
_DEDUP_CLEANUP_AFTER = 1000


def _cleanup_dedup_cache(window_seconds: int):
    if len(_recent_signals) < _DEDUP_CLEANUP_AFTER:
        return
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
    expired = [k for k, v in _recent_signals.items() if v < cutoff]
    for k in expired:
        del _recent_signals[k]


def _dedup_key(tenant_id: uuid.UUID, payload: WebhookPayload) -> str:
    return f"dedup:{tenant_id}:{payload.broker}:{payload.account}:{payload.symbol}:{payload.action}:{payload.quantity}"


async def _check_dedup(key: str, window_seconds: int) -> bool:
    """
    Returns True if this is a duplicate signal.
    Uses Redis SETNX with TTL, falls back to in-memory dict.
    """
    from app.redis import get_redis
    try:
        r = await get_redis()
        if r is not None:
            was_set = await r.set(key, "1", nx=True, ex=window_seconds)
            return not was_set  # True = duplicate (key already existed)
    except Exception:
        logger.debug("Redis dedup unavailable, using in-memory fallback")

    # In-memory fallback
    now = datetime.now(timezone.utc)
    _cleanup_dedup_cache(window_seconds)
    last_seen = _recent_signals.get(key)
    if last_seen and (now - last_seen) < timedelta(seconds=window_seconds):
        return True
    _recent_signals[key] = now
    return False


async def _get_stream_price(broker: str, account: str, symbol: str) -> float | None:
    """
    Get live mid price for SL/TP conversion.
    Tries: in-process stream manager → Redis cache → None.
    """
    # 1. In-process stream manager (same process, instant)
    try:
        if broker == "oanda":
            from app.services.oanda_stream import get_manager
            mgr = get_manager("oanda", account)
        elif broker == "tradovate":
            from app.services.tradovate_stream import get_manager as get_tv_manager
            mgr = get_tv_manager("tradovate", account)
        else:
            mgr = None
        if mgr:
            cached = mgr.get_price(symbol)
            if cached and cached.get("mid"):
                return cached["mid"]
    except Exception:
        pass

    # 2. Redis price cache (set by worker/stream manager)
    try:
        from app.redis import get_redis
        r = await get_redis()
        if r:
            raw = await r.hget(f"prices:{broker}:{account}", symbol)
            if raw:
                import json as _json
                data = _json.loads(raw)
                if data.get("mid"):
                    return data["mid"]
    except Exception:
        pass

    return None


async def _get_pending_exposure(
    db: AsyncSession, tenant_id: uuid.UUID, broker: str, account: str, symbol: str
) -> float:
    from sqlalchemy import func, case, literal_column
    result = await db.execute(
        select(
            func.coalesce(
                func.sum(
                    case(
                        (Order.action == "buy", Order.quantity),
                        else_=-Order.quantity,
                    )
                ),
                literal_column("0.0"),
            )
        ).where(
            Order.tenant_id == tenant_id,
            Order.broker == broker,
            Order.account == account,
            Order.symbol == symbol,
            Order.status == "open",
        )
    )
    return float(result.scalar_one())


async def _create_trail_trigger(db, order, payload, result):
    """Create a TrailTrigger row for Oanda streaming trail stop activation."""
    from app.models.trail_trigger import TrailTrigger
    from app.models.broker_account import BrokerAccount
    from sqlalchemy import select

    try:
        # Get broker account ID
        acct_result = await db.execute(
            select(BrokerAccount).where(
                BrokerAccount.tenant_id    == order.tenant_id,
                BrokerAccount.broker       == "oanda",
                BrokerAccount.account_alias == order.account,
            )
        )
        acct = acct_result.scalar_one_or_none()
        if not acct:
            return

        trigger = TrailTrigger(
            tenant_id         = order.tenant_id,
            broker_account_id = acct.id,
            order_id          = order.id,
            broker            = "oanda",
            account           = order.account,
            symbol            = order.symbol,
            direction         = order.action.value if hasattr(order.action, 'value') else order.action,
            trigger_price     = order.trail_trigger,        # converted to absolute price by offset_converter
            trail_distance    = order.trail_dist,         # converted distance (post offset_converter)
            trade_id          = result.client_trade_id,
            status            = "pending",
        )
        db.add(trigger)
        await db.commit()
        await db.refresh(trigger)

        # Register with the stream manager if running
        from app.services.oanda_stream import get_manager
        manager = get_manager("oanda", order.account)
        if manager:
            await manager.add_trail_trigger({
                "id":             trigger.id,
                "symbol":         trigger.symbol,
                "direction":      trigger.direction,
                "trigger_price":  trigger.trigger_price,
                "trail_distance": trigger.trail_distance,
                "trade_id":       trigger.trade_id,
                "tenant_id":      str(trigger.tenant_id),
            })

    except Exception as e:
        logger.exception(f"Error creating trail trigger for order {order.id}: {e}")


async def _resolve_fifo_quantity(broker, order: Order) -> int:
    """
    Determine a unique broker-side quantity for FIFO-enabled Oanda accounts.

    NFA FIFO rules require each open trade to have a unique size so that
    individual legs can be identified and closed without ambiguity. Random
    offsets risk collisions; this function guarantees uniqueness by:

      1. Fetching the set of currently-open trade sizes from Oanda
      2. Starting at the requested quantity and walking outward ±1, ±2, ±3...
         alternating direction (add first, then subtract), filling the first gap

    Example — requested qty=2000, existing sizes={2000, 2001, 1999}:
      Try 2001 → taken. Try 1999 → taken. Try 2002 → free → use 2002.

    The minimum returned value is 1. The search is bounded at ±500 units
    to prevent runaway loops; if no gap is found the base quantity is used
    and a warning is logged (operator should investigate).
    """
    from app.brokers.oanda import OandaBroker
    if not isinstance(broker, OandaBroker) or not broker.fifo_randomize:
        return int(order.quantity)

    base = int(order.quantity)
    try:
        taken = await broker.get_open_trade_quantities(order.account, order.symbol)
    except Exception:
        logger.exception("FIFO: failed to fetch open trade quantities — using base qty")
        return base

    if not taken:
        # No existing trades — base quantity is inherently unique
        return base

    MAX_SEARCH = 500
    for step in range(1, MAX_SEARCH + 1):
        for candidate in (base + step, base - step):
            if candidate >= 1 and candidate not in taken:
                logger.info(
                    f"FIFO: {order.symbol} base={base} taken={sorted(taken)} "
                    f"→ using {candidate} (step={step})"
                )
                return candidate

    logger.warning(
        f"FIFO: could not find unique qty for {order.symbol} within ±{MAX_SEARCH} "
        f"of {base} (taken={sorted(taken)}). Using base qty — check for stale positions."
    )
    return base


async def process_webhook(
    db: AsyncSession,
    payload: WebhookPayload,
    tenant_id: uuid.UUID,
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
    if await _check_dedup(dedup_key, settings.duplicate_window_seconds):
        logger.warning(f"Duplicate signal suppressed: {dedup_key}")
        raise ValueError(f"Duplicate signal suppressed (within {settings.duplicate_window_seconds}s window)")


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


    # --- 3b. Symbol translation via instrument_map ---
    # Check all accounts for this broker (not just the target account) so symbol
    # mappings only need to be configured once per broker.
    # Also cache the target broker_account to avoid a redundant DB query later.
    original_symbol = payload.symbol
    cached_broker_account = None
    broker_accounts = await db.execute(
        select(BrokerAccount).where(
            BrokerAccount.tenant_id == tenant_id,
            BrokerAccount.broker == payload.broker,
            BrokerAccount.is_active == True,  # noqa: E712
        )
    )
    for ba in broker_accounts.scalars().all():
        if ba.account_alias == payload.account:
            cached_broker_account = ba
        if ba.instrument_map:
            instr = ba.instrument_map.get(payload.symbol, {})
            if isinstance(instr, dict) and instr.get("target_symbol"):
                logger.info(
                    f"Symbol translation: {payload.symbol} → {instr['target_symbol']} "
                    f"(via instrument_map on {ba.account_alias})"
                )
                payload.symbol = instr["target_symbol"]



    # --- 4. Risk checks ---
    pos = await get_or_create_position(
        db, tenant_id, payload.broker, payload.account, payload.symbol,
        broker_account_id=cached_broker_account.id if cached_broker_account else None,
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

    # Resolve entry price for SL/TP offset conversion.
    # Always prefer the live stream mid over the payload price — the stream is
    # real-time whereas {{close}} from PineScript can be a bar behind.
    # Falls back to payload.price if no live price is available.
    entry_price = payload.price
    has_offsets = (payload.stop_loss is not None or payload.take_profit is not None
                   or payload.trailing_distance is not None or payload.trail_trigger is not None)
    needs_conversion = payload.sl_tp_type in ("pips", "pipettes", "ticks", "points") and has_offsets

    if needs_conversion:
        stream_price = await _get_stream_price(payload.broker, payload.account, payload.symbol)
        if stream_price:
            entry_price = stream_price
            logger.info(
                f"{payload.symbol}: using stream mid {entry_price} "
                f"as entry price for {payload.sl_tp_type} SL/TP conversion"
                + (f" (overriding payload price {payload.price})" if payload.price else "")
            )
        elif payload.price:
            entry_price = payload.price
            logger.info(
                f"{payload.symbol}: no stream price, using payload price {entry_price} "
                f"for {payload.sl_tp_type} SL/TP conversion"
            )

    if needs_conversion and entry_price is None:
        logger.warning(
            f"{payload.symbol}: no live price and no price in payload — "
            f"sl_tp_type='{payload.sl_tp_type}' values will be treated as absolute."
        )

    # Convert SL/TP/trailing from offsets (ticks/pips/points) to absolute prices if needed
    levels = convert_sl_tp(
        action=payload.action.value,
        instrument_type=payload.instrument_type.value,
        symbol=payload.symbol,
        entry_price=entry_price,
        stop_loss=payload.stop_loss,
        take_profit=payload.take_profit,
        trailing_distance=payload.trailing_distance,
        trail_trigger=payload.trail_trigger,
        trail_dist=payload.trail_dist,
        trail_update=payload.trail_update,
        sl_tp_type=payload.sl_tp_type,
    )
    if levels.stop_loss_was_offset or levels.take_profit_was_offset or levels.trailing_was_offset:
        logger.info(
            f"Offset conversion for {payload.symbol}: "
            f"SL {payload.stop_loss}→{levels.stop_loss} "
            f"TP {payload.take_profit}→{levels.take_profit} "
            f"TSL {payload.trailing_distance}→{levels.trailing_distance}"
        )


    raw_payload_str = json.dumps(payload.model_dump(exclude={"secret"}, mode="json"))
    order = Order(
        tenant_id=tenant_id,
        broker_account_id=cached_broker_account.id if cached_broker_account else None,
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
        stop_loss=levels.stop_loss,
        take_profit=levels.take_profit,
        trailing_distance=levels.trailing_distance,
        trail_trigger=levels.trail_trigger,
        trail_dist=levels.trail_dist,
        trail_update=levels.trail_update,
        algo_id=payload.algo_id,
        algo_version=payload.algo_version,
        comment=payload.comment,
        status=OrderStatus.PENDING,
        raw_payload=raw_payload_str,
    )
    db.add(order)

    await db.flush()

    logger.info(f"Order created: {order}")

    # --- 6. Submit to broker ---
    if cached_broker_account:
        broker = build_broker_from_account(cached_broker_account, payload.account)
    else:
        broker = await get_broker_for_tenant(payload.broker, payload.account, tenant_id, db)

    order.status = OrderStatus.SUBMITTED

    # FIFO: resolve a unique broker-side quantity before submission.
    # Only applies to Oanda BUY/SELL — CLOSE uses "ALL" units, not a size.
    if payload.broker == "oanda" and payload.action.value in ("buy", "sell"):
        order.broker_quantity = float(await _resolve_fifo_quantity(broker, order))

    t_broker_start = time.monotonic()
    try:
        if replaced_order is not None:
            result = await broker.cancel_replace_order(
                payload.cancel_replace_id, order.account, order
            )
        else:
            result = await broker.submit_order(order)
    except Exception as e:
        order._broker_latency_ms = (time.monotonic() - t_broker_start) * 1000
        logger.exception(f"Exception during broker submission for order {order.id}")
        order.status = OrderStatus.ERROR
        order.error_message = str(e)
        await db.commit()
        return order
    order._broker_latency_ms = (time.monotonic() - t_broker_start) * 1000

    if result.success:
        order.broker_order_id = result.broker_order_id
        if result.client_trade_id:
            order.client_trade_id = result.client_trade_id
        if result.broker_request:
            order.broker_request = result.broker_request
        if result.broker_response:
            order.broker_response = result.broker_response

        order.filled_quantity = result.filled_quantity
        order.avg_fill_price = result.avg_fill_price

        if result.order_open:
            order.status = OrderStatus.OPEN
        elif result.filled_quantity > 0:
            order.status = OrderStatus.FILLED
            await apply_fill_to_position(db, order, result.filled_quantity, result.avg_fill_price, position=pos)
            # Create trail trigger for Oanda after confirmed fill
            if (
                payload.broker == "oanda"
                and payload.trail_trigger is not None
                and payload.trail_dist is not None
            ):
                await _create_trail_trigger(db, order, payload, result)
        else:
            order.status = OrderStatus.SUBMITTED

        if replaced_order is not None:
            replaced_order.status = OrderStatus.CANCELLED
            logger.info(f"Order {replaced_order.id} cancelled (replaced by {order.id})")

        # --- 7. Increment monthly order counter ---
        await increment_order_count(db, tenant_id, subscription=enforcer.subscription if enforcer else None)
    else:
        order.status = OrderStatus.REJECTED
        order.error_message = result.error_message
        if result.broker_request:
            order.broker_request = result.broker_request
        if result.broker_response:
            order.broker_response = result.broker_response
        logger.warning(f"Order {order.id} rejected: {result.error_message}")


    await db.commit()

    logger.info(f"Order finalized: {order}")

    # Push real-time event to all connected SSE clients
    try:
        from app.services.events import push_delivery_event
        push_delivery_event({
            "order_id":   order.id,
            "tenant_id":  str(tenant_id),
            "broker":     order.broker,
            "account":    order.account,
            "symbol":     order.symbol,
            "action":     order.action.value if hasattr(order.action, "value") else order.action,
            "status":     order.status.value if hasattr(order.status, "value") else order.status,
            "quantity":   order.quantity,
        })
    except Exception:
        logger.exception("Failed to push SSE delivery event — ignoring")

    return order
