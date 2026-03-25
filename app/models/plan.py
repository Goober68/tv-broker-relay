from datetime import datetime, timezone
from sqlalchemy import (
    String, Boolean, DateTime, ForeignKey,
    Integer, Float, JSON, UniqueConstraint
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
import uuid
from app.models.db import Base


class Plan(Base):
    """
    Subscription plan definition.
    Seeded at startup — not tenant-editable.

    Limits use -1 to mean "unlimited".
    allowed_order_types is a JSON list, e.g. ["market", "limit", "stop"].
    None means all types allowed.
    """
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)  # "free", "pro", "enterprise"
    display_name: Mapped[str] = mapped_column(String(128))
    stripe_price_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # ── Limits ────────────────────────────────────────────────────────────────
    max_broker_accounts: Mapped[int] = mapped_column(Integer, default=1)
    max_monthly_orders: Mapped[int] = mapped_column(Integer, default=100)  # -1 = unlimited
    max_open_orders: Mapped[int] = mapped_column(Integer, default=5)       # -1 = unlimited
    requests_per_minute: Mapped[int] = mapped_column(Integer, default=10)  # webhook rate limit

    # JSON list of allowed order types, e.g. ["market"] or ["market","limit","stop"]
    # NULL means all types are allowed
    allowed_order_types: Mapped[list | None] = mapped_column(JSON, nullable=True, default=None)

    # ── Risk overrides (None = use global settings default) ───────────────────
    max_position_size: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_daily_loss: Mapped[float | None] = mapped_column(Float, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="plan")

    def __repr__(self):
        return f"<Plan {self.name} orders/mo={self.max_monthly_orders}>"


class Subscription(Base):
    """
    A tenant's active plan subscription.
    One subscription per tenant at any time.
    """
    __tablename__ = "subscriptions"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_subscription_tenant"),
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
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))

    # Stripe identifiers — null for free/manually assigned plans
    stripe_customer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)

    # Status mirrors Stripe: active, past_due, canceled, trialing, incomplete
    status: Mapped[str] = mapped_column(String(32), default="active")

    current_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Running counter — reset each billing period by a scheduled job or Stripe webhook
    orders_this_period: Mapped[int] = mapped_column(Integer, default=0)

    plan: Mapped["Plan"] = relationship(back_populates="subscriptions")
    tenant: Mapped["Tenant"] = relationship(back_populates="subscription")  # type: ignore[name-defined]

    @property
    def is_active(self) -> bool:
        return self.status in ("active", "trialing")

    def __repr__(self):
        return f"<Subscription tenant={self.tenant_id} plan={self.plan_id} status={self.status}>"
