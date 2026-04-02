from datetime import datetime, timezone
from sqlalchemy import String, Float, DateTime, Text, ForeignKey, Boolean
from sqlalchemy import Enum as SAEnum, Index
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID
from app.models.db import Base
import uuid
import enum


class OrderStatus(str, enum.Enum):
    PENDING   = "pending"
    SUBMITTED = "submitted"
    OPEN      = "open"
    FILLED    = "filled"
    PARTIAL   = "partial"
    CANCELLED = "cancelled"
    REJECTED  = "rejected"
    ERROR     = "error"


class OrderAction(str, enum.Enum):
    BUY   = "buy"
    SELL  = "sell"
    CLOSE = "close"


class OrderType(str, enum.Enum):
    MARKET     = "market"
    LIMIT      = "limit"
    STOP       = "stop"
    STOP_LIMIT = "stop_limit"


class TimeInForce(str, enum.Enum):
    GTC = "GTC"
    GTD = "GTD"
    DAY = "DAY"
    GFD = "GFD"
    IOC = "IOC"
    FOK = "FOK"


class InstrumentType(str, enum.Enum):
    FOREX  = "forex"
    EQUITY = "equity"
    FUTURE = "future"
    CFD    = "cfd"
    OPTION = "option"


# Which instrument types each broker supports
BROKER_INSTRUMENT_SUPPORT: dict[str, set[str]] = {
    "oanda":     {"forex", "cfd"},
    "ibkr":      {"equity", "future", "forex", "option"},
    "tradovate": {"future"},
    "etrade":    {"equity", "option"},
    "rithmic":      {"future"},
    "tradestation": {"equity", "future", "option"},
    "alpaca":       {"equity", "option"},
    "tastytrade":   {"equity", "future", "option"},
}

# IBKR secType mapping
IBKR_SEC_TYPE: dict[str, str] = {
    "equity": "STK",
    "future": "FUT",
    "forex":  "CASH",
    "option": "OPT",
}

# Well-known futures multipliers (point value per contract in USD)
DEFAULT_FUTURES_MULTIPLIERS: dict[str, float] = {
    "ES":   50.0,    "NQ":   20.0,    "RTY":  50.0,   "YM":    5.0,
    "MES":   5.0,    "MNQ":   2.0,
    "CL": 1000.0,    "NG": 10000.0,   "RB": 42000.0,
    "GC":  100.0,    "SI":  5000.0,   "HG": 25000.0,  "MHG": 2500.0,
    "MGC":  10.0,    "SIL":  1000.0,
    "ZB": 1000.0,    "ZN":  1000.0,   "ZF":  1000.0,
    "6E": 125000.0,  "6B":  62500.0,  "6J": 12500000.0,
}

# Use native_enum=False so SQLAlchemy stores the string value directly.
# This avoids the uppercase/lowercase mismatch with PostgreSQL enums.
_ENUM_KWARGS = {"native_enum": False}


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_tenant_symbol", "tenant_id", "symbol"),
        Index("ix_orders_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), index=True)
    broker_account_id: Mapped[int | None] = mapped_column(ForeignKey("broker_accounts.id"), nullable=True, index=True)

    # Routing (kept for quick access — broker_account_id is the canonical FK)
    broker:  Mapped[str] = mapped_column(String(32))
    account: Mapped[str] = mapped_column(String(64))

    # Instrument
    symbol:          Mapped[str]       = mapped_column(String(32))
    instrument_type: Mapped[str]       = mapped_column(SAEnum(InstrumentType, **_ENUM_KWARGS), default=InstrumentType.FOREX)
    exchange:        Mapped[str | None] = mapped_column(String(32), nullable=True)
    currency:        Mapped[str | None] = mapped_column(String(8),  nullable=True)

    # Order details
    action:         Mapped[str]         = mapped_column(SAEnum(OrderAction,  **_ENUM_KWARGS))
    order_type:     Mapped[str]         = mapped_column(SAEnum(OrderType,    **_ENUM_KWARGS), default=OrderType.MARKET)
    quantity:       Mapped[float]       = mapped_column(Float)
    price:          Mapped[float | None] = mapped_column(Float, nullable=True)
    time_in_force:  Mapped[str]         = mapped_column(SAEnum(TimeInForce,  **_ENUM_KWARGS), default=TimeInForce.GTC)
    expire_at:      Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Futures / equity
    multiplier:     Mapped[float] = mapped_column(Float, default=1.0)
    extended_hours: Mapped[bool]  = mapped_column(Boolean, default=False)

    # Options
    option_expiry:     Mapped[str | None]   = mapped_column(String(16), nullable=True)
    option_strike:     Mapped[float | None] = mapped_column(Float, nullable=True)
    option_right:      Mapped[str | None]   = mapped_column(String(4), nullable=True)
    option_multiplier: Mapped[float]        = mapped_column(Float, default=100.0)

    # Risk management
    stop_loss:         Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit:       Mapped[float | None] = mapped_column(Float, nullable=True)
    trailing_distance: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Tradovate native trailing stop fields
    trail_trigger:     Mapped[float | None] = mapped_column(Float, nullable=True)
    trail_dist:        Mapped[float | None] = mapped_column(Float, nullable=True)
    trail_update:      Mapped[float | None] = mapped_column(Float, nullable=True)

    # State
    status:          Mapped[str]         = mapped_column(SAEnum(OrderStatus, **_ENUM_KWARGS), default=OrderStatus.PENDING)
    broker_order_id:   Mapped[str | None]  = mapped_column(String(128), nullable=True)
    client_trade_id:   Mapped[str | None]  = mapped_column(String(128), nullable=True)  # Oanda clientTradeID
    broker_quantity:   Mapped[float | None] = mapped_column(Float, nullable=True)        # actual quantity sent to broker (may differ due to FIFO randomization)
    filled_quantity: Mapped[float]       = mapped_column(Float, default=0.0)
    avg_fill_price:  Mapped[float | None] = mapped_column(Float, nullable=True)
    commission:      Mapped[float | None] = mapped_column(Float, nullable=True)  # per-contract commission from broker

    # Algo tracking
    algo_id:       Mapped[str | None] = mapped_column(String(64), nullable=True)
    algo_version:  Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Metadata
    raw_payload:   Mapped[str | None] = mapped_column(Text, nullable=True)
    comment:       Mapped[str | None] = mapped_column(String(256), nullable=True)
    error_message:  Mapped[str | None] = mapped_column(Text, nullable=True)
    broker_request:  Mapped[str | None] = mapped_column(Text, nullable=True)  # outbound JSON sent to broker on failure
    broker_response: Mapped[str | None] = mapped_column(Text, nullable=True)  # broker response body on failure

    @property
    def is_resting(self) -> bool:
        return self.status == OrderStatus.OPEN.value

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            OrderStatus.FILLED.value, OrderStatus.CANCELLED.value,
            OrderStatus.REJECTED.value, OrderStatus.ERROR.value,
        )

    def __repr__(self):
        return (
            f"<Order {self.id} tenant={self.tenant_id} {self.action} {self.quantity} "
            f"{self.instrument_type}:{self.symbol} @ {self.price or 'MKT'} "
            f"on {self.broker} [{self.status}]>"
        )
