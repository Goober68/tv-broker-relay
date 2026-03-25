from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func
from app.models.db import get_db
from app.models.order import Order, OrderStatus
from app.models.position import Position
from app.models.broker_account import BrokerAccount
from app.models.tenant import Tenant
from app.dependencies.auth import get_current_tenant
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
    broker_order_id: str | None
    filled_quantity: float
    avg_fill_price: float | None
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
        .where(Order.tenant_id == tenant.id, Order.status == OrderStatus.OPEN.value)
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

    class Config:
        from_attributes = True


@router.get("/webhook-deliveries", response_model=list[DeliveryOut])
async def list_webhook_deliveries(
    outcome: str | None = Query(None),
    limit: int = Query(50, le=500),
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Recent webhook delivery log — useful for debugging TradingView alerts."""
    from app.models.webhook_delivery import WebhookDelivery
    stmt = (
        select(WebhookDelivery)
        .where(WebhookDelivery.tenant_id == tenant.id)
        .order_by(desc(WebhookDelivery.created_at))
        .limit(limit)
    )
    if outcome:
        stmt = stmt.where(WebhookDelivery.outcome == outcome)
    result = await db.execute(stmt)
    return result.scalars().all()


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
            root = ''.join(c for c in symbol if c.isalpha())
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
