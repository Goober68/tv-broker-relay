"""
Unified P&L Engine — single source of truth for all realized P&L.

Processes ALL fills (webhook, Report API sync, CSV import) through FIFO
matching and maintains precomputed state in the account_pnl_state and
daily_pnl tables.

Runs as two background tasks:
  - pnl_engine (every 15s): incremental processing of new fills
  - pnl_reconcile (every hour): full recalculation to catch drift
"""
import asyncio
import json
import logging
from collections import deque
from datetime import datetime, timezone, timedelta, date

from sqlalchemy import select, text, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import AsyncSessionLocal
from app.models.order import Order, DEFAULT_FUTURES_MULTIPLIERS
from app.models.broker_account import BrokerAccount
from app.models.position import Position
from app.config import get_settings
from app.services.utils import futures_root, trading_day, build_commission_lookup, get_commission

logger = logging.getLogger(__name__)


def _build_account_commission(broker_account: BrokerAccount) -> tuple[dict, float]:
    """Build commission lookup for a broker account."""
    return build_commission_lookup(
        broker_account.instrument_map,
        broker_account.commission_per_contract or 0.0,
        broker=broker_account.broker,
        account_type=broker_account.account_type,
    )


def _serialize_lots(open_lots: dict[str, deque]) -> dict:
    """Serialize open lots to JSON-safe dict."""
    result = {}
    for sym, lots in open_lots.items():
        result[sym] = [
            {"qty": q, "price": p, "mult": m, "comm": c}
            for q, p, m, c in lots
        ]
    return result


def _deserialize_lots(data: dict | list | None) -> dict[str, deque]:
    """Deserialize open lots from JSON."""
    if not data or isinstance(data, list):
        return {}
    result = {}
    for sym, lots in data.items():
        result[sym] = deque(
            (l["qty"], l["price"], l["mult"], l.get("comm"))
            for l in lots
        )
    return result


async def process_new_fills(db: AsyncSession, state_row, broker_account: BrokerAccount) -> int:
    """
    Process fills with id > last_processed_order_id through FIFO matching.
    Updates state_row and daily_pnl table. Returns number of realized events.
    """
    comm_lookup, default_comm = _build_account_commission(broker_account)
    open_lots = _deserialize_lots(state_row.open_lots)

    # Fetch new fills
    result = await db.execute(
        text("""
            SELECT id, created_at, LOWER(action) as action, avg_fill_price,
                   filled_quantity, multiplier, symbol, commission
            FROM orders
            WHERE tenant_id = :tenant_id
              AND broker    = :broker
              AND account   = :account
              AND id > :last_id
              AND status IN ('filled', 'FILLED')
              AND avg_fill_price IS NOT NULL
              AND filled_quantity > 0
            ORDER BY created_at ASC, id ASC
        """),
        {
            "tenant_id": str(broker_account.tenant_id),
            "broker": broker_account.broker,
            "account": broker_account.account_alias,
            "last_id": state_row.last_processed_order_id,
        },
    )
    fills = result.fetchall()

    if not fills:
        return 0

    realized_count = 0
    current_trading_day = trading_day(datetime.now(timezone.utc))

    # Check for trading day rollover
    if state_row.daily_pnl_trading_day and state_row.daily_pnl_trading_day != current_trading_day:
        state_row.daily_realized = 0.0
        state_row.hwm_daily = 0.0
        state_row.daily_pnl_trading_day = current_trading_day

    for fill in fills:
        order_id = fill.id
        ts = fill.created_at
        action = fill.action
        price = float(fill.avg_fill_price)
        qty = float(fill.filled_quantity)
        mult = float(fill.multiplier)
        sym = fill.symbol or ""
        fill_comm = float(fill.commission) if fill.commission is not None else None

        if not sym:
            state_row.last_processed_order_id = max(state_row.last_processed_order_id, order_id)
            continue

        if sym not in open_lots:
            open_lots[sym] = deque()
        lots = open_lots[sym]

        current_pos = sum(l[0] for l in lots)
        signed_qty = qty if action == "buy" else -qty

        if current_pos == 0 or (current_pos > 0 and signed_qty > 0) or (current_pos < 0 and signed_qty < 0):
            lots.append((signed_qty, price, mult, fill_comm))
        else:
            remaining = qty
            while remaining > 0 and lots:
                lot_qty, lot_price, lot_mult, lot_comm = lots[0]
                lot_abs = abs(lot_qty)
                match_qty = min(remaining, lot_abs)

                if lot_qty > 0:
                    pnl = (price - lot_price) * match_qty * lot_mult
                else:
                    pnl = (lot_price - price) * match_qty * lot_mult

                # Forex P&L conversion
                sym_clean = sym.replace("_", "").replace("/", "").upper()
                if len(sym_clean) == 6 and sym_clean[3:6] != "USD":
                    if price > 0:
                        pnl = pnl / price

                # Commission
                entry_comm = lot_comm if lot_comm is not None else get_commission(sym, comm_lookup, default_comm)
                exit_comm = fill_comm if fill_comm is not None else get_commission(sym, comm_lookup, default_comm)
                total_comm = (entry_comm + exit_comm) * match_qty
                if total_comm > 0:
                    pnl -= total_comm

                # Update state
                state_row.cumulative_realized += pnl
                if state_row.cumulative_realized > state_row.hwm_cumulative:
                    state_row.hwm_cumulative = state_row.cumulative_realized

                # Daily P&L
                fill_trading_day = trading_day(ts)
                if state_row.daily_pnl_trading_day is None or fill_trading_day != state_row.daily_pnl_trading_day:
                    state_row.daily_realized = pnl
                    state_row.hwm_daily = max(0.0, pnl)
                    state_row.daily_pnl_trading_day = fill_trading_day
                else:
                    state_row.daily_realized += pnl
                if state_row.daily_realized > state_row.hwm_daily:
                    state_row.hwm_daily = state_row.daily_realized

                # Upsert daily_pnl row
                await db.execute(
                    text("""
                        INSERT INTO daily_pnl (tenant_id, broker, account, trading_day, realized_pnl, trade_count, commission_total)
                        VALUES (:tid, :broker, :account, :day, :pnl, 1, :comm)
                        ON CONFLICT (tenant_id, broker, account, trading_day)
                        DO UPDATE SET
                            realized_pnl = daily_pnl.realized_pnl + :pnl,
                            trade_count = daily_pnl.trade_count + 1,
                            commission_total = daily_pnl.commission_total + :comm
                    """),
                    {
                        "tid": str(broker_account.tenant_id),
                        "broker": broker_account.broker,
                        "account": broker_account.account_alias,
                        "day": fill_trading_day,
                        "pnl": round(pnl, 6),
                        "comm": round(total_comm, 6),
                    },
                )

                realized_count += 1
                remaining -= match_qty
                if match_qty >= lot_abs:
                    lots.popleft()
                else:
                    new_lot_qty = lot_qty + match_qty if lot_qty < 0 else lot_qty - match_qty
                    lots[0] = (new_lot_qty, lot_price, lot_mult, lot_comm)

            if remaining > 0:
                lots.append((signed_qty / qty * remaining, price, mult, fill_comm))

        state_row.last_processed_order_id = max(state_row.last_processed_order_id, order_id)

    # Serialize lots and update timestamp
    state_row.open_lots = _serialize_lots(open_lots)
    state_row.updated_at = datetime.now(timezone.utc)

    return realized_count


async def full_recalculate(db: AsyncSession, broker_account: BrokerAccount) -> dict:
    """
    Walk ALL fills from scratch and rebuild state + daily_pnl.
    Returns {cumulative_realized, hwm_cumulative, daily_totals, drift}.
    """
    comm_lookup, default_comm = _build_account_commission(broker_account)

    result = await db.execute(
        text("""
            SELECT id, created_at, LOWER(action) as action, avg_fill_price,
                   filled_quantity, multiplier, symbol, commission
            FROM orders
            WHERE tenant_id = :tenant_id
              AND broker    = :broker
              AND account   = :account
              AND status IN ('filled', 'FILLED')
              AND avg_fill_price IS NOT NULL
              AND filled_quantity > 0
            ORDER BY created_at ASC, id ASC
        """),
        {
            "tenant_id": str(broker_account.tenant_id),
            "broker": broker_account.broker,
            "account": broker_account.account_alias,
        },
    )
    fills = result.fetchall()

    open_lots: dict[str, deque] = {}
    cumulative = 0.0
    hwm = 0.0
    daily_totals: dict[date, tuple[float, int, float]] = {}  # day -> (pnl, count, comm)
    max_order_id = 0

    for fill in fills:
        max_order_id = max(max_order_id, fill.id)
        action = fill.action
        price = float(fill.avg_fill_price)
        qty = float(fill.filled_quantity)
        mult = float(fill.multiplier)
        sym = fill.symbol or ""
        fill_comm = float(fill.commission) if fill.commission is not None else None

        if not sym:
            continue

        if sym not in open_lots:
            open_lots[sym] = deque()
        lots = open_lots[sym]

        current_pos = sum(l[0] for l in lots)
        signed_qty = qty if action == "buy" else -qty

        if current_pos == 0 or (current_pos > 0 and signed_qty > 0) or (current_pos < 0 and signed_qty < 0):
            lots.append((signed_qty, price, mult, fill_comm))
        else:
            remaining = qty
            while remaining > 0 and lots:
                lot_qty, lot_price, lot_mult, lot_comm = lots[0]
                lot_abs = abs(lot_qty)
                match_qty = min(remaining, lot_abs)

                if lot_qty > 0:
                    pnl = (price - lot_price) * match_qty * lot_mult
                else:
                    pnl = (lot_price - price) * match_qty * lot_mult

                sym_clean = sym.replace("_", "").replace("/", "").upper()
                if len(sym_clean) == 6 and sym_clean[3:6] != "USD":
                    if price > 0:
                        pnl = pnl / price

                entry_comm = lot_comm if lot_comm is not None else get_commission(sym, comm_lookup, default_comm)
                exit_comm = fill_comm if fill_comm is not None else get_commission(sym, comm_lookup, default_comm)
                total_comm = (entry_comm + exit_comm) * match_qty
                if total_comm > 0:
                    pnl -= total_comm

                cumulative += pnl
                if cumulative > hwm:
                    hwm = cumulative

                td = trading_day(fill.created_at)
                existing = daily_totals.get(td, (0.0, 0, 0.0))
                daily_totals[td] = (existing[0] + pnl, existing[1] + 1, existing[2] + total_comm)

                remaining -= match_qty
                if match_qty >= lot_abs:
                    lots.popleft()
                else:
                    new_lot_qty = lot_qty + match_qty if lot_qty < 0 else lot_qty - match_qty
                    lots[0] = (new_lot_qty, lot_price, lot_mult, lot_comm)

            if remaining > 0:
                lots.append((signed_qty / qty * remaining, price, mult, fill_comm))

    return {
        "cumulative_realized": cumulative,
        "hwm_cumulative": hwm,
        "daily_totals": daily_totals,
        "open_lots": open_lots,
        "max_order_id": max_order_id,
    }


# ── Background Tasks ──────────────────────────────────────────────────────────

async def _pnl_engine_once():
    """Incremental P&L processing — runs every 15s."""
    async with AsyncSessionLocal() as db:
        # Find all accounts that have fills
        acct_result = await db.execute(
            select(BrokerAccount).where(BrokerAccount.is_active == True)  # noqa: E712
        )
        accounts = acct_result.scalars().all()

        for acct in accounts:
            try:
                # Get or create state row
                state_result = await db.execute(
                    text("""
                        SELECT id, cumulative_realized, daily_realized, daily_pnl_trading_day,
                               hwm_cumulative, hwm_daily, open_lots, last_processed_order_id,
                               last_full_recalc_at, recalc_drift_detected, updated_at
                        FROM account_pnl_state
                        WHERE tenant_id = :tid AND broker = :broker AND account = :account
                    """),
                    {"tid": str(acct.tenant_id), "broker": acct.broker, "account": acct.account_alias},
                )
                row = state_result.fetchone()

                if row is None:
                    # Create initial state
                    await db.execute(
                        text("""
                            INSERT INTO account_pnl_state (tenant_id, broker, account, updated_at)
                            VALUES (:tid, :broker, :account, :now)
                        """),
                        {
                            "tid": str(acct.tenant_id), "broker": acct.broker,
                            "account": acct.account_alias, "now": datetime.now(timezone.utc),
                        },
                    )
                    await db.commit()
                    state_result = await db.execute(
                        text("""
                            SELECT id, cumulative_realized, daily_realized, daily_pnl_trading_day,
                                   hwm_cumulative, hwm_daily, open_lots, last_processed_order_id,
                                   last_full_recalc_at, recalc_drift_detected, updated_at
                            FROM account_pnl_state
                            WHERE tenant_id = :tid AND broker = :broker AND account = :account
                        """),
                        {"tid": str(acct.tenant_id), "broker": acct.broker, "account": acct.account_alias},
                    )
                    row = state_result.fetchone()

                # Use a mutable wrapper for the state
                class State:
                    pass
                state = State()
                state.id = row.id
                state.cumulative_realized = row.cumulative_realized
                state.daily_realized = row.daily_realized
                state.daily_pnl_trading_day = row.daily_pnl_trading_day
                state.hwm_cumulative = row.hwm_cumulative
                state.hwm_daily = row.hwm_daily
                state.open_lots = row.open_lots
                state.last_processed_order_id = row.last_processed_order_id
                state.updated_at = row.updated_at

                realized = await process_new_fills(db, state, acct)

                if realized > 0 or state.last_processed_order_id != row.last_processed_order_id:
                    # Write state back
                    await db.execute(
                        text("""
                            UPDATE account_pnl_state SET
                                cumulative_realized = :cum, daily_realized = :daily,
                                daily_pnl_trading_day = :day, hwm_cumulative = :hwm,
                                hwm_daily = :hwm_d, open_lots = :lots,
                                last_processed_order_id = :last_id, updated_at = :now
                            WHERE id = :id
                        """),
                        {
                            "cum": round(state.cumulative_realized, 6),
                            "daily": round(state.daily_realized, 6),
                            "day": state.daily_pnl_trading_day,
                            "hwm": round(state.hwm_cumulative, 6),
                            "hwm_d": round(state.hwm_daily, 6),
                            "lots": json.dumps(state.open_lots if isinstance(state.open_lots, dict) else {}),
                            "last_id": state.last_processed_order_id,
                            "now": datetime.now(timezone.utc),
                            "id": state.id,
                        },
                    )
                    await db.commit()

                    if realized > 0:
                        logger.info(
                            f"P&L engine: {realized} events for {acct.broker}/{acct.account_alias} "
                            f"cum={state.cumulative_realized:.2f} daily={state.daily_realized:.2f}"
                        )

                # Handle trading day rollover (no new fills but day changed)
                current_td = trading_day(datetime.now(timezone.utc))
                if state.daily_pnl_trading_day and state.daily_pnl_trading_day != current_td:
                    await db.execute(
                        text("""
                            UPDATE account_pnl_state SET
                                daily_realized = 0.0, hwm_daily = 0.0,
                                daily_pnl_trading_day = :day, updated_at = :now
                            WHERE id = :id
                        """),
                        {"day": current_td, "now": datetime.now(timezone.utc), "id": state.id},
                    )
                    await db.commit()

            except Exception:
                logger.exception(f"P&L engine error for {acct.broker}/{acct.account_alias}")


async def _pnl_reconcile_once():
    """Full P&L recalculation — runs every hour. Detects and corrects drift."""
    async with AsyncSessionLocal() as db:
        acct_result = await db.execute(
            select(BrokerAccount).where(BrokerAccount.is_active == True)  # noqa: E712
        )
        accounts = acct_result.scalars().all()

        for acct in accounts:
            try:
                recalc = await full_recalculate(db, acct)
                cum = recalc["cumulative_realized"]
                hwm = recalc["hwm_cumulative"]
                max_id = recalc["max_order_id"]

                # Compare with incremental state
                state_result = await db.execute(
                    text("""
                        SELECT id, cumulative_realized, hwm_cumulative
                        FROM account_pnl_state
                        WHERE tenant_id = :tid AND broker = :broker AND account = :account
                    """),
                    {"tid": str(acct.tenant_id), "broker": acct.broker, "account": acct.account_alias},
                )
                row = state_result.fetchone()

                if row is None:
                    continue

                drift = abs(cum - row.cumulative_realized)
                if drift > 0.01:
                    logger.warning(
                        f"P&L reconcile: drift detected for {acct.broker}/{acct.account_alias}: "
                        f"incremental={row.cumulative_realized:.2f} recalc={cum:.2f} drift={drift:.2f}"
                    )

                    # Determine current trading day's P&L from daily_totals
                    current_td = trading_day(datetime.now(timezone.utc))
                    daily_data = recalc["daily_totals"].get(current_td, (0.0, 0, 0.0))

                    # Rebuild daily_pnl table for this account
                    await db.execute(
                        text("DELETE FROM daily_pnl WHERE tenant_id = :tid AND broker = :broker AND account = :account"),
                        {"tid": str(acct.tenant_id), "broker": acct.broker, "account": acct.account_alias},
                    )
                    for td, (pnl, count, comm) in recalc["daily_totals"].items():
                        await db.execute(
                            text("""
                                INSERT INTO daily_pnl (tenant_id, broker, account, trading_day, realized_pnl, trade_count, commission_total)
                                VALUES (:tid, :broker, :account, :day, :pnl, :count, :comm)
                            """),
                            {
                                "tid": str(acct.tenant_id), "broker": acct.broker,
                                "account": acct.account_alias, "day": td,
                                "pnl": round(pnl, 6), "count": count, "comm": round(comm, 6),
                            },
                        )

                    # Overwrite incremental state
                    await db.execute(
                        text("""
                            UPDATE account_pnl_state SET
                                cumulative_realized = :cum, hwm_cumulative = :hwm,
                                daily_realized = :daily, daily_pnl_trading_day = :day,
                                open_lots = :lots, last_processed_order_id = :last_id,
                                last_full_recalc_at = :now, recalc_drift_detected = TRUE,
                                updated_at = :now
                            WHERE id = :id
                        """),
                        {
                            "cum": round(cum, 6), "hwm": round(hwm, 6),
                            "daily": round(daily_data[0], 6), "day": current_td,
                            "lots": json.dumps(_serialize_lots(recalc["open_lots"])),
                            "last_id": max_id,
                            "now": datetime.now(timezone.utc),
                            "id": row.id,
                        },
                    )
                else:
                    # No drift — just update recalc timestamp
                    await db.execute(
                        text("""
                            UPDATE account_pnl_state SET
                                last_full_recalc_at = :now, recalc_drift_detected = FALSE
                            WHERE id = :id
                        """),
                        {"now": datetime.now(timezone.utc), "id": row.id},
                    )

                await db.commit()

            except Exception:
                logger.exception(f"P&L reconcile error for {acct.broker}/{acct.account_alias}")
