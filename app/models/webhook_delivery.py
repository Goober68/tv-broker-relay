from datetime import datetime, timezone
from sqlalchemy import String, Integer, Float, DateTime, Text, ForeignKey, Index, Boolean
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID
import uuid
from app.models.db import Base


class WebhookDelivery(Base):
    """
    Audit log of every inbound webhook call.
    Written before processing — captures raw payload, outcome, and timing.
    Invaluable for debugging TradingView alert misconfigurations.
    Retained for 90 days by default (purged by background task).
    """
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        Index("ix_deliveries_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), index=True)

    # Request metadata
    source_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # Raw payload as received (secret field stripped before storage)
    raw_payload: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Outcome
    # http_status: what we returned to TradingView
    http_status: Mapped[int] = mapped_column(Integer)
    # Whether auth passed (False = 403 before any processing)
    auth_passed: Mapped[bool] = mapped_column(Boolean, default=False)
    # Resulting order ID if one was created
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id"), nullable=True
    )
    # Short human-readable outcome for the list view
    outcome: Mapped[str] = mapped_column(String(32))  # "filled", "open", "rejected", "error", "auth_failed", "rate_limited", "validation_error"
    # Full error detail if applicable
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Timing
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    def __repr__(self):
        return (
            f"<WebhookDelivery {self.id} tenant={self.tenant_id} "
            f"{self.outcome} {self.http_status}>"
        )
