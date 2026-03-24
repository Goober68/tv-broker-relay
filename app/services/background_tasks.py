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
            .where(Order.status == OrderStatus.OPEN)
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

        for pos in positions:
            try:
                broker = await get_broker_for_tenant(
                    pos.broker, pos.account, pos.tenant_id, db
                )
                broker_qty = await broker.get_position(pos.account, pos.symbol)
                internal_qty = pos.quantity

                if abs(internal_qty) < 1e-9 and abs(broker_qty) < 1e-9:
                    continue  # both flat

                if abs(broker_qty) < 1e-9 and abs(internal_qty) > 0:
                    # Broker shows flat but we show a position
                    logger.warning(
                        f"RECONCILIATION DRIFT — Position {pos.id} "
                        f"tenant={pos.tenant_id} {pos.broker}/{pos.symbol}: "
                        f"internal={internal_qty:.4f} broker=0 (broker shows flat!). "
                        f"Manual review required."
                    )
                    continue

                if abs(internal_qty) < 1e-9:
                    continue  # we show flat, ignore small broker remainder

                drift_pct = abs((broker_qty - internal_qty) / internal_qty) * 100
                if drift_pct > 1.0:
                    logger.warning(
                        f"RECONCILIATION DRIFT — Position {pos.id} "
                        f"tenant={pos.tenant_id} {pos.broker}/{pos.symbol}: "
                        f"internal={internal_qty:.4f} broker={broker_qty:.4f} "
                        f"drift={drift_pct:.1f}%. Manual review required."
                    )
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
            Order.status == OrderStatus.FILLED,
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
    ]
    return tasks
