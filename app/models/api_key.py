from datetime import datetime, timezone
from sqlalchemy import String, Boolean, DateTime, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.models.db import Base


class ApiKey(Base):
    """
    Per-tenant webhook API keys.

    The raw key is only returned once at creation time (like GitHub PATs).
    We store a SHA-256 hash for fast lookup and never the plaintext.

    Format of raw key:  tvr_{tenant_id}_{48 random url-safe bytes}
    The prefix makes keys identifiable in logs/configs without exposing the secret.
    """
    __tablename__ = "api_keys"
    __table_args__ = (
        Index("ix_api_keys_tenant_active", "tenant_id", "is_active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)

    # Human-readable label set by the tenant (e.g. "TradingView Production")
    name: Mapped[str] = mapped_column(String(128))

    # SHA-256 hash of the raw key — used for lookup
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    # First 12 chars of raw key stored in plain text for display ("tvr_3_aBcD...")
    # Never enough to reconstruct the key, just enough to identify it in a list
    key_prefix: Mapped[str] = mapped_column(String(20))

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tenant: Mapped["Tenant"] = relationship(back_populates="api_keys")  # type: ignore[name-defined]

    def __repr__(self):
        return f"<ApiKey {self.id} tenant={self.tenant_id} name={self.name!r} active={self.is_active}>"
