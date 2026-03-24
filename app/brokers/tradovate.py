"""
Tradovate Adapter — futures only.

Tradovate is a futures-only broker. The adapter rejects any non-future instrument type.
Symbol format: root symbol only (e.g. "ES", "NQ", "CL").
Tradovate resolves the front-month contract automatically when given the root.
For a specific contract month, use the full symbol (e.g. "ESZ24").
"""
import httpx
import logging
from datetime import datetime, timezone, timedelta
from app.brokers.base import BrokerBase, BrokerOrderResult, OrderStatusResult
from app.models.order import Order, OrderAction, OrderType, InstrumentType, TimeInForce

logger = logging.getLogger(__name__)

_TIF_MAP = {
    TimeInForce.DAY: "Day",
    TimeInForce.GTC: "GTC",
    TimeInForce.IOC: "IOC",
    TimeInForce.FOK: "FOK",
    TimeInForce.GTD: "Day",  # Tradovate doesn't support GTD; fall back to Day
}


class TradovateBroker(BrokerBase):

    def __init__(
        self,
        username: str,
        password: str,
        app_id: str,
        app_version: str,
        base_url: str,
        instrument_map: dict | None = None,
    ):
        self.username = username
        self.password = password
        self.app_id = app_id
        self.app_version = app_version
        self.base_url = base_url.rstrip("/")
        self.instrument_map: dict[str, dict] = instrument_map or {}
        self._access_token: str | None = None
        self._token_expiry: datetime | None = None

    @classmethod
    def from_credentials(cls, creds: dict) -> "TradovateBroker":
        return cls(
            username=creds["username"],
            password=creds["password"],
            app_id=creds["app_id"],
            app_version=creds.get("app_version", "1.0"),
            base_url=creds.get("base_url", "https://live.tradovateapi.com/v1"),
            instrument_map=creds.get("instrument_map"),
        )

    @classmethod
    def from_settings(cls) -> "TradovateBroker":
        from app.config import get_settings
        s = get_settings()
        return cls(
            username=s.tradovate_username, password=s.tradovate_password,
            app_id=s.tradovate_app_id, app_version=s.tradovate_app_version,
            base_url=s.tradovate_base_url,
        )

    async def _ensure_authenticated(self) -> str:
        if self._access_token and self._token_expiry and datetime.now(timezone.utc) < self._token_expiry:
            return self._access_token
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{self.base_url}/auth/accesstokenrequest",
                json={
                    "name": self.username, "password": self.password,
                    "appId": self.app_id, "appVersion": self.app_version,
                    "cid": 0, "sec": "",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            if "errorText" in data:
                raise RuntimeError(f"Tradovate auth failed: {data['errorText']}")
            self._access_token = data["accessToken"]
            self._token_expiry = datetime.now(timezone.utc) + timedelta(minutes=55)
            return self._access_token

    def _headers(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _get_multiplier(self, symbol: str) -> float:
        """Return the point value for a futures contract from instrument_map or defaults."""
        from app.models.order import DEFAULT_FUTURES_MULTIPLIERS
        instr = self.instrument_map.get(symbol, {})
        return instr.get("multiplier") or DEFAULT_FUTURES_MULTIPLIERS.get(symbol, 1.0)

    async def submit_order(self, order: Order) -> BrokerOrderResult:
        # Tradovate only trades futures
        if order.instrument_type != InstrumentType.FUTURE:
            return BrokerOrderResult(
                success=False,
                error_message=(
                    f"Tradovate only supports futures. "
                    f"Got instrument_type={order.instrument_type.value!r}. "
                    f"Use IBKR or E*Trade for equities."
                ),
            )

        token = await self._ensure_authenticated()

        if order.action == OrderAction.CLOSE:
            return await self._close_position(token, order.account, order.symbol)

        action_map = {OrderAction.BUY: "Buy", OrderAction.SELL: "Sell"}
        order_type_map = {
            OrderType.MARKET: "Market",
            OrderType.LIMIT:  "Limit",
            OrderType.STOP:   "Stop",
        }
        tif = _TIF_MAP.get(order.time_in_force, "Day")

        body: dict = {
            "accountSpec": order.account,
            "symbol": order.symbol,
            "action": action_map[order.action],
            "orderQty": int(order.quantity),  # always whole contracts
            "orderType": order_type_map[order.order_type],
            "timeInForce": tif,
            "isAutomated": True,
        }
        if order.price:
            body["price"] = order.price
        if order.stop_loss is not None:
            body["stopLoss"] = {"stopPrice": order.stop_loss}
        if order.take_profit is not None:
            body["takeProfit"] = {"limitPrice": order.take_profit}

        async with httpx.AsyncClient(headers=self._headers(token), timeout=15.0) as client:
            try:
                resp = await client.post(f"{self.base_url}/order/placeorder", json=body)
                resp.raise_for_status()
                data = resp.json()
                if data.get("failureReason"):
                    return BrokerOrderResult(success=False, error_message=data["failureReason"])
                order_id = str(data.get("orderId", ""))
                is_open = order.order_type != OrderType.MARKET
                return BrokerOrderResult(
                    success=True, broker_order_id=order_id, order_open=is_open
                )
            except httpx.HTTPStatusError as e:
                logger.error(f"Tradovate order error {e.response.status_code}: {e.response.text}")
                return BrokerOrderResult(success=False, error_message=e.response.text)
            except Exception as e:
                logger.exception("Unexpected error submitting to Tradovate")
                return BrokerOrderResult(success=False, error_message=str(e))

    async def _close_position(self, token: str, account: str, symbol: str) -> BrokerOrderResult:
        async with httpx.AsyncClient(headers=self._headers(token), timeout=15.0) as client:
            try:
                resp = await client.post(
                    f"{self.base_url}/order/liquidateposition",
                    json={"accountSpec": account, "symbol": symbol, "isAutomated": True},
                )
                resp.raise_for_status()
                data = resp.json()
                if data.get("failureReason"):
                    return BrokerOrderResult(success=False, error_message=data["failureReason"])
                return BrokerOrderResult(success=True, broker_order_id=str(data.get("orderId", "")))
            except Exception as e:
                return BrokerOrderResult(success=False, error_message=str(e))

    async def get_position(self, account: str, symbol: str) -> float:
        token = await self._ensure_authenticated()
        async with httpx.AsyncClient(headers=self._headers(token), timeout=10.0) as client:
            try:
                resp = await client.get(f"{self.base_url}/position/list")
                resp.raise_for_status()
                for pos in resp.json():
                    if pos.get("symbol") == symbol:
                        return float(pos.get("netPos", 0))
                return 0.0
            except Exception:
                logger.exception(f"Error fetching Tradovate position for {symbol}")
                return 0.0

    async def poll_order_status(
        self, broker_order_id: str, account: str
    ) -> OrderStatusResult:
        """Poll Tradovate order status."""
        token = await self._ensure_authenticated()
        async with httpx.AsyncClient(headers=self._headers(token), timeout=10.0) as client:
            try:
                resp = await client.get(
                    f"{self.base_url}/order/{broker_order_id}"
                )
                if resp.status_code == 404:
                    return OrderStatusResult(found=False)
                resp.raise_for_status()
                order = resp.json()
                status = order.get("orderStatus", "")
                if status == "Filled":
                    filled = float(order.get("filledQty", 0))
                    avg = float(order.get("avgPrice", 0)) or None
                    return OrderStatusResult(
                        found=True, is_filled=True,
                        filled_quantity=filled, avg_fill_price=avg,
                    )
                elif status in ("Cancelled", "Expired", "Rejected"):
                    return OrderStatusResult(found=True, is_cancelled=True)
                else:
                    return OrderStatusResult(found=True, is_open=True)
            except Exception as e:
                logger.exception(f"Error polling Tradovate order {broker_order_id}")
                return OrderStatusResult(found=False, error_message=str(e))

    async def cancel_order(self, broker_order_id: str, account: str) -> bool:
        token = await self._ensure_authenticated()
        async with httpx.AsyncClient(headers=self._headers(token), timeout=10.0) as client:
            try:
                resp = await client.post(
                    f"{self.base_url}/order/cancelorder",
                    json={"orderId": int(broker_order_id)},
                )
                return resp.status_code == 200
            except Exception:
                return False
