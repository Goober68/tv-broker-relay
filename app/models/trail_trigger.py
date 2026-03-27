"""
TrailTrigger model — persists pending trailing stop triggers for Oanda positions.

When a webhook includes trail_trigger, a TrailTrigger row is created.
The Oanda price stream monitors each tick and fires the trailing stop
order when the trigger price is hit.
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Float, Boolean, DateTime, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID
from app.models.db import Base


class TrailTrigger(Base):
    __tablename__ = "trail_triggers"

    id:             Mapped[int]        = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at:     Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at:     Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # Ownership
    tenant_id:      Mapped[uuid.UUID]  = mapped_column(UUID(as_uuid=True), nullable=False)
    broker_account_id: Mapped[int]     = mapped_column(Integer, ForeignKey("broker_accounts.id"), nullable=False)
    order_id:       Mapped[int]        = mapped_column(Integer, ForeignKey("orders.id"), nullable=True)

    # Position context
    broker:         Mapped[str]        = mapped_column(String(32), nullable=False)
    account:        Mapped[str]        = mapped_column(String(128), nullable=False)
    symbol:         Mapped[str]        = mapped_column(String(32), nullable=False)
    direction:      Mapped[str]        = mapped_column(String(8), nullable=False)   # "buy" or "sell"

    # Trigger config — all in absolute price units
    trigger_price:  Mapped[float]      = mapped_column(Float, nullable=False)       # price that activates the trail
    trail_distance: Mapped[float]      = mapped_column(Float, nullable=False)       # trailing distance in price units
    trade_id:       Mapped[str | None] = mapped_column(String(128), nullable=True)  # Oanda clientTradeID to attach trail to

    # Status
    status:         Mapped[str]        = mapped_column(String(16), default="pending")  # pending | fired | cancelled | error
    fired_at:       Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_detail:   Mapped[str | None] = mapped_column(Text, nullable=True)
