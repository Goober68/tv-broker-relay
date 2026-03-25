from datetime import datetime, timezone
from sqlalchemy import String, Float, DateTime, Text, ForeignKey, Enum as SAEnum, Index
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID
import uuid
from app.models.db import Base
import enum


class OrderStatus(str, enum.Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    OPEN = "open"
    FILLED = "filled"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    ERROR = "error"


class OrderAction(str, enum.Enum):
    BUY = "buy"
    SELL = "sell"
    CLOSE = "close"


class OrderType(str, enum.Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


class TimeInForce(str, enum.Enum):
    GTC = "GTC"
    GTD = "GTD"
    DAY = "DAY"
    IOC = "IOC"
    FOK = "FOK"


class InstrumentType(str, enum.Enum):
    FOREX = "forex"      # currency pairs — Oanda native
    EQUITY = "equity"    # stocks, ETFs — IBKR, E*Trade
    FUTURE = "future"    # exchange-traded futures — IBKR, Tradovate
    CFD = "cfd"          # CFDs — Oanda
    OPTION = "option"   # Equity options — IBKR, E*Trade


# Which instrument types each broker supports
BROKER_INSTRUMENT_SUPPORT: dict[str, set[str]] = {
    "oanda":     {"forex", "cfd"},
    "ibkr":      {"equity", "future", "forex", "option"},
    "tradovate": {"future"},
    "etrade":    {"equity", "option"},
    "rithmic":   {"future"},
}

# IBKR secType mapping
IBKR_SEC_TYPE: dict[str, str] = {
    "equity": "STK",
    "future": "FUT",
    "forex":  "CASH",
    "option": "OPT",
}

# Well-known futures multipliers (point value per contract).
# Tenants can override these in their broker account instrument_map.
# Values are in the contract's native currency (usually USD).
DEFAULT_FUTURES_MULTIPLIERS: dict[str, float] = {
    # Equity index futures (CME)
    "ES":   50.0,   # E-mini S&P 500
    "NQ":   20.0,   # E-mini Nasdaq-100
    "RTY":  50.0,   # E-mini Russell 2000
    "YM":    5.0,   # E-mini Dow
    "MES":   5.0,   # Micro E-mini S&P 500
    "MNQ":   2.0,   # Micro E-mini Nasdaq
    # Energy (NYMEX)
    "CL":  1000.0,  # WTI Crude Oil
    "NG":  10000.0, # Natural Gas
    "RB":  42000.0, # Gasoline (42,000 gal/contract)
    # Metals (COMEX)
    "GC":   100.0,  # Gold
    "SI":   5000.0, # Silver
    "HG":  25000.0, # Copper
    # Treasuries (CBOT)
    "ZB":   1000.0, # 30-Year T-Bond
    "ZN":   1000.0, # 10-Year T-Note
    "ZF":   1000.0, # 5-Year T-Note
    # FX futures (CME)
    "6E":  125000.0, # Euro FX
    "6B":   62500.0, # British Pound
    "6J": 12500000.0,# Japanese Yen
}


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

    # Routing
    broker: Mapped[str] = mapped_column(String(32))
    account: Mapped[str] = mapped_column(String(64))

    # Instrument
    symbol: Mapped[str] = mapped_column(String(32))
    instrument_type: Mapped[str] = mapped_column(
        SAEnum(InstrumentType), default=InstrumentType.FOREX
    )
    # Optional instrument qualifiers
    exchange: Mapped[str | None] = mapped_column(String(32), nullable=True)  # e.g. "CME", "NASDAQ"
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)   # e.g. "USD", "EUR"

    # Order details
    action: Mapped[str] = mapped_column(SAEnum(OrderAction))
    order_type: Mapped[str] = mapped_column(SAEnum(OrderType), default=OrderType.MARKET)
    quantity: Mapped[float] = mapped_column(Float)  # shares for equity, contracts for futures
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    time_in_force: Mapped[str] = mapped_column(SAEnum(TimeInForce), default=TimeInForce.GTC)
    expire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Futures-specific
    # Effective multiplier used for P&L calculation — captured at order time
    multiplier: Mapped[float] = mapped_column(Float, default=1.0)

    # Options contract spec (all nullable — only used when instrument_type=option)
    option_expiry: Mapped[str | None] = mapped_column(String(16), nullable=True)   # "2025-03-21"
    option_strike: Mapped[float | None] = mapped_column(Float, nullable=True)       # 185.0
    option_right: Mapped[str | None] = mapped_column(String(4), nullable=True)      # "C" or "P"
    option_multiplier: Mapped[float] = mapped_column(Float, default=100.0)          # standard = 100 shares/contract

    # Equity-specific
    extended_hours: Mapped[bool] = mapped_column(
        __import__("sqlalchemy").Boolean, default=False
    )  # pre/post market trading

    # Risk management
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    trailing_distance: Mapped[float | None] = mapped_column(Float, nullable=True)

    # State
    status: Mapped[str] = mapped_column(SAEnum(OrderStatus), default=OrderStatus.PENDING)
    broker_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    filled_quantity: Mapped[float] = mapped_column(Float, default=0.0)
    avg_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Metadata
    raw_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    comment: Mapped[str | None] = mapped_column(String(256), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    @property
    def is_resting(self) -> bool:
        return self.status == OrderStatus.OPEN

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            OrderStatus.FILLED, OrderStatus.CANCELLED,
            OrderStatus.REJECTED, OrderStatus.ERROR,
        )

    def __repr__(self):
        return (
            f"<Order {self.id} tenant={self.tenant_id} {self.action} {self.quantity} "
            f"{self.instrument_type}:{self.symbol} @ {self.price or 'MKT'} "
            f"on {self.broker} [{self.status}]>"
        )
