"""
TradeStation Broker Adapter.

Uses the TradeStation REST API v3 with OAuth 2.0 authentication.
Supports equities, futures, and options.

Credentials required:
    client_id:     OAuth client ID from TradeStation developer portal
    client_secret: OAuth client secret
    refresh_token: Long-lived refresh token (obtained via OAuth flow)
    account_id:    TradeStation account number (e.g. "123456789")
    base_url:      API base URL
                   Live: https://api.tradestation.com
                   Sim:  https://sim-api.tradestation.com

To obtain refresh_token:
    1. Register app at https://developer.tradestation.com
    2. Complete OAuth 2.0 authorization code flow
    3. Store the refresh_token — the adapter auto-refreshes access tokens

API docs: https://api.tradestation.com/docs
"""
import httpx
import logging
from datetime import datetime, timezone, timedelta
from app.brokers.base import BrokerBase, BrokerOrderResult, OrderStatusResult
from app.models.order import (
    Order, OrderAction, OrderType, TimeInForce,
    InstrumentType, DEFAULT_FUTURES_MULTIPLIERS,
)

logger = logging.getLogger(__name__)

_ORDER_TYPE_MAP = {
    OrderType.MARKET:     "Market",
    OrderType.LIMIT:      "Limit",
    OrderType.STOP:       "StopMarket",
    OrderType.STOP_LIMIT: "StopLimit",
}

_TIF_MAP = {
    TimeInForce.DAY: "DAY",
    TimeInForce.GTC: "GTC",
    TimeInForce.IOC: "IOC",
    TimeInForce.FOK: "FOK",
    TimeInForce.GTD: "GTD",
}

_FILLED_STATUSES    = {"FLL", "FLP", "FILLED"}
_CANCELLED_STATUSES = {"CAN", "REJ", "EXP", "CANCELLED", "REJECTED", "EXPIRED"}
_OPEN_STATUSES      = {"ACK", "OPN", "DON", "UCN", "LAT", "TSO", "TBK"}


class TradeStationBroker(BrokerBase):

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        account_id: str,
        base_url: str = "https://api.tradestation.com",
    ):
        self.client_id      = client_id
        self.client_secret  = client_secret
        self.refresh_token  = refresh_token
        self.account_id     = account_id
        self.base_url       = base_url.rstrip("/")

        self._access_token: str | None      = None
        self._token_expiry: datetime | None = None

    @classmethod
    def from_credentials(cls, creds: dict) -> "TradeStationBroker":
        return cls(
            client_id     = creds["client_id"],
            client_secret = creds["client_secret"],
            refresh_token = creds["refresh_token"],
            account_id    = creds["account_id"],
            base_url      = creds.get("base_url", "https://api.tradestation.com"),
        )

    @classmethod
    def from_settings(cls) -> "TradeStationBroker":
        from app.config import get_settings
        s = get_settings()
        return cls(
            client_id     = s.tradestation_client_id,
            client_secret = s.tradestation_client_secret,
            refresh_token = s.tradestation_refresh_token,
            account_id    = s.tradestation_account_id,
            base_url      = s.tradestation_base_url,
        )

    # ── Authentication ─────────────────────────────────────────────────────────

    async def _ensure_authenticated(self) -> str:
        if (self._access_token
                and self._token_expiry
                and datetime.now(timezone.utc) < self._token_expiry):
            return self._access_token

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://signin.tradestation.com/oauth/token",
                data={
                    "grant_type":    "refresh_token",
                    "client_id":     self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": self.refresh_token,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            data = resp.json()

            self._access_token = data["access_token"]
            expires_in = int(data.get("expires_in", 1200))
            self._token_expiry = (
                datetime.now(timezone.utc) + timedelta(seconds=expires_in - 60)
            )
            # Store new refresh token if rotated
            if "refresh_token" in data:
                self.refresh_token = data["refresh_token"]

            logger.info("TradeStation authentication successful")
            return self._access_token

    def _headers(self, token: str) -> dict:
        return {
            "Authorization": f"Bearer {token}",
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
                    f"{self.base_url}/v3/orderexecution/orders",
                    json=body,
                )
                resp.raise_for_status()
                data = resp.json()

                # TradeStation returns list of order results
                orders = data.get("Orders", [data])
                if not orders:
                    return BrokerOrderResult(
                        success=False, error_message="No order result returned"
                    )

                first = orders[0]
                order_id = str(first.get("OrderID", ""))
                error = first.get("Error") or first.get("Message")

                if error:
                    return BrokerOrderResult(success=False, error_message=error)

                is_open = order.order_type != OrderType.MARKET
                return BrokerOrderResult(
                    success=True,
                    broker_order_id=order_id,
                    order_open=is_open,
                )

            except httpx.HTTPStatusError as e:
                logger.error(f"TradeStation order error {e.response.status_code}: {e.response.text}")
                return BrokerOrderResult(success=False, error_message=e.response.text)
            except Exception as e:
                logger.exception("Unexpected error submitting to TradeStation")
                return BrokerOrderResult(success=False, error_message=str(e))

    def _build_order_body(self, order: Order) -> dict:
        account_id = self._resolve_account(order.account)
        is_buy = order.action == OrderAction.BUY

        body = {
            "AccountID":   account_id,
            "Symbol":      order.symbol,
            "TradeAction": "BUY" if is_buy else "SELL",
            "Quantity":    str(int(order.quantity)) if order.instrument_type == InstrumentType.FUTURE else str(order.quantity),
            "OrderType":   _ORDER_TYPE_MAP.get(order.order_type, "Market"),
            "TimeInForce": {"Duration": _TIF_MAP.get(order.time_in_force, "DAY")},
        }

        if order.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and order.price:
            body["LimitPrice"] = str(order.price)

        if order.order_type in (OrderType.STOP, OrderType.STOP_LIMIT) and order.price:
            body["StopPrice"] = str(order.price)

        if order.time_in_force == TimeInForce.GTD and order.expire_at:
            body["TimeInForce"]["Expiration"] = order.expire_at.strftime("%Y-%m-%dT%H:%M:%SZ")

        if order.extended_hours:
            body["AdvancedOptions"] = {"TradeInGtx": True}

        # Bracket orders (SL/TP as OSO)
        legs = []
        if order.stop_loss is not None:
            legs.append({
                "Type": "STOP",
                "Symbol": order.symbol,
                "TradeAction": "SELL" if is_buy else "BUY",
                "Quantity": body["Quantity"],
                "StopPrice": str(order.stop_loss),
                "TimeInForce": {"Duration": "GTC"},
            })
        if order.take_profit is not None:
            legs.append({
                "Type": "LIMIT",
                "Symbol": order.symbol,
                "TradeAction": "SELL" if is_buy else "BUY",
                "Quantity": body["Quantity"],
                "LimitPrice": str(order.take_profit),
                "TimeInForce": {"Duration": "GTC"},
            })
        if legs:
            body["OSOs"] = [{"Type": "NORMAL", "Orders": legs}]

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
            broker          = "tradestation",
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
                    f"{self.base_url}/v3/brokerage/accounts/{account_id}/positions",
                    params={"symbol": symbol},
                )
                resp.raise_for_status()
                data = resp.json()
                positions = data.get("Positions", [])
                for pos in positions:
                    if pos.get("Symbol") == symbol:
                        qty = float(pos.get("Quantity", 0))
                        # Long = positive, Short = negative
                        if pos.get("LongShort", "Long") == "Short":
                            qty = -qty
                        return qty
                return 0.0
            except Exception:
                logger.exception(f"Error fetching TradeStation position for {symbol}")
                return 0.0

    # ── Cancel Order ───────────────────────────────────────────────────────────

    async def cancel_order(self, broker_order_id: str, account: str) -> bool:
        try:
            token = await self._ensure_authenticated()
        except Exception:
            return False

        async with httpx.AsyncClient(
            headers=self._headers(token), timeout=10.0
        ) as client:
            try:
                resp = await client.delete(
                    f"{self.base_url}/v3/orderexecution/orders/{broker_order_id}"
                )
                return resp.status_code in (200, 204)
            except Exception:
                logger.exception(f"Error cancelling TradeStation order {broker_order_id}")
                return False

    # ── Cancel-Replace ─────────────────────────────────────────────────────────

    async def cancel_replace_order(
        self, broker_order_id: str, account: str, new_order: Order
    ) -> BrokerOrderResult:
        try:
            token = await self._ensure_authenticated()
        except Exception as e:
            return BrokerOrderResult(success=False, error_message=f"Auth failed: {e}")

        body = self._build_order_body(new_order)

        async with httpx.AsyncClient(
            headers=self._headers(token), timeout=15.0
        ) as client:
            try:
                resp = await client.put(
                    f"{self.base_url}/v3/orderexecution/orders/{broker_order_id}",
                    json=body,
                )
                if resp.status_code in (200, 201):
                    data = resp.json()
                    orders = data.get("Orders", [data])
                    new_id = str(orders[0].get("OrderID", broker_order_id)) if orders else broker_order_id
                    return BrokerOrderResult(
                        success=True,
                        broker_order_id=new_id,
                        order_open=new_order.order_type != OrderType.MARKET,
                    )
            except Exception:
                pass

        # Fallback: cancel + resubmit
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

        async with httpx.AsyncClient(
            headers=self._headers(token), timeout=10.0
        ) as client:
            try:
                resp = await client.get(
                    f"{self.base_url}/v3/brokerage/orders/{broker_order_id}"
                )
                if resp.status_code == 404:
                    return OrderStatusResult(found=False)
                resp.raise_for_status()
                data = resp.json()

                orders = data.get("Orders", [data])
                if not orders:
                    return OrderStatusResult(found=False)

                order = orders[0]
                status = order.get("StatusDescription", "").upper()

                if status in _FILLED_STATUSES or order.get("Status") in _FILLED_STATUSES:
                    filled = float(order.get("FilledQuantity", 0))
                    avg = float(order.get("AveragePrice", 0)) or None
                    return OrderStatusResult(
                        found=True, is_filled=True,
                        filled_quantity=filled, avg_fill_price=avg,
                    )
                if status in _CANCELLED_STATUSES or order.get("Status") in _CANCELLED_STATUSES:
                    return OrderStatusResult(found=True, is_cancelled=True)
                return OrderStatusResult(found=True, is_open=True)

            except Exception as e:
                logger.exception(f"Error polling TradeStation order {broker_order_id}")
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
                    f"{self.base_url}/v3/brokerage/accounts/{account_id}/positions"
                )
                resp.raise_for_status()
                data = resp.json()
                result = []
                for pos in data.get("Positions", []):
                    qty = float(pos.get("Quantity", 0))
                    if abs(qty) < 1e-9:
                        continue
                    if pos.get("LongShort", "Long") == "Short":
                        qty = -qty

                    symbol        = pos.get("Symbol", "")
                    last_price    = float(pos.get("Last", 0)) or None
                    unrealized    = float(pos.get("UnrealizedProfitLoss", 0)) or None
                    result.append({
                        "symbol":         symbol,
                        "last_price":     last_price,
                        "unrealized_pnl": unrealized,
                    })
                return result
            except Exception:
                logger.exception("Error fetching TradeStation positions P&L")
                return []
