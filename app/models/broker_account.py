from datetime import datetime, timezone
from sqlalchemy import String, Boolean, DateTime, ForeignKey, Text, JSON, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
import uuid
from app.models.db import Base
from typing import Literal

BrokerName = Literal["oanda", "ibkr", "tradovate", "etrade", "rithmic", "tradestation", "alpaca", "tastytrade"]

BROKER_CREDENTIAL_FIELDS: dict[str, list[str]] = {
    "oanda":     ["api_key", "account_id", "base_url"],
    "ibkr":      ["gateway_url", "account_id"],
    "tradovate": ["username", "password", "app_id", "app_version", "device_id", "cid", "sec", "base_url"],
    "etrade":    ["consumer_key", "consumer_secret", "oauth_token", "oauth_token_secret",
                  "account_id", "base_url"],
}


class BrokerAccount(Base):
    """
    A tenant's connection to a broker.

    instrument_map is a JSON dict used by IBKR (and optionally others) to resolve
    symbol → broker-specific contract identifier.

    For IBKR, the format is:
        {
            "AAPL":  {"conid": 265598,   "exchange": "NASDAQ", "sec_type": "STK"},
            "ES":    {"conid": 495512551, "exchange": "CME",    "sec_type": "FUT",
                      "multiplier": 50.0},
            "EUR":   {"conid": 12087792,  "exchange": "IDEALPRO", "sec_type": "CASH"},
        }

    For Tradovate, conid is not used — symbol alone works. But multiplier overrides
    can still be stored here:
        {
            "ES": {"multiplier": 50.0},
            "NQ": {"multiplier": 20.0},
        }

    instrument_map is NOT encrypted — it contains no secrets.
    """
    __tablename__ = "broker_accounts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "broker", "account_alias", name="uq_broker_account"),
        Index("ix_broker_accounts_tenant", "tenant_id"),
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
    broker: Mapped[str] = mapped_column(String(32))
    account_alias: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    credentials_encrypted: Mapped[str] = mapped_column(Text)

    # Per-account instrument configuration (not encrypted — no secrets)
    instrument_map: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # FIFO randomization — for US Oanda accounts subject to NFA FIFO rules
    # Adds a small random offset to order quantity so each trade has a unique size
    # allowing individual trades to be identified and closed without FIFO conflicts
    fifo_randomize:  Mapped[bool]       = mapped_column(Boolean, default=False)
    fifo_max_offset: Mapped[int]        = mapped_column(Integer, default=3)

    # Auto-close configuration — for prop firm session-end compliance
    # auto_close_time: "HH:MM" in ET (e.g. "16:50" = 4:50 PM ET = 10 min before 5 PM roll)
    auto_close_enabled: Mapped[bool]        = mapped_column(Boolean, default=False)
    auto_close_time:    Mapped[str | None]  = mapped_column(String(5), nullable=True)  # "HH:MM" ET

    tenant: Mapped["Tenant"] = relationship(back_populates="broker_accounts")  # type: ignore

    def get_instrument(self, symbol: str) -> dict | None:
        """Look up instrument config for a symbol. Returns None if not configured."""
        if not self.instrument_map:
            return None
        return self.instrument_map.get(symbol)

    def __repr__(self):
        return (
            f"<BrokerAccount {self.id} tenant={self.tenant_id} "
            f"{self.broker}/{self.account_alias} active={self.is_active}>"
        )
