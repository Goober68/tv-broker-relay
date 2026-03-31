from fastapi import APIRouter, Depends, Query, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func
import re
from app.models.db import get_db
from app.models.order import Order, OrderStatus
from app.models.position import Position
from app.models.broker_account import BrokerAccount
from app.models.tenant import Tenant
from app.dependencies.auth import get_current_tenant

_FUTURES_CONTRACT_RE = re.compile(r'^(.+?)[FGHJKMNQUVXZ]\d{1,2}$')

def _futures_root(contract: str) -> str:
    m = _FUTURES_CONTRACT_RE.match(contract)
    if m:
        return m.group(1)
    return ''.join(c for c in contract if c.isalpha())
from pydantic import BaseModel
from datetime import datetime

router = APIRouter(prefix="/api")


class PositionOut(BaseModel):
    id: int
    broker: str
    account: str
    symbol: str
    instrument_type: str
    quantity: float
    avg_price: float
    multiplier: float
    realized_pnl: float
    daily_realized_pnl: float
    # Live P&L fields — populated by pnl_poll background task
    last_price: float | None = None
    unrealized_pnl: float | None = None
    last_price_at: datetime | None = None
    updated_at: datetime

    class Config:
        from_attributes = True


class OrderOut(BaseModel):
    id: int
    created_at: datetime
    broker: str
    account: str
    symbol: str
    instrument_type: str
    exchange: str | None
    currency: str | None
    action: str
    order_type: str
    quantity: float
    price: float | None
    time_in_force: str
    expire_at: datetime | None
    multiplier: float
    extended_hours: bool
    option_expiry: str | None
    option_strike: float | None
    option_right: str | None
    option_multiplier: float
    stop_loss: float | None
    take_profit: float | None
    trailing_distance: float | None
    status: str
    broker_order_id:  str | None
    client_trade_id:  str | None = None
    broker_quantity:  float | None = None
    broker_request:  str | None = None
    broker_response: str | None = None
    filled_quantity: float
    avg_fill_price: float | None
    algo_id: str | None = None
    algo_version: str | None = None
    comment: str | None
    error_message: str | None

    class Config:
        from_attributes = True


@router.get("/positions", response_model=list[PositionOut])
async def list_positions(
    broker: str | None = Query(None),
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Position).where(Position.tenant_id == tenant.id)
    if broker:
        stmt = stmt.where(Position.broker == broker)
    result = await db.execute(stmt)
    return result.scalars().all()


@router.get("/positions/{broker}/{account}/{symbol}", response_model=PositionOut)
async def get_position(
    broker: str,
    account: str,
    symbol: str,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Position).where(
            Position.tenant_id == tenant.id,
            Position.broker == broker,
            Position.account == account,
            Position.symbol == symbol.upper(),
        )
    )
    pos = result.scalar_one_or_none()
    if pos is None:
        raise HTTPException(status_code=404, detail="Position not found")
    return pos


@router.get("/orders/open", response_model=list[OrderOut])
async def list_open_orders(
    broker: str | None = Query(None),
    symbol: str | None = Query(None),
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(Order)
        .where(Order.tenant_id == tenant.id, Order.status == "open")
        .order_by(desc(Order.created_at))
    )
    if broker:
        stmt = stmt.where(Order.broker == broker)
    if symbol:
        stmt = stmt.where(Order.symbol == symbol.upper())
    result = await db.execute(stmt)
    return result.scalars().all()


@router.get("/orders", response_model=list[OrderOut])
async def list_orders(
    broker: str | None = Query(None),
    symbol: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(50, le=500),
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(Order)
        .where(Order.tenant_id == tenant.id)
        .order_by(desc(Order.created_at))
        .limit(limit)
    )
    if broker:
        stmt = stmt.where(Order.broker == broker)
    if symbol:
        stmt = stmt.where(Order.symbol == symbol.upper())
    if status:
        stmt = stmt.where(Order.status == status)
    result = await db.execute(stmt)
    return result.scalars().all()


# ── Webhook Delivery Log ───────────────────────────────────────────────────────

class DeliveryOut(BaseModel):
    id: int
    created_at: datetime
    source_ip: str | None
    outcome: str
    http_status: int
    auth_passed: bool
    order_id: int | None
    error_detail: str | None
    duration_ms: float | None
    raw_payload: str | None
    user_agent: str | None = None
    broker_request:  str | None = None   # outbound JSON sent to broker (from joined order row)
    broker_response: str | None = None   # response body received from broker
    account_display_name: str | None = None
    algo: str | None = None

    class Config:
        from_attributes = False  # manual construction — not direct ORM mapping


@router.get("/webhook-deliveries", response_model=list[DeliveryOut])
async def list_webhook_deliveries(
    outcome: str | None = Query(None),
    limit: int = Query(25, le=500),
    offset: int = Query(0, ge=0),
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Recent webhook delivery log — useful for debugging TradingView alerts."""
    from app.models.webhook_delivery import WebhookDelivery
    from sqlalchemy.orm import outerjoin
    stmt = (
        select(WebhookDelivery, Order.broker_request, Order.broker_response, Order.error_message)
        .outerjoin(Order, Order.id == WebhookDelivery.order_id)
        .where(WebhookDelivery.tenant_id == tenant.id)
        .order_by(desc(WebhookDelivery.created_at))
        .limit(limit)
        .offset(offset)
    )
    if outcome:
        stmt = stmt.where(WebhookDelivery.outcome == outcome)
    result = await db.execute(stmt)
    rows = result.all()

    # Build account alias → display name lookup
    acct_result = await db.execute(
        select(BrokerAccount.account_alias, BrokerAccount.display_name).where(
            BrokerAccount.tenant_id == tenant.id,
        )
    )
    display_names = {r[0]: r[1] for r in acct_result.fetchall() if r[1]}

    import json as _json
    def _parse_payload(raw_payload: str | None) -> dict:
        if not raw_payload:
            return {}
        try:
            return _json.loads(raw_payload)
        except Exception:
            return {}

    def _get_display_name(p: dict) -> str | None:
        alias = p.get("account", "primary")
        return display_names.get(alias)

    def _get_algo(p: dict) -> str | None:
        algo_id = p.get("algo_id")
        if not algo_id:
            return None
        algo_ver = p.get("algo_version")
        return f"{algo_id}.{algo_ver}" if algo_ver else algo_id

    result = []
    for d, broker_request, broker_response, order_error in rows:
        p = _parse_payload(d.raw_payload)
        result.append(DeliveryOut(
            id=d.id,
            created_at=d.created_at,
            source_ip=d.source_ip,
            outcome=d.outcome,
            http_status=d.http_status,
            auth_passed=d.auth_passed,
            order_id=d.order_id,
            error_detail=d.error_detail or order_error,
            duration_ms=d.duration_ms,
            raw_payload=d.raw_payload,
            user_agent=d.user_agent,
            broker_request=broker_request,
            broker_response=broker_response,
            account_display_name=_get_display_name(p),
            algo=_get_algo(p),
        ))
    return result



# ── Server-Sent Events ─────────────────────────────────────────────────────────

@router.get("/events")
async def sse_events(
    request: Request,
    token: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Server-Sent Events stream for real-time delivery notifications.
    Emits a 'delivery' event whenever a webhook is processed.
    Sends a heartbeat comment every 15s to keep the connection alive.

    The JWT is validated once at connect time via the standard Bearer header.
    EventSource in the browser cannot set headers, so the frontend passes the
    token as a query parameter: /api/events?token=<access_token>
    """
    # Authenticate — EventSource cannot send headers so token comes as query param
    import uuid as _uuid
    from jose import JWTError
    from app.services.auth import decode_access_token, get_tenant_by_id
    from fastapi import HTTPException as _HTTPException
    if not token:
        raise _HTTPException(status_code=401, detail="Missing token")
    try:
        payload = decode_access_token(token)
        tenant_id = _uuid.UUID(payload["sub"])
    except (JWTError, KeyError, ValueError):
        raise _HTTPException(status_code=401, detail="Invalid token")
    tenant = await get_tenant_by_id(db, tenant_id)
    if tenant is None or not tenant.is_active:
        raise _HTTPException(status_code=401, detail="Invalid token")

    from app.services.events import event_stream
    return StreamingResponse(
        event_stream(tenant.id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # tell Nginx/Caddy not to buffer the stream
            "Connection": "keep-alive",
        },
    )


# ── Position Sync ──────────────────────────────────────────────────────────────

class SyncResult(BaseModel):
    broker: str
    account: str
    created: list[str]   # symbols newly inserted
    updated: list[str]   # symbols whose quantity was updated
    skipped: list[str]   # symbols with zero position on broker (ignored)
    errors:  list[str]   # any error messages


class SyncResponse(BaseModel):
    results: list[SyncResult]
    total_created: int
    total_updated: int


@router.post("/positions/sync", response_model=SyncResponse)
async def sync_positions(
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    Pull current open positions from every active broker account and upsert
    them into the relay's position table.

    Use this after:
      - Restoring the database
      - Manually opening a position outside the relay
      - Any time the relay's position state gets out of sync with the broker

    For each position returned by the broker:
      - If a relay position row exists → update quantity, avg_price, last_price,
        unrealized_pnl, last_price_at
      - If no row exists → create one (realized_pnl starts at 0)

    Positions the broker shows as flat (quantity = 0) are skipped.
    Positions the relay tracks but the broker no longer has are left untouched
    (they may have been closed outside the relay — use DELETE /api/positions/{id}
    to remove them manually if needed).
    """
    from app.brokers.registry import get_broker_for_tenant
    from app.models.order import DEFAULT_FUTURES_MULTIPLIERS
    from datetime import timezone

    # Load all active broker accounts for this tenant
    result = await db.execute(
        select(BrokerAccount).where(
            BrokerAccount.tenant_id == tenant.id,
            BrokerAccount.is_active == True,  # noqa: E712
        )
    )
    accounts = result.scalars().all()

    if not accounts:
        raise HTTPException(
            status_code=422,
            detail="No active broker accounts found. Add one via POST /broker-accounts first."
        )

    all_results = []
    now = datetime.now(timezone.utc)

    for acct in accounts:
        created = []
        updated = []
        skipped = []
        errors  = []

        try:
            broker = await get_broker_for_tenant(
                acct.broker, acct.account_alias, tenant.id, db
            )
            pnl_data = await broker.get_open_positions_pnl(acct.account_alias)
        except Exception as e:
            errors.append(str(e))
            all_results.append(SyncResult(
                broker=acct.broker, account=acct.account_alias,
                created=[], updated=[], skipped=[], errors=errors,
            ))
            continue

        for item in pnl_data:
            symbol        = item.get("symbol", "")
            last_price    = item.get("last_price")
            unrealized    = item.get("unrealized_pnl")

            # Determine quantity — get_open_positions_pnl doesn't return qty directly,
            # so we fetch it from get_position()
            try:
                qty = await broker.get_position(acct.account_alias, symbol)
            except Exception as e:
                errors.append(f"{symbol}: could not fetch quantity — {e}")
                continue

            if abs(qty) < 1e-9:
                skipped.append(symbol)
                continue

            # Determine instrument type from broker
            instrument_type = _infer_instrument_type(acct.broker, symbol)

            # Resolve multiplier
            root = _futures_root(symbol)
            multiplier = (
                DEFAULT_FUTURES_MULTIPLIERS.get(symbol)
                or DEFAULT_FUTURES_MULTIPLIERS.get(root, 1.0)
            )
            # Check instrument_map override
            if acct.instrument_map:
                instr = acct.instrument_map.get(symbol) or acct.instrument_map.get(root)
                if instr and instr.get("multiplier"):
                    multiplier = float(instr["multiplier"])

            # Look for existing position row
            existing = await db.execute(
                select(Position).where(
                    Position.tenant_id == tenant.id,
                    Position.broker    == acct.broker,
                    Position.account   == acct.account_alias,
                    Position.symbol    == symbol,
                )
            )
            pos = existing.scalar_one_or_none()

            if pos is not None:
                # Update existing row
                pos.quantity        = qty
                pos.multiplier      = multiplier
                pos.instrument_type = instrument_type
                pos.last_price      = last_price
                pos.unrealized_pnl  = unrealized
                pos.last_price_at   = now
                pos.updated_at      = now
                updated.append(symbol)
            else:
                # Create new row — avg_price unknown (0.0), realized P&L starts at 0
                pos = Position(
                    tenant_id       = tenant.id,
                    broker          = acct.broker,
                    account         = acct.account_alias,
                    symbol          = symbol,
                    instrument_type = instrument_type,
                    quantity        = qty,
                    avg_price       = 0.0,   # unknown — opened outside relay
                    multiplier      = multiplier,
                    realized_pnl    = 0.0,
                    daily_realized_pnl = 0.0,
                    last_price      = last_price,
                    unrealized_pnl  = unrealized,
                    last_price_at   = now,
                )
                db.add(pos)
                created.append(symbol)

        all_results.append(SyncResult(
            broker  = acct.broker,
            account = acct.account_alias,
            created = created,
            updated = updated,
            skipped = skipped,
            errors  = errors,
        ))

    await db.commit()

    return SyncResponse(
        results       = all_results,
        total_created = sum(len(r.created) for r in all_results),
        total_updated = sum(len(r.updated) for r in all_results),
    )


@router.delete("/positions/{position_id}", status_code=204)
async def delete_position(
    position_id: int,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    Remove a stale position row that the broker no longer has.
    Only affects the relay's internal state — does not send any order to the broker.
    """
    result = await db.execute(
        select(Position).where(
            Position.id        == position_id,
            Position.tenant_id == tenant.id,
        )
    )
    pos = result.scalar_one_or_none()
    if pos is None:
        raise HTTPException(status_code=404, detail="Position not found")
    await db.delete(pos)
    await db.commit()


def _infer_instrument_type(broker: str, symbol: str) -> str:
    """Best-effort instrument type from broker + symbol."""
    if broker == "tradovate":
        return "future"
    if broker == "etrade":
        return "equity"
    if broker == "oanda":
        # Oanda symbols: forex = "EUR_USD", CFD = "BCO_USD" (commodities), "DE30_EUR" (indices)
        # Forex pairs are always 3-letter_3-letter currency codes
        parts = symbol.split("_")
        if (len(parts) == 2
                and len(parts[0]) == 3
                and len(parts[1]) == 3
                and parts[0].isalpha()
                and parts[1].isalpha()):
            return "forex"
        return "cfd"
    if broker == "ibkr":
        return "equity"   # best guess — instrument_map has the real type
    return "forex"


# ── P&L Dashboard (unified engine) ────────────────────────────────────────────

class DashboardPnl(BaseModel):
    daily_realized: float
    total_realized: float
    total_unrealized: float
    open_positions: int


@router.get("/pnl/dashboard", response_model=DashboardPnl)
async def get_pnl_dashboard(
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Precomputed P&L totals from the unified engine."""
    from sqlalchemy import text as sql_text

    # Sum across all accounts from the P&L engine state
    result = await db.execute(
        sql_text("""
            SELECT COALESCE(SUM(daily_realized), 0) as daily,
                   COALESCE(SUM(cumulative_realized), 0) as total
            FROM account_pnl_state
            WHERE tenant_id = :tid
        """),
        {"tid": str(tenant.id)},
    )
    row = result.fetchone()

    # Unrealized from positions table (populated by broker poll / stream)
    unreal_result = await db.execute(
        select(func.coalesce(func.sum(Position.unrealized_pnl), 0)).where(
            Position.tenant_id == tenant.id,
            func.abs(Position.quantity) > 1e-9,
        )
    )
    total_unrealized = float(unreal_result.scalar_one())

    # Open position count
    pos_count = await db.execute(
        select(func.count(Position.id)).where(
            Position.tenant_id == tenant.id,
            func.abs(Position.quantity) > 1e-9,
        )
    )

    return DashboardPnl(
        daily_realized=round(row.daily, 2),
        total_realized=round(row.total, 2),
        total_unrealized=round(total_unrealized, 2),
        open_positions=pos_count.scalar_one(),
    )


# ── P&L Summary ────────────────────────────────────────────────────────────────

class PnlBar(BaseModel):
    period_start: datetime
    realized_pnl: float
    unrealized_pnl: float
    cumulative_realized: float
    cumulative_total: float
    order_count: int


class AccountPnlSummary(BaseModel):
    broker: str
    account: str
    display_name: str | None
    bars: list[PnlBar]
    # Account balance
    balance: float | None = None
    commission_per_contract: float | None = None
    # Drawdown tracking
    max_total_drawdown: float | None = None   # account limit
    max_daily_drawdown: float | None = None   # account limit
    current_drawdown: float = 0.0             # from HWM of cumulative realized
    drawdown_remaining: float | None = None   # max_total_drawdown - current_drawdown
    today_drawdown: float = 0.0               # from today's HWM


@router.get("/pnl/summary", response_model=list[AccountPnlSummary])
async def get_pnl_summary(
    period: str = Query("daily", pattern="^(15min|daily|weekly|monthly|yearly)$"),
    start: str | None = Query(None, description="Custom range start (ISO date, e.g. 2026-01-01)"),
    end: str | None = Query(None, description="Custom range end (ISO date, e.g. 2026-03-28)"),
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    Return P&L bucketed by time period for each active broker account.

    period: "15min" | "daily" | "weekly" | "monthly" | "yearly"
    start/end: optional custom date range (overrides default lookback)
    """
    from datetime import timezone, timedelta
    from zoneinfo import ZoneInfo
    from sqlalchemy import case, cast, Float

    ET = ZoneInfo("America/New_York")
    now = datetime.now(timezone.utc)

    def _trading_day(ts):
        """Convert a UTC timestamp to its futures trading day.
        Trading day rolls at 5pm ET — a fill at 4:59pm ET belongs to that
        calendar day; a fill at 5:01pm ET belongs to the next day."""
        ts_et = ts.astimezone(ET)
        if ts_et.hour >= 17:  # at or after 5pm ET → next trading day
            return (ts_et + timedelta(days=1)).date()
        return ts_et.date()

    # Determine lookback and truncation based on period
    if period == "15min":
        lookback  = now - timedelta(hours=24)
        trunc_sql = "date_trunc('hour', created_at) + INTERVAL '15 min' * FLOOR(EXTRACT(MINUTE FROM created_at) / 15)"
    elif period == "daily":
        lookback  = now - timedelta(days=30)
        trunc_sql = "date_trunc('day', created_at)"
    elif period == "weekly":
        lookback  = now - timedelta(weeks=12)
        trunc_sql = "date_trunc('week', created_at)"
    elif period == "monthly":
        lookback  = now - timedelta(days=365)
        trunc_sql = "date_trunc('month', created_at)"
    else:  # yearly
        lookback  = now - timedelta(days=365 * 5)
        trunc_sql = "date_trunc('year', created_at)"

    # Custom date range overrides default lookback
    if start:
        try:
            from datetime import date as date_type
            lookback = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    # Get active broker accounts for this tenant
    acct_result = await db.execute(
        select(BrokerAccount).where(
            BrokerAccount.tenant_id == tenant.id,
            BrokerAccount.is_active == True,  # noqa: E712
        ).order_by(BrokerAccount.broker, BrokerAccount.account_alias)
    )
    accounts = acct_result.scalars().all()

    summaries = []

    for acct in accounts:
        # Fetch ALL filled orders for this account (not just within lookback)
        # so FIFO matching is correct even when entry was before the lookback window.
        from sqlalchemy import text
        from collections import deque

        all_fills = await db.execute(
            text("""
                SELECT created_at, LOWER(action) as action, avg_fill_price,
                       filled_quantity, multiplier, symbol, commission
                FROM orders
                WHERE tenant_id = :tenant_id
                  AND broker    = :broker
                  AND account   = :account
                  AND status IN ('filled', 'FILLED')
                  AND avg_fill_price IS NOT NULL
                  AND filled_quantity > 0
                ORDER BY created_at ASC
            """),
            {
                "tenant_id": str(tenant.id),
                "broker":    acct.broker,
                "account":   acct.account_alias,
            }
        )
        fills = all_fills.fetchall()

        # FIFO matching per symbol: track open lots, compute realized P&L on closes
        # realized_events = [(timestamp, pnl, symbol), ...]
        realized_events = []
        open_lots: dict[str, deque] = {}  # symbol → deque of (qty, price, multiplier)
        default_commission = acct.commission_per_contract or 0.0
        instrument_map = acct.instrument_map or {}

        # Build reverse lookup: target_symbol/root -> commission
        # instrument_map is keyed by TradingView symbol (e.g. "MNQ1!") but
        # orders use broker symbols (e.g. "MNQM6"). Match via target_symbol.
        _commission_by_target = {}
        for _key, _val in instrument_map.items():
            if isinstance(_val, dict) and "commission" in _val:
                comm = float(_val["commission"])
                _commission_by_target[_key] = comm
                ts = _val.get("target_symbol", "")
                if ts:
                    _commission_by_target[ts] = comm
                    _commission_by_target[_futures_root(ts)] = comm
                _commission_by_target[_futures_root(_key)] = comm

        def _get_commission(symbol: str) -> float:
            """Look up per-product commission, fall back to account default."""
            # Try full symbol (e.g. MNQM6)
            if symbol in _commission_by_target:
                return _commission_by_target[symbol]
            # Try root symbol (e.g. MNQ)
            root = _futures_root(symbol)
            if root in _commission_by_target:
                return _commission_by_target[root]
            return default_commission

        for fill in fills:
            ts = fill.created_at
            action = fill.action
            price = float(fill.avg_fill_price)
            qty = float(fill.filled_quantity)
            mult = float(fill.multiplier)
            sym = fill.symbol
            # Per-fill commission from broker (if available)
            fill_comm = float(fill.commission) if fill.commission is not None else None

            if sym not in open_lots:
                open_lots[sym] = deque()
            lots = open_lots[sym]

            # Determine if this trade opens or closes
            # Position direction: positive = long, negative = short
            current_pos = sum(l[0] for l in lots)
            signed_qty = qty if action == "buy" else -qty

            # Same direction as current position (or opening from flat) → add lot
            # Lots store: (signed_qty, price, multiplier, commission_per_contract)
            if current_pos == 0 or (current_pos > 0 and signed_qty > 0) or (current_pos < 0 and signed_qty < 0):
                lots.append((signed_qty, price, mult, fill_comm))
            else:
                # Opposite direction → close lots FIFO
                remaining = qty
                while remaining > 0 and lots:
                    lot_qty, lot_price, lot_mult, lot_comm = lots[0]
                    lot_abs = abs(lot_qty)
                    match_qty = min(remaining, lot_abs)

                    # P&L = (exit - entry) * qty * multiplier * direction
                    if lot_qty > 0:
                        # Closing a long: sold at price, bought at lot_price
                        pnl = (price - lot_price) * match_qty * lot_mult
                    else:
                        # Closing a short: bought at price, sold at lot_price
                        pnl = (lot_price - price) * match_qty * lot_mult

                    # Forex P&L conversion: if the quote currency isn't USD,
                    # convert P&L to USD using the close price as the rate.
                    # e.g. USD_JPY: P&L is in JPY, divide by rate to get USD
                    # e.g. EUR_USD: P&L is already in USD, no conversion needed
                    sym_clean = sym.replace("_", "").replace("/", "").upper()
                    if len(sym_clean) == 6 and sym_clean[3:6] != "USD":
                        if price > 0:
                            pnl = pnl / price

                    # Deduct round-trip commission (entry + exit side)
                    # Prefer per-fill commission from broker, fall back to config
                    entry_comm = lot_comm if lot_comm is not None else _get_commission(sym)
                    exit_comm = fill_comm if fill_comm is not None else _get_commission(sym)
                    total_comm = (entry_comm + exit_comm) * match_qty
                    if total_comm > 0:
                        pnl -= total_comm

                    realized_events.append((ts, pnl))

                    remaining -= match_qty
                    if match_qty >= lot_abs:
                        lots.popleft()
                    else:
                        # Partially consumed lot
                        new_lot_qty = lot_qty + match_qty if lot_qty < 0 else lot_qty - match_qty
                        lots[0] = (new_lot_qty, lot_price, lot_mult, lot_comm)

                # If remaining > 0, this trade flipped the position — open new lot
                if remaining > 0:
                    lots.append((signed_qty / qty * remaining, price, mult, fill_comm))

        # Bucket realized P&L events by period (only events within lookback)
        period_pnl: dict[str, tuple[float, int]] = {}  # period_key → (pnl_sum, order_count)
        for ts, pnl in realized_events:
            if ts < lookback:
                continue
            # Truncate timestamp to period bucket
            if period == "15min":
                bucket = ts.replace(minute=(ts.minute // 15) * 15, second=0, microsecond=0)
            elif period == "daily":
                # Use futures trading day (rolls at 5pm ET)
                td = _trading_day(ts)
                bucket = datetime(td.year, td.month, td.day, tzinfo=timezone.utc)
            elif period == "weekly":
                # Truncate to Monday of the trading day's week
                td = _trading_day(ts)
                monday = td - timedelta(days=td.weekday())
                bucket = datetime(monday.year, monday.month, monday.day, tzinfo=timezone.utc)
            elif period == "monthly":
                td = _trading_day(ts)
                bucket = datetime(td.year, td.month, 1, tzinfo=timezone.utc)
            else:  # yearly
                td = _trading_day(ts)
                bucket = datetime(td.year, 1, 1, tzinfo=timezone.utc)

            key = bucket.isoformat()
            existing = period_pnl.get(key, (0.0, 0))
            period_pnl[key] = (existing[0] + pnl, existing[1] + 1)

        # Get current unrealized P&L for this account from positions table
        pos_result = await db.execute(
            select(Position).where(
                Position.tenant_id == tenant.id,
                Position.broker    == acct.broker,
                Position.account   == acct.account_alias,
                func.abs(Position.quantity) > 1e-9,
            )
        )
        open_positions  = pos_result.scalars().all()
        total_unrealized = sum(p.unrealized_pnl or 0 for p in open_positions)

        # Build bars with running cumulative
        bars = []
        cumulative = 0.0
        for key in sorted(period_pnl.keys()):
            pnl_val, count = period_pnl[key]
            cumulative += pnl_val
            bars.append(PnlBar(
                period_start        = datetime.fromisoformat(key),
                realized_pnl        = round(pnl_val, 2),
                unrealized_pnl      = 0.0,
                cumulative_realized = round(cumulative, 2),
                cumulative_total    = round(cumulative + total_unrealized, 2),
                order_count         = count,
            ))

        # Append unrealized to the last bar if we have open positions
        if bars and total_unrealized != 0:
            bars[-1] = bars[-1].model_copy(
                update={"unrealized_pnl": total_unrealized}
            )

        # Calculate drawdown from high-water mark of ALL-TIME realized P&L
        all_time_cumulative = 0.0
        hwm = 0.0
        today_cumulative = 0.0
        today_hwm = 0.0
        # Trading day starts at 5pm ET previous calendar day
        current_trading_day = _trading_day(now)
        today_start_et = datetime(
            current_trading_day.year, current_trading_day.month, current_trading_day.day,
            17, 0, 0, tzinfo=ET,
        ) - timedelta(days=1)
        today_start = today_start_et.astimezone(timezone.utc)

        for ts, pnl in realized_events:
            all_time_cumulative += pnl
            if all_time_cumulative > hwm:
                hwm = all_time_cumulative
            if ts >= today_start:
                today_cumulative += pnl
                if today_cumulative > today_hwm:
                    today_hwm = today_cumulative

        current_drawdown = round(hwm - all_time_cumulative, 2)
        today_drawdown = round(today_hwm - today_cumulative, 2)

        # Drawdown remaining: prefer balance-based calc if drawdown_floor is set
        if acct.drawdown_floor is not None and balance is not None:
            drawdown_remaining = round(balance - acct.drawdown_floor, 2)
        elif acct.max_total_drawdown:
            drawdown_remaining = round(acct.max_total_drawdown - current_drawdown, 2)
        else:
            drawdown_remaining = None

        # Fetch account balance from broker
        balance = None
        try:
            from app.brokers.registry import get_broker_for_tenant
            broker_adapter = await get_broker_for_tenant(
                acct.broker, acct.account_alias, tenant.id, db
            )
            balance = await broker_adapter.get_balance(acct.account_alias)
        except Exception:
            pass  # balance is optional — don't fail the summary

        summaries.append(AccountPnlSummary(
            broker       = acct.broker,
            account      = acct.account_alias,
            display_name = acct.display_name,
            bars         = bars,
            balance      = balance,
            commission_per_contract = acct.commission_per_contract,
            max_total_drawdown = acct.max_total_drawdown,
            max_daily_drawdown = acct.max_daily_drawdown,
            current_drawdown   = current_drawdown,
            drawdown_remaining = drawdown_remaining,
            today_drawdown     = today_drawdown,
        ))

    return summaries
