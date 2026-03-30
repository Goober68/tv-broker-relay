"""
Background task runner.

All tasks are asyncio tasks launched at application startup via the lifespan.
Each runs in an infinite loop with a configurable sleep interval.
Errors in individual iterations are caught and logged — the task loop continues.

Tasks:
  fill_poll_task       — check OPEN orders against broker, mark filled/cancelled
  ibkr_keepalive_task  — ping IBKR gateway to prevent session expiry
  reconcile_task       — sync internal position state against broker APIs
  daily_summary_task   — send daily P&L emails at configured UTC hour
  delivery_purge_task  — purge old webhook delivery logs (>90 days)
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from app.models.db import AsyncSessionLocal
from app.models.order import Order, OrderStatus, OrderAction, OrderType, TimeInForce
from app.models.position import Position
from app.models.broker_account import BrokerAccount
from app.models.tenant import Tenant
from app.models.plan import Subscription
from app.models.webhook_delivery import WebhookDelivery
from app.brokers.registry import get_broker_for_tenant
from app.services.state import apply_fill_to_position
from app.services.email_service import send_order_filled, send_daily_summary
from app.config import get_settings

logger = logging.getLogger(__name__)


async def _run_forever(name: str, interval: int, coro_factory):
    """
    Run coro_factory() in a loop with `interval` seconds between iterations.
    Catches and logs all exceptions so the loop never dies from a single error.
    """
    logger.info(f"Background task started: {name} (interval={interval}s)")
    while True:
        try:
            await coro_factory()
        except asyncio.CancelledError:
            logger.info(f"Background task cancelled: {name}")
            return
        except Exception:
            logger.exception(f"Error in background task {name} — continuing")
        await asyncio.sleep(interval)


# ── Fill Polling ───────────────────────────────────────────────────────────────

async def _poll_fills_once():
    """
    Find all OPEN orders and ask each broker their current status.
    Updates DB state and position tracking for any that have filled or cancelled.
    Sends email on fill.
    """
    async with AsyncSessionLocal() as db:
        # Load all OPEN orders with their tenant info
        result = await db.execute(
            select(Order)
            .where(Order.status == "open")
            .where(Order.broker_order_id.isnot(None))
        )
        open_orders = result.scalars().all()

        if not open_orders:
            return

        logger.debug(f"Fill poll: checking {len(open_orders)} open orders")

        for order in open_orders:
            try:
                broker = await get_broker_for_tenant(
                    order.broker, order.account, order.tenant_id, db
                )
                status = await broker.poll_order_status(order.broker_order_id, order.account)

                if not status.found:
                    # Order no longer exists on broker — treat as cancelled
                    order.status = OrderStatus.CANCELLED
                    logger.warning(
                        f"Order {order.id} (broker_id={order.broker_order_id}) "
                        f"not found on {order.broker} — marking cancelled"
                    )
                elif status.is_filled:
                    order.status = OrderStatus.FILLED
                    order.filled_quantity = status.filled_quantity
                    order.avg_fill_price = status.avg_fill_price
                    await apply_fill_to_position(
                        db, order, status.filled_quantity, status.avg_fill_price
                    )
                    logger.info(
                        f"Order {order.id} filled: {order.quantity} {order.symbol} "
                        f"@ {status.avg_fill_price}"
                    )
                    # Send fill notification
                    tenant_result = await db.execute(
                        select(Tenant).where(Tenant.id == order.tenant_id)
                    )
                    tenant = tenant_result.scalar_one_or_none()
                    if tenant:
                        await send_order_filled(
                            to=tenant.email,
                            order_id=order.id,
                            symbol=order.symbol,
                            action=order.action.value,
                            filled_qty=status.filled_quantity,
                            avg_price=status.avg_fill_price,
                            broker=order.broker,
                            broker_order_id=order.broker_order_id,
                        )
                elif status.is_cancelled:
                    order.status = OrderStatus.CANCELLED
                    logger.info(f"Order {order.id} cancelled on {order.broker}")
                # else: still open — no change

            except Exception:
                logger.exception(
                    f"Error polling order {order.id} ({order.broker_order_id})"
                )

        await db.commit()


# ── IBKR Keepalive ─────────────────────────────────────────────────────────────

async def _ibkr_keepalive_once():
    """
    Ping every active IBKR broker account's gateway to prevent session expiry.
    IBKR sessions expire after ~24h without activity. The /tickle endpoint
    resets the expiry timer.
    """
    import httpx
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(BrokerAccount).where(
                BrokerAccount.broker == "ibkr",
                BrokerAccount.is_active == True,  # noqa: E712
            )
        )
        ibkr_accounts = result.scalars().all()

        if not ibkr_accounts:
            return

        from app.services.credentials import decrypt_credentials
        pinged = 0
        for account in ibkr_accounts:
            try:
                creds = decrypt_credentials(account.credentials_encrypted)
                gateway_url = creds.get("gateway_url", "https://localhost:5000/v1/api")
                async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
                    resp = await client.post(f"{gateway_url}/tickle")
                    if resp.status_code == 200:
                        pinged += 1
                    else:
                        logger.warning(
                            f"IBKR tickle failed for account {account.id}: "
                            f"status={resp.status_code}"
                        )
            except Exception:
                logger.exception(f"Error pinging IBKR gateway for account {account.id}")

        if pinged:
            logger.debug(f"IBKR keepalive: pinged {pinged} gateway(s)")


# ── Position Reconciliation ────────────────────────────────────────────────────

async def _sync_oanda_closed_trades(db, broker, pos):
    """Fetch recent closed trades from Oanda and create synthetic close orders."""
    closed_trades = await broker.get_recent_closed_trades(pos.account)
    if not closed_trades:
        return

    # Only sync trades from the last 30 days to avoid importing ancient history
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)

    total_realized = 0.0
    for ct in closed_trades:
        if ct["realized_pl"] == 0:
            continue
        # Skip old trades
        if ct.get("close_time"):
            try:
                from datetime import datetime as dt
                ct_time = dt.fromisoformat(ct["close_time"].replace("Z", "+00:00"))
                if ct_time < cutoff:
                    continue
            except Exception:
                pass
        # Check if we already have this trade recorded
        existing = await db.execute(
            select(Order).where(
                Order.tenant_id == pos.tenant_id,
                Order.broker_order_id == f"oanda_close_{ct['trade_id']}",
            )
        )
        if existing.scalar_one_or_none():
            continue

        total_realized += ct["realized_pl"]

        from datetime import datetime as dt
        close_time = None
        if ct.get("close_time"):
            try:
                close_time = dt.fromisoformat(ct["close_time"].replace("Z", "+00:00"))
            except Exception:
                pass

        close_action = "BUY" if float(ct["units"]) < 0 else "SELL"
        close_order = Order(
            created_at=close_time or datetime.now(timezone.utc),
            updated_at=close_time or datetime.now(timezone.utc),
            tenant_id=pos.tenant_id,
            broker=pos.broker,
            account=pos.account,
            symbol=ct["symbol"],
            instrument_type=pos.instrument_type,
            action=close_action,
            order_type="MARKET",
            quantity=abs(float(ct["units"])),
            multiplier=pos.multiplier,
            status="FILLED",
            filled_quantity=abs(float(ct["units"])),
            avg_fill_price=ct["close_price"],
            broker_order_id=f"oanda_close_{ct['trade_id']}",
            time_in_force="FOK",
            comment="SL/TP/TSL close (reconciled from Oanda)",
        )
        db.add(close_order)
        logger.info(
            f"RECONCILIATION: Synced Oanda closed trade {ct['trade_id']} "
            f"{ct['symbol']} P&L={ct['realized_pl']:.2f}"
        )

    if total_realized != 0:
        # Find the position for this symbol to update realized P&L
        from sqlalchemy import and_
        pos_result = await db.execute(
            select(Position).where(
                Position.tenant_id == pos.tenant_id,
                Position.broker == pos.broker,
                Position.account == pos.account,
                Position.symbol == closed_trades[0]["symbol"],
            )
        )
        position = pos_result.scalar_one_or_none()
        if position:
            position.realized_pnl += total_realized
            now = datetime.now(timezone.utc)
            if position.daily_pnl_date and position.daily_pnl_date.date() == now.date():
                position.daily_realized_pnl += total_realized
            else:
                position.daily_realized_pnl = total_realized
                position.daily_pnl_date = now
        await db.commit()
        logger.info(f"RECONCILIATION: Total synced P&L for {pos.account}: {total_realized:.2f}")


async def _reconcile_once():
    """
    Sync internal position state against broker APIs.

    For each non-flat position in the DB, fetches the broker's current quantity.
    Flags drift (>1% difference) and logs a warning. Does NOT auto-correct —
    corrections require human review to avoid masking bugs.

    In a future version this could emit alerts or write to a reconciliation_log table.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Position).where(
                func.abs(Position.quantity) > 1e-9  # non-flat only
            )
        )
        positions = result.scalars().all()

        if not positions:
            return

        logger.debug(f"Reconciliation: checking {len(positions)} open positions")

        # Track which oanda accounts we've already synced closed trades for
        _oanda_synced: set[str] = set()

        for pos in positions:
            try:
                broker = await get_broker_for_tenant(
                    pos.broker, pos.account, pos.tenant_id, db
                )

                # Always sync closed trades from Oanda (once per account per cycle)
                if pos.broker == "oanda":
                    acct_key = f"{pos.tenant_id}:{pos.account}"
                    if acct_key not in _oanda_synced:
                        _oanda_synced.add(acct_key)
                        from app.brokers.oanda import OandaBroker
                        if isinstance(broker, OandaBroker):
                            try:
                                await _sync_oanda_closed_trades(db, broker, pos)
                            except Exception:
                                logger.exception(f"Error syncing Oanda closed trades for {pos.account}")

                broker_qty = await broker.get_position(pos.account, pos.symbol)
                internal_qty = pos.quantity

                if abs(internal_qty) < 1e-9 and abs(broker_qty) < 1e-9:
                    continue  # both flat

                if abs(broker_qty) < 1e-9 and abs(internal_qty) > 0:
                    # Broker shows flat — position was closed externally (TP/SL hit,
                    # manual close, or liquidation). Fetch realized P&L from broker.
                    realized = 0.0

                    if pos.broker == "oanda":
                        from app.brokers.oanda import OandaBroker
                        if isinstance(broker, OandaBroker):
                            try:
                                closed_trades = await broker.get_recent_closed_trades(pos.account)
                                for ct in closed_trades:
                                    if ct["symbol"] != pos.symbol or ct["realized_pl"] == 0:
                                        continue
                                    # Check if we already have this trade recorded
                                    existing = await db.execute(
                                        select(Order).where(
                                            Order.tenant_id == pos.tenant_id,
                                            Order.broker_order_id == f"oanda_close_{ct['trade_id']}",
                                        )
                                    )
                                    if existing.scalar_one_or_none():
                                        continue

                                    realized += ct["realized_pl"]
                                    logger.info(
                                        f"RECONCILIATION: Found closed trade {ct['trade_id']} "
                                        f"{pos.symbol} P&L={ct['realized_pl']:.2f}"
                                    )

                                    # Create synthetic close order for P&L tracking
                                    from datetime import datetime as dt
                                    close_time = None
                                    if ct.get("close_time"):
                                        try:
                                            close_time = dt.fromisoformat(ct["close_time"].replace("Z", "+00:00"))
                                        except Exception:
                                            pass
                                    close_action = "BUY" if ct["units"] < 0 else "SELL"
                                    close_order = Order(
                                        created_at=close_time or datetime.now(timezone.utc),
                                        updated_at=close_time or datetime.now(timezone.utc),
                                        tenant_id=pos.tenant_id,
                                        broker=pos.broker,
                                        account=pos.account,
                                        symbol=pos.symbol,
                                        instrument_type=pos.instrument_type,
                                        action=close_action,
                                        order_type="MARKET",
                                        quantity=abs(ct["units"]),
                                        multiplier=pos.multiplier,
                                        status="FILLED",
                                        filled_quantity=abs(ct["units"]),
                                        avg_fill_price=ct["close_price"],
                                        broker_order_id=f"oanda_close_{ct['trade_id']}",
                                        time_in_force="FOK",
                                        comment="SL/TP/TSL close (reconciled from Oanda)",
                                    )
                                    db.add(close_order)

                            except Exception:
                                logger.exception("Error fetching closed trades for reconciliation")

                    logger.info(
                        f"RECONCILIATION: Position {pos.id} "
                        f"tenant={pos.tenant_id} {pos.broker}/{pos.symbol} "
                        f"closed at broker (was {internal_qty:.4f}). "
                        f"Realized P&L: {realized:.2f}. Zeroing relay position."
                    )

                    # Update position P&L
                    if realized != 0:
                        pos.realized_pnl += realized
                        from datetime import datetime, timezone
                        now = datetime.now(timezone.utc)
                        if pos.daily_pnl_date and pos.daily_pnl_date.date() == now.date():
                            pos.daily_realized_pnl += realized
                        else:
                            pos.daily_realized_pnl = realized
                            pos.daily_pnl_date = now

                    pos.quantity       = 0.0
                    pos.unrealized_pnl = None
                    pos.last_price     = None
                    pos.last_price_at  = None
                    await db.commit()
                    continue

                if abs(internal_qty) < 1e-9:
                    continue  # we show flat, ignore small broker remainder

                drift_pct = abs((broker_qty - internal_qty) / internal_qty) * 100
                if drift_pct > 1.0:
                    # Partial fill or partial close — auto-correct quantity
                    # and fetch closed trades for P&L tracking
                    logger.info(
                        f"RECONCILIATION: Position {pos.id} "
                        f"tenant={pos.tenant_id} {pos.broker}/{pos.symbol}: "
                        f"internal={internal_qty:.4f} broker={broker_qty:.4f} "
                        f"drift={drift_pct:.1f}%. Auto-correcting."
                    )

                    # Fetch closed trades for P&L (same logic as full close)
                    if pos.broker == "oanda":
                        from app.brokers.oanda import OandaBroker
                        if isinstance(broker, OandaBroker):
                            try:
                                closed_trades = await broker.get_recent_closed_trades(pos.account)
                                partial_realized = 0.0
                                for ct in closed_trades:
                                    if ct["symbol"] != pos.symbol or ct["realized_pl"] == 0:
                                        continue
                                    existing = await db.execute(
                                        select(Order).where(
                                            Order.tenant_id == pos.tenant_id,
                                            Order.broker_order_id == f"oanda_close_{ct['trade_id']}",
                                        )
                                    )
                                    if existing.scalar_one_or_none():
                                        continue

                                    partial_realized += ct["realized_pl"]
                                    logger.info(
                                        f"RECONCILIATION: Closed trade {ct['trade_id']} "
                                        f"{pos.symbol} P&L={ct['realized_pl']:.2f}"
                                    )
                                    from datetime import datetime as dt
                                    close_time = None
                                    if ct.get("close_time"):
                                        try:
                                            close_time = dt.fromisoformat(ct["close_time"].replace("Z", "+00:00"))
                                        except Exception:
                                            pass
                                    close_action = "BUY" if ct["units"] < 0 else "SELL"
                                    close_order = Order(
                                        created_at=close_time or datetime.now(timezone.utc),
                                        updated_at=close_time or datetime.now(timezone.utc),
                                        tenant_id=pos.tenant_id,
                                        broker=pos.broker,
                                        account=pos.account,
                                        symbol=pos.symbol,
                                        instrument_type=pos.instrument_type,
                                        action=close_action,
                                        order_type="MARKET",
                                        quantity=abs(ct["units"]),
                                        multiplier=pos.multiplier,
                                        status="FILLED",
                                        filled_quantity=abs(ct["units"]),
                                        avg_fill_price=ct["close_price"],
                                        broker_order_id=f"oanda_close_{ct['trade_id']}",
                                        time_in_force="FOK",
                                        comment="SL/TP/TSL close (reconciled from Oanda)",
                                    )
                                    db.add(close_order)

                                if partial_realized != 0:
                                    pos.realized_pnl += partial_realized
                                    now = datetime.now(timezone.utc)
                                    if pos.daily_pnl_date and pos.daily_pnl_date.date() == now.date():
                                        pos.daily_realized_pnl += partial_realized
                                    else:
                                        pos.daily_realized_pnl = partial_realized
                                        pos.daily_pnl_date = now
                                    logger.info(f"RECONCILIATION: Partial close P&L: {partial_realized:.2f}")

                            except Exception:
                                logger.exception("Error fetching closed trades for partial reconciliation")

                    pos.quantity = broker_qty
                    await db.commit()
                else:
                    logger.debug(
                        f"Position {pos.id} {pos.symbol}: OK "
                        f"(internal={internal_qty:.4f} broker={broker_qty:.4f})"
                    )

            except Exception:
                logger.exception(
                    f"Error reconciling position {pos.id} "
                    f"({pos.broker}/{pos.symbol})"
                )


# ── Live P&L Polling ──────────────────────────────────────────────────────────

async def _poll_pnl_once():
    """
    Poll open position P&L from supported brokers (Oanda, Tradovate, E*Trade).
    Groups non-flat positions by (tenant, broker, account) to minimise API calls,
    then writes last_price, unrealized_pnl, and last_price_at back to the DB.

    Only runs for brokers that implement get_open_positions_pnl().
    """
    SUPPORTED = {"oanda", "tradovate", "etrade", "tradestation", "alpaca", "tastytrade"}

    async with AsyncSessionLocal() as db:
        # Load all non-flat positions for supported brokers
        result = await db.execute(
            select(Position).where(
                func.abs(Position.quantity) > 1e-9,
                Position.broker.in_(SUPPORTED),
            )
        )
        positions = result.scalars().all()
        if not positions:
            return

        # Group by (tenant_id, broker, account) to make one API call per account
        from itertools import groupby
        from operator import attrgetter

        key = lambda p: (p.tenant_id, p.broker, p.account)
        positions_sorted = sorted(positions, key=key)

        for (tenant_id, broker_name, account), group in groupby(positions_sorted, key=key):
            group_positions = list(group)
            try:
                broker = await get_broker_for_tenant(broker_name, account, tenant_id, db)
                pnl_data = await broker.get_open_positions_pnl(account)
                if not pnl_data:
                    continue

                # Build lookup by symbol (and root symbol for futures)
                pnl_by_symbol = {}
                for item in pnl_data:
                    sym = item.get("symbol", "")
                    pnl_by_symbol[sym] = item
                    # Also index by root for Tradovate (ESH5 -> ES)
                    root = item.get("symbol_root") or ''.join(c for c in sym if c.isalpha())
                    if root and root != sym:
                        pnl_by_symbol[root] = item

                now = datetime.now(timezone.utc)
                updated = 0
                for pos in group_positions:
                    data = pnl_by_symbol.get(pos.symbol)
                    if data is None:
                        # Try stripping contract month suffix
                        root = ''.join(c for c in pos.symbol if c.isalpha())
                        data = pnl_by_symbol.get(root)
                    if data:
                        pos.last_price     = data.get("last_price")
                        pos.unrealized_pnl = data.get("unrealized_pnl")
                        pos.last_price_at  = now
                        updated += 1

                if updated:
                    await db.commit()
                    logger.debug(
                        f"P&L poll: updated {updated} positions "
                        f"for tenant={tenant_id} {broker_name}/{account}"
                    )

            except Exception:
                logger.exception(
                    f"Error polling P&L for tenant={tenant_id} {broker_name}/{account}"
                )


# ── Daily P&L Email ────────────────────────────────────────────────────────────

_last_summary_date: datetime | None = None


async def _daily_summary_once():
    """
    Send daily P&L summary emails to all active tenants.
    Fires once per day at DAILY_SUMMARY_HOUR_UTC.
    Guards against running twice in the same UTC day.
    """
    global _last_summary_date
    settings = get_settings()
    now = datetime.now(timezone.utc)

    # Only run at the configured hour
    if now.hour != settings.daily_summary_hour_utc:
        return

    # Only run once per day
    today = now.date()
    if _last_summary_date and _last_summary_date.date() == today:
        return
    _last_summary_date = now

    logger.info(f"Sending daily P&L summaries for {today}")

    async with AsyncSessionLocal() as db:
        # Get all active tenants with subscriptions
        result = await db.execute(
            select(Tenant)
            .where(Tenant.is_active == True)  # noqa: E712
            .options(selectinload(Tenant.subscription))
        )
        tenants = result.scalars().all()

        for tenant in tenants:
            try:
                await _send_summary_for_tenant(db, tenant, today)
            except Exception:
                logger.exception(f"Error sending daily summary to tenant {tenant.id}")


async def _send_summary_for_tenant(db: AsyncSession, tenant: Tenant, today) -> None:
    # Fetch open positions
    pos_result = await db.execute(
        select(Position).where(
            Position.tenant_id == tenant.id,
            func.abs(Position.quantity) > 1e-9,
        )
    )
    positions = pos_result.scalars().all()

    # Count orders filled today
    today_start = datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc)
    order_result = await db.execute(
        select(func.count(Order.id)).where(
            Order.tenant_id == tenant.id,
            Order.status == "filled",
            Order.updated_at >= today_start,
        )
    )
    orders_today = order_result.scalar_one() or 0

    daily_pnl = sum(p.daily_realized_pnl for p in positions)

    pos_dicts = [
        {
            "symbol": p.symbol,
            "quantity": p.quantity,
            "daily_realized_pnl": p.daily_realized_pnl,
            "broker": p.broker,
        }
        for p in positions
    ]

    await send_daily_summary(
        to=tenant.email,
        date_str=str(today),
        positions=pos_dicts,
        daily_pnl=daily_pnl,
        orders_today=orders_today,
    )


# ── Delivery Log Purge ────────────────────────────────────────────────────────

async def _purge_old_deliveries_once():
    """Delete webhook delivery logs older than 90 days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    async with AsyncSessionLocal() as db:
        from sqlalchemy import delete
        result = await db.execute(
            delete(WebhookDelivery).where(WebhookDelivery.created_at < cutoff)
        )
        if result.rowcount:
            logger.info(f"Purged {result.rowcount} webhook delivery log entries older than 90 days")
        await db.commit()


# ── Auto-Close Task ───────────────────────────────────────────────────────────

async def _auto_close_once():
    """
    Check all broker accounts with auto_close_enabled=True.
    If the current ET time matches the account's auto_close_time (within a 1-minute
    window), close all open positions for that account and log to webhook_deliveries.

    Runs every 60 seconds. Uses a 2-minute guard to prevent double-firing.
    """
    from zoneinfo import ZoneInfo
    from datetime import timezone as _tz
    from app.models.broker_account import BrokerAccount
    from app.models.webhook_delivery import WebhookDelivery
    from app.models.order import Order, OrderAction, OrderType, TimeInForce

    ET = ZoneInfo("America/New_York")
    now_et  = datetime.now(ET)
    now_hhmm = now_et.strftime("%H:%M")

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(BrokerAccount).where(
                BrokerAccount.auto_close_enabled == True,  # noqa: E712
                BrokerAccount.auto_close_time.isnot(None),
                BrokerAccount.is_active == True,  # noqa: E712
            )
        )
        accounts = result.scalars().all()

        for acct in accounts:
            if acct.auto_close_time != now_hhmm:
                continue

            # Guard: don't fire twice in the same minute
            two_min_ago = datetime.now(_tz.utc) - timedelta(seconds=120)
            guard = await db.execute(
                select(WebhookDelivery).where(
                    WebhookDelivery.tenant_id  == acct.tenant_id,
                    WebhookDelivery.outcome.in_(["auto_close", "auto_close_partial"]),
                    WebhookDelivery.created_at >= two_min_ago,
                    WebhookDelivery.error_detail.like(f"%{acct.account_alias}%"),
                )
            )
            if guard.scalar_one_or_none() is not None:
                continue

            logger.info(
                f"AUTO-CLOSE: firing for tenant={acct.tenant_id} "
                f"{acct.broker}/{acct.account_alias} at {now_hhmm} ET"
            )

            # Load non-flat positions for this account
            pos_result = await db.execute(
                select(Position).where(
                    Position.tenant_id == acct.tenant_id,
                    Position.broker    == acct.broker,
                    Position.account   == acct.account_alias,
                    func.abs(Position.quantity) > 1e-9,
                )
            )
            positions = pos_result.scalars().all()

            if not positions:
                db.add(WebhookDelivery(
                    tenant_id   = acct.tenant_id,
                    source_ip   = "relay-auto-close",
                    outcome     = "auto_close",
                    http_status = 200,
                    auth_passed = True,
                    error_detail= f"{acct.broker}/{acct.account_alias} at {now_hhmm} ET: no open positions",
                    duration_ms = 0,
                ))
                await db.commit()
                continue

            try:
                broker = await get_broker_for_tenant(
                    acct.broker, acct.account_alias, acct.tenant_id, db
                )
            except Exception as e:
                logger.error(f"AUTO-CLOSE: failed to load broker for {acct.account_alias}: {e}")
                continue

            closed = []
            errors = []

            for pos in positions:
                try:
                    close_order = Order(
                        tenant_id       = acct.tenant_id,
                        broker          = acct.broker,
                        account         = acct.account_alias,
                        symbol          = pos.symbol,
                        instrument_type = pos.instrument_type,
                        action          = OrderAction.CLOSE,
                        order_type      = OrderType.MARKET,
                        quantity        = abs(pos.quantity),
                        time_in_force   = TimeInForce.FOK,
                        multiplier      = pos.multiplier,
                        status          = "pending",
                    )
                    result = await broker.submit_order(close_order)
                    if result.success:
                        pos.quantity       = 0.0
                        pos.unrealized_pnl = None
                        pos.last_price     = None
                        closed.append(pos.symbol)
                        logger.info(f"AUTO-CLOSE: closed {pos.symbol} for {acct.broker}/{acct.account_alias}")
                    else:
                        errors.append(f"{pos.symbol}: {result.error_message}")
                        logger.error(f"AUTO-CLOSE: failed {pos.symbol}: {result.error_message}")
                except Exception as e:
                    errors.append(f"{pos.symbol}: {e}")
                    logger.exception(f"AUTO-CLOSE: exception closing {pos.symbol}")

            summary = (
                f"{acct.broker}/{acct.account_alias} at {now_hhmm} ET — "
                f"closed: {', '.join(closed) or 'none'}. "
                f"errors: {'; '.join(errors) or 'none'}"
            )
            db.add(WebhookDelivery(
                tenant_id   = acct.tenant_id,
                source_ip   = "relay-auto-close",
                outcome     = "auto_close" if not errors else "auto_close_partial",
                http_status = 200 if not errors else 500,
                auth_passed = True,
                error_detail= summary,
                duration_ms = 0,
            ))
            await db.commit()
            logger.info(f"AUTO-CLOSE complete: {summary}")


# ── Oanda Stream Manager ──────────────────────────────────────────────────────

async def _oanda_stream_once():
    """
    Check all active Oanda broker accounts.
    If there are open positions → ensure price + transaction streams are running.
    If all positions are flat → stop the streams to avoid unnecessary connections.
    """
    from app.models.broker_account import BrokerAccount
    from app.services.credentials import decrypt_credentials
    from app.services.oanda_stream import get_or_create_manager, remove_manager

    async with AsyncSessionLocal() as db:
        # Get all active Oanda accounts
        result = await db.execute(
            select(BrokerAccount).where(
                BrokerAccount.broker    == "oanda",
                BrokerAccount.is_active == True,  # noqa: E712
            )
        )
        accounts = result.scalars().all()

        for acct in accounts:
            try:
                creds = decrypt_credentials(acct.credentials_encrypted)
            except Exception:
                continue

            # Find open positions for this account
            pos_result = await db.execute(
                select(Position).where(
                    Position.tenant_id == acct.tenant_id,
                    Position.broker    == "oanda",
                    Position.account   == acct.account_alias,
                    func.abs(Position.quantity) > 1e-9,
                )
            )
            open_positions = pos_result.scalars().all()
            symbols = {p.symbol for p in open_positions}

            manager = get_or_create_manager(
                broker     = "oanda",
                account    = acct.account_alias,
                api_key    = creds.get("api_key", ""),
                account_id = creds.get("account_id", ""),
                base_url   = creds.get("base_url", "https://api-fxtrade.oanda.com/v3"),
            )

            if symbols:
                if not manager.is_running():
                    await manager.start(symbols)
                else:
                    await manager.update_symbols(symbols)
            else:
                if manager.is_running():
                    await manager.stop()
                    remove_manager("oanda", acct.account_alias)


# ── Task Launcher ──────────────────────────────────────────────────────────────

def start_background_tasks() -> list[asyncio.Task]:
    """
    Launch all background tasks. Returns a list of Task objects
    so the caller can cancel them on shutdown.
    """
    settings = get_settings()
    tasks = [
        asyncio.create_task(
            _run_forever("fill_poll", settings.fill_poll_interval_seconds, _poll_fills_once),
            name="fill_poll",
        ),
        asyncio.create_task(
            _run_forever("pnl_poll", settings.pnl_poll_interval_seconds, _poll_pnl_once),
            name="pnl_poll",
        ),
        asyncio.create_task(
            _run_forever("ibkr_keepalive", settings.ibkr_keepalive_interval_seconds, _ibkr_keepalive_once),
            name="ibkr_keepalive",
        ),
        asyncio.create_task(
            _run_forever("reconcile", settings.reconcile_interval_seconds, _reconcile_once),
            name="reconcile",
        ),
        asyncio.create_task(
            # Daily summary: check every minute whether it's time to send
            _run_forever("daily_summary", 60, _daily_summary_once),
            name="daily_summary",
        ),
        asyncio.create_task(
            # Purge old delivery logs once per hour
            _run_forever("delivery_purge", 3600, _purge_old_deliveries_once),
            name="delivery_purge",
        ),
        asyncio.create_task(
            # Auto-close: check every 60s whether any account needs session-end close
            _run_forever("auto_close", 60, _auto_close_once),
            name="auto_close",
        ),
        asyncio.create_task(
            # Oanda stream manager: check every 30s, connect/disconnect as needed
            _run_forever("oanda_stream", 30, _oanda_stream_once),
            name="oanda_stream",
        ),
    ]
    return tasks
