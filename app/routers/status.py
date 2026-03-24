from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from app.models.db import get_db
from app.models.order import Order, OrderStatus
from app.models.position import Position
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
    quantity: float
    avg_price: float
    realized_pnl: float
    daily_realized_pnl: float
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
        .where(Order.tenant_id == tenant.id, Order.status == OrderStatus.OPEN)
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
