from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from app.models.order import Order


@dataclass
class BrokerOrderResult:
    success: bool
    broker_order_id: str | None = None
    filled_quantity: float = 0.0
    avg_fill_price: float | None = None
    error_message: str | None = None
    order_open: bool = False


@dataclass
class OrderStatusResult:
    """Result of polling an open order's current state from the broker."""
    found: bool                    # False if the order no longer exists on the broker
    is_filled: bool = False
    is_cancelled: bool = False
    is_open: bool = False          # still resting on the book
    filled_quantity: float = 0.0
    avg_fill_price: float | None = None
    error_message: str | None = None


class BrokerBase(ABC):

    @abstractmethod
    async def submit_order(self, order: Order) -> BrokerOrderResult:
        ...

    @abstractmethod
    async def get_position(self, account: str, symbol: str) -> float:
        ...

    @abstractmethod
    async def cancel_order(self, broker_order_id: str, account: str) -> bool:
        ...

    async def cancel_replace_order(
        self, broker_order_id: str, account: str, new_order: Order
    ) -> BrokerOrderResult:
        cancelled = await self.cancel_order(broker_order_id, account)
        if not cancelled:
            return BrokerOrderResult(
                success=False,
                error_message=f"Cancel of order {broker_order_id} failed before replace"
            )
        return await self.submit_order(new_order)

    async def poll_order_status(
        self, broker_order_id: str, account: str
    ) -> OrderStatusResult:
        """
        Poll the current status of an open order.
        Default implementation returns not-found — override in brokers that support it.
        """
        return OrderStatusResult(found=False)
