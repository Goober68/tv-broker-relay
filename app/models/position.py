from datetime import datetime, timezone
from sqlalchemy import String, Float, DateTime, ForeignKey, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID
import uuid
from app.models.db import Base


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "broker", "account", "symbol", name="uq_position_tenant"),
        Index("ix_positions_tenant", "tenant_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), index=True)
    broker_account_id: Mapped[int | None] = mapped_column(ForeignKey("broker_accounts.id"), nullable=True, index=True)

    # Kept for quick access and unique constraint — broker_account_id is the canonical FK
    broker: Mapped[str] = mapped_column(String(32))
    account: Mapped[str] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(32))
    instrument_type: Mapped[str] = mapped_column(String(16), default="forex")

    # Positive = long, negative = short, 0 = flat
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    avg_price: Mapped[float] = mapped_column(Float, default=0.0)

    # Contract multiplier for futures P&L scaling.
    # Equities and forex: 1.0
    # Futures: e.g. 50.0 for ES ($50/point), 20.0 for NQ
    multiplier: Mapped[float] = mapped_column(Float, default=1.0)

    # Running P&L — multiplier-adjusted (i.e. in account currency)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    daily_realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    daily_pnl_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Live P&L — polled from broker every N seconds
    last_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    unrealized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_price_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def is_flat(self) -> bool:
        return abs(self.quantity) < 1e-9

    def is_long(self) -> bool:
        return self.quantity > 1e-9

    def is_short(self) -> bool:
        return self.quantity < -1e-9

    def __repr__(self):
        return (
            f"<Position tenant={self.tenant_id} {self.broker}/{self.account} "
            f"{self.instrument_type}:{self.symbol} qty={self.quantity:.4f} "
            f"mult={self.multiplier}>"
        )
