"""
Tastytrade Broker Adapter.

Uses the Tastytrade REST API.
Supports equities, equity options, futures, and futures options.

Credentials required:
    username:    Tastytrade account username (email)
    password:    Tastytrade account password
    account_id:  Account number (e.g. "5WX12345")
    base_url:    API base URL
                 Live: https://api.tastytrade.com
                 Cert: https://api.cert.tastytrade.com  (sandbox)

Session tokens are short-lived (~24h). The adapter refreshes automatically.

API docs: https://developer.tastytrade.com
"""
import httpx
import logging
from datetime import datetime, timezone, timedelta
from app.brokers.base import BrokerBase, BrokerOrderResult, OrderStatusResult
from app.models.order import (
    Order, OrderAction, OrderType, TimeInForce, InstrumentType,
)

logger = logging.getLogger(__name__)

_TIF_MAP = {
    TimeInForce.DAY: "Day",
    TimeInForce.GTC: "GTC",
    TimeInForce.IOC: "IOC",
    TimeInForce.FOK: "FOK",
    TimeInForce.GTD: "GTD",
}

_FILLED_STATUSES    = {"Filled", "filled"}
_CANCELLED_STATUSES = {"Cancelled", "cancelled", "Expired", "expired",
                        "Rejected", "rejected"}


class TastytradeBroker(BrokerBase):

    def __init__(
        self,
        username: str,
        password: str,
        account_id: str,
        base_url: str = "https://api.tastytrade.com",
    ):
        self.username   = username
        self.password   = password
        self.account_id = account_id
        self.base_url   = base_url.rstrip("/")

        self._session_token: str | None      = None
        self._token_expiry:  datetime | None = None

    @classmethod
    def from_credentials(cls, creds: dict) -> "TastytradeBroker":
        return cls(
            username   = creds["username"],
            password   = creds["password"],
            account_id = creds["account_id"],
            base_url   = creds.get("base_url", "https://api.tastytrade.com"),
        )

    @classmethod
    def from_settings(cls) -> "TastytradeBroker":
        from app.config import get_settings
        s = get_settings()
        return cls(
            username   = s.tastytrade_username,
            password   = s.tastytrade_password,
            account_id = s.tastytrade_account_id,
            base_url   = s.tastytrade_base_url,
        )

    # ── Authentication ─────────────────────────────────────────────────────────

    async def _ensure_authenticated(self) -> str:
        if (self._session_token
                and self._token_expiry
                and datetime.now(timezone.utc) < self._token_expiry):
            return self._session_token

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{self.base_url}/sessions",
                json={
                    "login":      self.username,
                    "password":   self.password,
                    "remember-me": True,
                },
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json().get("data", resp.json())

            self._session_token = data.get("session-token") or data.get("token")
            if not self._session_token:
                raise RuntimeError(
                    f"Tastytrade auth response missing session-token. "
                    f"Keys: {list(data.keys())}"
                )
            # Session tokens last ~24h — refresh after 23h
            self._token_expiry = datetime.now(timezone.utc) + timedelta(hours=23)
            logger.info("Tastytrade authentication successful")
            return self._session_token

    def _headers(self, token: str) -> dict:
        return {
            "Authorization": token,  # Tastytrade uses bare token, not "Bearer"
            "Content-Type":  "application/json",
        }

    def _resolve_account(self, account: str) -> str:
        return self.account_id if account == "primary" else account

    # ── Order Submission ───────────────────────────────────────────────────────

    async def submit_order(self, order: Order) -> BrokerOrderResult:
        if order.action == OrderAction.CLOSE:
            return await self._close_position(order.account, order.symbol)

        try:
            token = await self._ensure_authenticated()
        except Exception as e:
            return BrokerOrderResult(success=False, error_message=f"Auth failed: {e}")

        account_id = self._resolve_account(order.account)
        body = self._build_order_body(order)

        async with httpx.AsyncClient(
            headers=self._headers(token), timeout=15.0
        ) as client:
            try:
                resp = await client.post(
                    f"{self.base_url}/accounts/{account_id}/orders",
                    json=body,
                )
                resp.raise_for_status()
                data = resp.json().get("data", resp.json())

                order_data = data.get("order", data)
                order_id   = str(order_data.get("id", ""))
                status     = order_data.get("status", "")
                errors     = data.get("errors", [])

                if errors:
                    msg = "; ".join(e.get("message", str(e)) for e in errors)
                    return BrokerOrderResult(success=False, error_message=msg)

                is_open = order.order_type != OrderType.MARKET
                if status in _FILLED_STATUSES:
                    filled = float(order_data.get("size", order.quantity))
                    avg    = float(order_data.get("average-fill-price", 0)) or None
                    return BrokerOrderResult(
                        success=True,
                        broker_order_id=order_id,
                        filled_quantity=filled,
                        avg_fill_price=avg,
                    )

                return BrokerOrderResult(
                    success=True,
                    broker_order_id=order_id,
                    order_open=is_open,
                )

            except httpx.HTTPStatusError as e:
                logger.error(f"Tastytrade order error {e.response.status_code}: {e.response.text}")
                return BrokerOrderResult(success=False, error_message=e.response.text)
            except Exception as e:
                logger.exception("Unexpected error submitting to Tastytrade")
                return BrokerOrderResult(success=False, error_message=str(e))

    def _build_order_body(self, order: Order) -> dict:
        is_buy = order.action == OrderAction.BUY

        # Tastytrade uses "legs" architecture
        action_str = "Buy to Open" if is_buy else "Sell to Open"

        # Determine instrument type string
        if order.instrument_type == InstrumentType.FUTURE:
            instrument_type_str = "Future"
        elif order.instrument_type == InstrumentType.OPTION:
            instrument_type_str = "Equity Option"
        else:
            instrument_type_str = "Equity"

        leg = {
            "instrument-type": instrument_type_str,
            "symbol":          order.symbol,
            "quantity":        int(order.quantity) if order.instrument_type == InstrumentType.FUTURE else order.quantity,
            "action":          action_str,
        }

        body: dict = {
            "order-type":    "Market" if order.order_type == OrderType.MARKET
                             else "Limit" if order.order_type == OrderType.LIMIT
                             else "Stop Limit" if order.order_type == OrderType.STOP_LIMIT
                             else "Stop",
            "time-in-force": _TIF_MAP.get(order.time_in_force, "Day"),
            "legs":          [leg],
        }

        if order.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and order.price:
            body["price"] = str(order.price)

        if order.order_type in (OrderType.STOP, OrderType.STOP_LIMIT) and order.price:
            body["stop-trigger"] = str(order.price)

        if order.time_in_force == TimeInForce.GTD and order.expire_at:
            body["gtc-date"] = order.expire_at.strftime("%Y-%m-%d")

        # Tastytrade doesn't support native bracket orders
        # SL/TP must be placed as separate orders after fill
        if order.stop_loss or order.take_profit:
            logger.warning(
                f"Tastytrade: stop_loss/take_profit not supported as bracket orders. "
                f"Place separate orders after fill for {order.symbol}."
            )

        return body

    # ── Close Position ─────────────────────────────────────────────────────────

    async def _close_position(self, account: str, symbol: str) -> BrokerOrderResult:
        qty = await self.get_position(account, symbol)
        if abs(qty) < 1e-9:
            return BrokerOrderResult(
                success=True, filled_quantity=0,
                error_message="Position already flat"
            )

        close_order = Order(
            tenant_id       = 0,
            broker          = "tastytrade",
            account         = account,
            symbol          = symbol,
            instrument_type = InstrumentType.EQUITY,
            action          = OrderAction.SELL if qty > 0 else OrderAction.BUY,
            order_type      = OrderType.MARKET,
            quantity        = abs(qty),
            time_in_force   = TimeInForce.DAY,
        )
        return await self.submit_order(close_order)

    # ── Get Position ───────────────────────────────────────────────────────────

    async def get_position(self, account: str, symbol: str) -> float:
        try:
            token = await self._ensure_authenticated()
        except Exception:
            return 0.0

        account_id = self._resolve_account(account)
        async with httpx.AsyncClient(
            headers=self._headers(token), timeout=10.0
        ) as client:
            try:
                resp = await client.get(
                    f"{self.base_url}/accounts/{account_id}/positions"
                )
                resp.raise_for_status()
                data = resp.json().get("data", {})
                items = data.get("items", [])
                for pos in items:
                    if pos.get("symbol") == symbol:
                        qty = float(pos.get("quantity", 0))
                        direction = pos.get("quantity-direction", "Long")
                        return qty if direction == "Long" else -qty
                return 0.0
            except Exception:
                logger.exception(f"Error fetching Tastytrade position for {symbol}")
                return 0.0

    # ── Cancel Order ───────────────────────────────────────────────────────────

    async def cancel_order(self, broker_order_id: str, account: str) -> bool:
        try:
            token = await self._ensure_authenticated()
        except Exception:
            return False

        account_id = self._resolve_account(account)
        async with httpx.AsyncClient(
            headers=self._headers(token), timeout=10.0
        ) as client:
            try:
                resp = await client.delete(
                    f"{self.base_url}/accounts/{account_id}/orders/{broker_order_id}"
                )
                return resp.status_code in (200, 204)
            except Exception:
                logger.exception(f"Error cancelling Tastytrade order {broker_order_id}")
                return False

    # ── Cancel-Replace ─────────────────────────────────────────────────────────

    async def cancel_replace_order(
        self, broker_order_id: str, account: str, new_order: Order
    ) -> BrokerOrderResult:
        cancelled = await self.cancel_order(broker_order_id, account)
        if not cancelled:
            return BrokerOrderResult(
                success=False,
                error_message=f"Cancel of {broker_order_id} failed before replace"
            )
        return await self.submit_order(new_order)

    # ── Poll Order Status ──────────────────────────────────────────────────────

    async def poll_order_status(
        self, broker_order_id: str, account: str
    ) -> OrderStatusResult:
        try:
            token = await self._ensure_authenticated()
        except Exception:
            return OrderStatusResult(found=False)

        account_id = self._resolve_account(account)
        async with httpx.AsyncClient(
            headers=self._headers(token), timeout=10.0
        ) as client:
            try:
                resp = await client.get(
                    f"{self.base_url}/accounts/{account_id}/orders/{broker_order_id}"
                )
                if resp.status_code == 404:
                    return OrderStatusResult(found=False)
                resp.raise_for_status()
                data  = resp.json().get("data", resp.json())
                order = data.get("order", data)
                status = order.get("status", "")

                if status in _FILLED_STATUSES:
                    filled = float(order.get("size", 0))
                    avg    = float(order.get("average-fill-price", 0)) or None
                    return OrderStatusResult(
                        found=True, is_filled=True,
                        filled_quantity=filled, avg_fill_price=avg,
                    )
                if status in _CANCELLED_STATUSES:
                    return OrderStatusResult(found=True, is_cancelled=True)
                return OrderStatusResult(found=True, is_open=True)

            except Exception as e:
                logger.exception(f"Error polling Tastytrade order {broker_order_id}")
                return OrderStatusResult(found=False, error_message=str(e))

    # ── Live P&L Polling ───────────────────────────────────────────────────────

    async def get_open_positions_pnl(self, account: str) -> list[dict]:
        try:
            token = await self._ensure_authenticated()
        except Exception:
            return []

        account_id = self._resolve_account(account)
        async with httpx.AsyncClient(
            headers=self._headers(token), timeout=10.0
        ) as client:
            try:
                resp = await client.get(
                    f"{self.base_url}/accounts/{account_id}/positions"
                )
                resp.raise_for_status()
                data  = resp.json().get("data", {})
                items = data.get("items", [])
                result = []
                for pos in items:
                    qty = float(pos.get("quantity", 0))
                    if abs(qty) < 1e-9:
                        continue
                    direction = pos.get("quantity-direction", "Long")
                    if direction == "Short":
                        qty = -qty
                    result.append({
                        "symbol":         pos.get("symbol", ""),
                        "last_price":     float(pos.get("mark", 0)) or None,
                        "unrealized_pnl": float(pos.get("unrealized-day-gain", 0)) or None,
                    })
                return result
            except Exception:
                logger.exception("Error fetching Tastytrade positions P&L")
                return []
