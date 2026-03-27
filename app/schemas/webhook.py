from pydantic import BaseModel, field_validator, model_validator
from typing import Literal
from datetime import datetime
from app.models.order import (
    OrderAction, OrderType, TimeInForce, InstrumentType,
    BROKER_INSTRUMENT_SUPPORT,
)


class WebhookPayload(BaseModel):
    secret: str | None = None  # API key — used when X-Webhook-Secret header is not available
    broker: Literal["oanda", "ibkr", "tradovate", "etrade", "rithmic", "tradestation", "alpaca", "tastytrade"]
    account: str = "primary"
    action: OrderAction
    symbol: str
    instrument_type: InstrumentType = InstrumentType.FOREX
    exchange: str | None = None    # e.g. "CME", "NASDAQ" — optional hint for IBKR
    currency: str | None = None    # settlement currency, e.g. "USD"
    order_type: OrderType = OrderType.MARKET
    quantity: float                # shares for equity, contracts for futures, units for forex
    price: float | None = None
    comment: str | None = None

    # Limit / stop order controls
    time_in_force: TimeInForce = TimeInForce.GFD
    expire_at: datetime | None = None

    @field_validator("expire_at", mode="before")
    @classmethod
    def parse_expire_at(cls, v):
        """
        Accept expire_at as:
          - ISO 8601 string:  "2025-06-01T14:30:00Z"
          - Unix ms integer:  1748785800000  (from TradingView {{timenow + 900000}})
          - Unix s integer:   1748785800
        """
        if v is None:
            return None
        if isinstance(v, (int, float)):
            ts = float(v)
            # Heuristic: values > 1e10 are milliseconds
            if ts > 1_000_000_000_000:
                ts = ts / 1000
            from datetime import timezone
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        return v  # Let pydantic handle ISO string parsing

    # Cancel-and-replace
    cancel_replace_id: str | None = None

    # Equity-specific
    extended_hours: bool = False   # allow pre/post-market fills (equities only)

    # Options contract spec — required when instrument_type=option
    option_expiry: str | None = None    # ISO date: "2025-03-21"
    option_strike: float | None = None  # strike price: 185.0
    option_right: str | None = None     # "C" (call) or "P" (put)
    option_multiplier: float = 100.0    # shares per contract (standard = 100)

    # Risk management
    stop_loss: float | None = None
    take_profit: float | None = None
    trailing_distance: float | None = None  # legacy — kept for backward compat

    # Trailing stop (Tradovate) — all values in units specified by sl_tp_type
    trail_trigger: float | None = None  # price level where trail activates (stopPrice)
    trail_dist:    float | None = None  # trailing distance (trailPrice)
    trail_update:  float | None = None  # minimum move before trail updates (step)

    # SL/TP unit type — controls how stop_loss, take_profit, trailing_distance,
    # trail_trigger, trail_dist, and trail_update are interpreted.
    # If omitted, the relay infers the type from instrument_type (legacy behaviour).
    #
    #   "absolute" — price levels (e.g. 1.07500, 148.750, 20900.0)
    #   "ticks"    — number of ticks from entry (futures only, e.g. 20 ticks on NQ = 5 pts)
    #   "pips"     — number of pips from entry (forex only, e.g. 50 pips on EUR_USD = 0.0050)
    #   "points"   — raw price points from entry (any instrument, e.g. 2.50)
    sl_tp_type: Literal["absolute", "ticks", "pips", "points"] | None = None

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, v: str) -> str:
        return v.upper().strip()

    @field_validator("exchange")
    @classmethod
    def normalize_exchange(cls, v: str | None) -> str | None:
        return v.upper().strip() if v else None

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, v: str | None) -> str | None:
        return v.upper().strip() if v else None

    @field_validator("quantity")
    @classmethod
    def quantity_must_be_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("quantity must be positive")
        return v

    @model_validator(mode="after")
    def broker_supports_instrument_type(self) -> "WebhookPayload":
        """Reject instrument types the broker cannot trade."""
        supported = BROKER_INSTRUMENT_SUPPORT.get(self.broker, set())
        if self.instrument_type.value not in supported:
            raise ValueError(
                f"Broker {self.broker!r} does not support instrument type "
                f"{self.instrument_type.value!r}. "
                f"Supported types: {sorted(supported)}"
            )
        return self

    @model_validator(mode="after")
    def futures_quantity_must_be_integer(self) -> "WebhookPayload":
        """Futures are traded in whole contracts."""
        if self.instrument_type == InstrumentType.FUTURE:
            if self.quantity != int(self.quantity):
                raise ValueError(
                    f"Futures quantity must be a whole number of contracts, got {self.quantity}"
                )
        return self

    @model_validator(mode="after")
    def extended_hours_equity_only(self) -> "WebhookPayload":
        if self.extended_hours and self.instrument_type != InstrumentType.EQUITY:
            raise ValueError("extended_hours is only valid for equity orders")
        return self

    @model_validator(mode="after")
    def option_fields_required_for_options(self) -> "WebhookPayload":
        if self.instrument_type == InstrumentType.OPTION:
            missing = []
            if not self.option_expiry:
                missing.append("option_expiry")
            if self.option_strike is None:
                missing.append("option_strike")
            if not self.option_right:
                missing.append("option_right")
            if missing:
                raise ValueError(
                    f"Options require: {missing}. "
                    "Provide option_expiry (YYYY-MM-DD), option_strike, and option_right ('C' or 'P')."
                )
            if self.option_right.upper() not in ("C", "P"):
                raise ValueError("option_right must be 'C' (call) or 'P' (put)")
            self.option_right = self.option_right.upper()
        return self

    @model_validator(mode="after")
    def price_required_for_limit_stop(self) -> "WebhookPayload":
        if self.order_type in (OrderType.LIMIT, OrderType.STOP) and self.price is None:
            raise ValueError("price is required for limit and stop orders")
        return self

    @model_validator(mode="after")
    def gtd_requires_expire_at(self) -> "WebhookPayload":
        if self.time_in_force == TimeInForce.GTD and self.expire_at is None:
            raise ValueError("expire_at is required when time_in_force is GTD")
        return self

    @model_validator(mode="after")
    def tif_market_order_rules(self) -> "WebhookPayload":
        if self.order_type == OrderType.MARKET:
            if self.time_in_force not in (TimeInForce.FOK, TimeInForce.IOC, TimeInForce.GFD):
                raise ValueError(
                    f"Market orders only support FOK, IOC, or GFD time_in_force, got {self.time_in_force}"
                )
        return self



    @model_validator(mode="after")
    def validate_sl_tp_side(self) -> "WebhookPayload":
        # SL/TP may be offsets (ticks/pips) at this point — validation
        # of direction is deferred to offset_converter after conversion.
        return self


class OrderResponse(BaseModel):
    order_id: int
    status: str
    broker_order_id: str | None = None
    message: str | None = None

    class Config:
        from_attributes = True
