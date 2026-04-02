"""
Rithmic R|Web API Adapter.

Uses the Rithmic R|Web REST API (requires separate approval from Rithmic).

Credentials required:
    username:    Rithmic username
    password:    Rithmic password
    app_key:     API key issued by Rithmic for your application
    base_url:    API base URL
                 Sim:  https://paper-rithmic-rapi.rithmic.com
                 Live: https://rithmic-rapi.rithmic.com  (confirm with Rithmic)
    system_name: Rithmic system name (e.g. "Rithmic Paper Trading")
                 Sim:  "Rithmic Paper Trading"
                 Live: "Rithmic 01" (or as assigned by Rithmic)

IMPORTANT — field names and endpoint paths:
    The R|Web API spec is not fully public. The paths, request field names,
    and response shapes below are based on available documentation and
    community knowledge. Anything marked with # VERIFY may need adjustment
    once you have credentials and can test against the actual API.
    Check https://rkindapi.rithmic.com/docs if Rithmic has provided you
    documentation access.
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

# Rithmic order type mapping  # VERIFY against your API docs
_ORDER_TYPE_MAP = {
    OrderType.MARKET:     "MKT",
    OrderType.LIMIT:      "LMT",
    OrderType.STOP:       "STP",
    OrderType.STOP_LIMIT: "STPLMT",
}

# Rithmic time-in-force mapping  # VERIFY
_TIF_MAP = {
    TimeInForce.DAY: "DAY",
    TimeInForce.GTC: "GTC",
    TimeInForce.IOC: "IOC",
    TimeInForce.FOK: "FOK",
    TimeInForce.GTD: "GTD",
}

# Rithmic order status values  # VERIFY — these are common names but may differ
_FILLED_STATUSES    = {"FILLED", "COMPLETE", "FILL"}
_CANCELLED_STATUSES = {"CANCELLED", "CANCELED", "REJECTED", "EXPIRED"}
_OPEN_STATUSES      = {"OPEN", "WORKING", "PENDING", "SUBMITTED", "PARTIAL"}


class RithmicBroker(BrokerBase):
    """
    Rithmic R|Web API adapter.
    Futures only — Rithmic is a futures/options execution platform.
    """

    def __init__(
        self,
        username: str,
        password: str,
        app_key: str,
        system_name: str,
        base_url: str,
    ):
        self.username    = username
        self.password    = password
        self.app_key     = app_key
        self.system_name = system_name
        self.base_url    = base_url.rstrip("/")

        self._access_token:  str | None      = None
        self._token_expiry:  datetime | None = None

    @classmethod
    def from_credentials(cls, creds: dict) -> "RithmicBroker":
        return cls(
            username    = creds["username"],
            password    = creds["password"],
            app_key     = creds["app_key"],
            system_name = creds.get("system_name", "Rithmic Paper Trading"),
            base_url    = creds.get("base_url", "https://paper-rithmic-rapi.rithmic.com"),
        )

    @classmethod
    def from_settings(cls) -> "RithmicBroker":
        from app.config import get_settings
        s = get_settings()
        return cls(
            username    = s.rithmic_username,
            password    = s.rithmic_password,
            app_key     = s.rithmic_app_key,
            system_name = s.rithmic_system_name,
            base_url    = s.rithmic_base_url,
        )

    # ── Authentication ─────────────────────────────────────────────────────────

    async def _ensure_authenticated(self) -> str:
        """
        Return a valid bearer token, refreshing if expired.

        # VERIFY: endpoint path, request body field names, response field names.
        Common patterns for R|Web:
            POST /api/auth/login  or  POST /api/login  or  POST /login
        """
        if (self._access_token
                and self._token_expiry
                and datetime.now(timezone.utc) < self._token_expiry):
            return self._access_token

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{self.base_url}/api/auth/login",  # VERIFY path
                json={
                    "user":        self.username,    # VERIFY field name (may be "username")
                    "password":    self.password,
                    "appKey":      self.app_key,     # VERIFY field name
                    "systemName":  self.system_name, # VERIFY field name
                },
            )
            resp.raise_for_status()
            data = resp.json()

            # VERIFY: response field names for token and expiry
            # Common patterns: "accessToken", "access_token", "token"
            token = (
                data.get("accessToken")
                or data.get("access_token")
                or data.get("token")
            )
            if not token:
                raise RuntimeError(
                    f"Rithmic auth response did not contain a token. "
                    f"Response keys: {list(data.keys())}. "
                    f"Check your credentials and API key."
                )

            self._access_token = token
            # Tokens typically last 1 hour — refresh after 55 minutes
            # VERIFY: check if the response includes an expiry field
            expires_in = data.get("expiresIn", 3600)  # seconds
            self._token_expiry = (
                datetime.now(timezone.utc) + timedelta(seconds=int(expires_in) - 60)
            )
            logger.info("Rithmic authentication successful")
            return self._access_token

    def _auth_headers(self, token: str) -> dict:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
        }

    # ── Order Submission ───────────────────────────────────────────────────────

    async def submit_order(self, order: Order) -> BrokerOrderResult:
        # Rithmic only supports futures
        if order.instrument_type not in (InstrumentType.FUTURE,):
            return BrokerOrderResult(
                success=False,
                error_message=(
                    f"Rithmic only supports futures. "
                    f"Got instrument_type={order.instrument_type.value!r}."
                ),
            )

        if order.action == OrderAction.CLOSE:
            return await self._close_position(order.account, order.symbol)

        try:
            token = await self._ensure_authenticated()
        except Exception as e:
            return BrokerOrderResult(success=False, error_message=f"Auth failed: {e}")

        body = self._build_order_body(order)

        async with httpx.AsyncClient(
            headers=self._auth_headers(token), timeout=15.0
        ) as client:
            try:
                resp = await client.post(
                    f"{self.base_url}/api/orders",  # VERIFY path
                    json=body,
                )
                resp.raise_for_status()
                data = resp.json()

                # VERIFY: response field name for the order ID
                # Common patterns: "orderId", "order_id", "basketId", "id"
                order_id = (
                    str(data.get("orderId", ""))
                    or str(data.get("order_id", ""))
                    or str(data.get("basketId", ""))
                    or str(data.get("id", ""))
                )

                if not order_id:
                    return BrokerOrderResult(
                        success=False,
                        error_message=(
                            f"Rithmic order response missing order ID. "
                            f"Response: {data}"
                        ),
                    )

                is_open = order.order_type != OrderType.MARKET
                return BrokerOrderResult(
                    success=True,
                    broker_order_id=order_id,
                    order_open=is_open,
                )

            except httpx.HTTPStatusError as e:
                logger.error(
                    f"Rithmic order error {e.response.status_code}: {e.response.text}"
                )
                return BrokerOrderResult(
                    success=False, error_message=e.response.text
                )
            except Exception as e:
                logger.exception("Unexpected error submitting to Rithmic")
                return BrokerOrderResult(success=False, error_message=str(e))

    def _build_order_body(self, order: Order) -> dict:
        """
        Build the Rithmic order request body.
        # VERIFY: all field names against your API documentation.
        """
        action_map = {OrderAction.BUY: "BUY", OrderAction.SELL: "SELL"}
        body = {
            # VERIFY field names:
            "symbol":      order.symbol,
            "exchange":    order.exchange or "CME",  # VERIFY: Rithmic may require exchange
            "quantity":    int(order.quantity),       # futures = whole contracts
            "side":        action_map[order.action],  # VERIFY: may be "buySell" or "transactionType"
            "orderType":   _ORDER_TYPE_MAP[order.order_type],  # VERIFY
            "timeInForce": _TIF_MAP.get(order.time_in_force, "DAY"),  # VERIFY
            "accountId":   order.account,             # VERIFY field name
        }

        if order.price and order.order_type in (OrderType.LIMIT, OrderType.STOP, OrderType.STOP_LIMIT):
            body["price"] = order.price  # VERIFY field name (may be "limitPrice"/"stopPrice")

        if order.stop_loss is not None:
            body["stopLoss"] = order.stop_loss  # VERIFY

        if order.take_profit is not None:
            body["takeProfit"] = order.take_profit  # VERIFY

        if order.time_in_force == TimeInForce.GTD and order.expire_at:
            body["gtdTime"] = order.expire_at.isoformat()  # VERIFY format

        if order.comment:
            body["userTag"] = order.comment  # VERIFY field name

        return body

    # ── Close Position ─────────────────────────────────────────────────────────

    async def _close_position(self, account: str, symbol: str) -> BrokerOrderResult:
        """Close by fetching current position and submitting opposing market order."""
        qty = await self.get_position(account, symbol)
        if abs(qty) < 1e-9:
            return BrokerOrderResult(
                success=True, filled_quantity=0,
                error_message="Position already flat"
            )

        close_order = Order(
            tenant_id       = 0,  # not used in adapter
            broker          = "rithmic",
            account         = account,
            symbol          = symbol,
            instrument_type = InstrumentType.FUTURE,
            action          = OrderAction.SELL if qty > 0 else OrderAction.BUY,
            order_type      = OrderType.MARKET,
            quantity        = abs(qty),
            time_in_force   = TimeInForce.DAY,
        )
        return await self.submit_order(close_order)

    # ── Get Position ───────────────────────────────────────────────────────────

    async def get_position(self, account: str, symbol: str) -> float:
        """
        Return net position quantity for a symbol.
        # VERIFY: endpoint path and response field names.
        """
        try:
            token = await self._ensure_authenticated()
        except Exception:
            return 0.0

        async with httpx.AsyncClient(
            headers=self._auth_headers(token), timeout=10.0
        ) as client:
            try:
                # VERIFY path — may be /api/positions or /api/accounts/{id}/positions
                resp = await client.get(
                    f"{self.base_url}/api/positions",
                    params={"accountId": account},  # VERIFY param name
                )
                resp.raise_for_status()
                positions = resp.json()

                # VERIFY: response may be wrapped e.g. {"positions": [...]}
                if isinstance(positions, dict):
                    positions = (
                        positions.get("positions")
                        or positions.get("data")
                        or []
                    )

                for pos in positions:
                    # VERIFY field names: symbol, netPos, size, quantity
                    pos_symbol = (
                        pos.get("symbol")
                        or pos.get("instrumentSymbol")
                        or ""
                    )
                    if pos_symbol.upper() == symbol.upper():
                        qty = float(
                            pos.get("netPos")
                            or pos.get("netPosition")
                            or pos.get("size")
                            or pos.get("quantity")
                            or 0
                        )
                        return qty
                return 0.0
            except Exception:
                logger.exception(f"Error fetching Rithmic position for {symbol}")
                return 0.0

    # ── Cancel Order ───────────────────────────────────────────────────────────

    async def cancel_order(self, broker_order_id: str, account: str) -> bool:
        """
        Cancel an open order.
        # VERIFY: endpoint path and method (DELETE vs POST with action field).
        """
        try:
            token = await self._ensure_authenticated()
        except Exception:
            return False

        async with httpx.AsyncClient(
            headers=self._auth_headers(token), timeout=10.0
        ) as client:
            try:
                # VERIFY: may be DELETE /api/orders/{id} or POST /api/orders/{id}/cancel
                resp = await client.delete(
                    f"{self.base_url}/api/orders/{broker_order_id}",
                    params={"accountId": account},  # VERIFY
                )
                return resp.status_code in (200, 204)
            except Exception:
                logger.exception(f"Error cancelling Rithmic order {broker_order_id}")
                return False

    # ── Cancel-Replace ─────────────────────────────────────────────────────────

    async def cancel_replace_order(
        self, broker_order_id: str, account: str, new_order: Order
    ) -> BrokerOrderResult:
        """
        Cancel and replace an open order.
        Attempts native modify first; falls back to cancel+resubmit.
        # VERIFY: whether Rithmic R|Web supports native order modification.
        """
        try:
            token = await self._ensure_authenticated()
        except Exception as e:
            return BrokerOrderResult(success=False, error_message=f"Auth failed: {e}")

        body = self._build_order_body(new_order)

        async with httpx.AsyncClient(
            headers=self._auth_headers(token), timeout=15.0
        ) as client:
            try:
                # VERIFY: may be PUT /api/orders/{id} or PATCH /api/orders/{id}
                resp = await client.put(
                    f"{self.base_url}/api/orders/{broker_order_id}",
                    json=body,
                )
                if resp.status_code in (200, 201):
                    data = resp.json()
                    new_id = (
                        str(data.get("orderId", ""))
                        or str(data.get("order_id", ""))
                        or broker_order_id
                    )
                    return BrokerOrderResult(
                        success=True,
                        broker_order_id=new_id,
                        order_open=new_order.order_type != OrderType.MARKET,
                    )
            except Exception:
                pass

        # Fallback: cancel then resubmit
        logger.info(
            f"Rithmic native modify failed for {broker_order_id} — "
            f"falling back to cancel+resubmit"
        )
        cancelled = await self.cancel_order(broker_order_id, account)
        if not cancelled:
            return BrokerOrderResult(
                success=False,
                error_message=f"Cancel of order {broker_order_id} failed before replace"
            )
        return await self.submit_order(new_order)

    # ── Poll Order Status ──────────────────────────────────────────────────────

    async def poll_order_status(
        self, broker_order_id: str, account: str
    ) -> OrderStatusResult:
        """
        Poll current status of an open order.
        # VERIFY: endpoint path and status field name/values.
        """
        try:
            token = await self._ensure_authenticated()
        except Exception:
            return OrderStatusResult(found=False)

        async with httpx.AsyncClient(
            headers=self._auth_headers(token), timeout=10.0
        ) as client:
            try:
                # VERIFY path
                resp = await client.get(
                    f"{self.base_url}/api/orders/{broker_order_id}",
                )
                if resp.status_code == 404:
                    return OrderStatusResult(found=False)
                resp.raise_for_status()
                data = resp.json()

                # VERIFY field names: status, filledQty, avgFillPrice
                status = (
                    (data.get("status") or data.get("orderStatus") or "")
                    .upper()
                    .strip()
                )

                if status in _FILLED_STATUSES:
                    filled = float(
                        data.get("filledQty")
                        or data.get("filled_quantity")
                        or data.get("fillQty")
                        or 0
                    )
                    avg_price = float(
                        data.get("avgFillPrice")
                        or data.get("avg_fill_price")
                        or data.get("averageFillPrice")
                        or 0
                    ) or None
                    return OrderStatusResult(
                        found=True,
                        is_filled=True,
                        filled_quantity=filled,
                        avg_fill_price=avg_price,
                    )

                if status in _CANCELLED_STATUSES:
                    return OrderStatusResult(found=True, is_cancelled=True)

                return OrderStatusResult(found=True, is_open=True)

            except Exception as e:
                logger.exception(
                    f"Error polling Rithmic order status for {broker_order_id}"
                )
                return OrderStatusResult(found=False, error_message=str(e))

    # ── Live P&L Polling ───────────────────────────────────────────────────────

    async def get_open_positions_pnl(self, account: str) -> list[dict]:
        """
        Fetch live unrealized P&L for all open positions.
        # VERIFY: endpoint, field names, and whether Rithmic returns P&L directly
        or whether we need to calculate it from avg_price + last_price.
        """
        try:
            token = await self._ensure_authenticated()
        except Exception:
            return []

        async with httpx.AsyncClient(
            headers=self._auth_headers(token), timeout=10.0
        ) as client:
            try:
                resp = await client.get(
                    f"{self.base_url}/api/positions",  # VERIFY path
                    params={"accountId": account},
                )
                resp.raise_for_status()
                data = resp.json()

                if isinstance(data, dict):
                    data = data.get("positions") or data.get("data") or []

                result = []
                for pos in data:
                    qty = float(
                        pos.get("netPos")
                        or pos.get("netPosition")
                        or pos.get("size")
                        or 0
                    )
                    if abs(qty) < 1e-9:
                        continue

                    symbol = (
                        pos.get("symbol")
                        or pos.get("instrumentSymbol")
                        or ""
                    )

                    # VERIFY field names for P&L and last price
                    unrealized_pnl = float(
                        pos.get("openPnl")
                        or pos.get("unrealizedPnl")
                        or pos.get("openPL")
                        or pos.get("unrealized_pnl")
                        or 0
                    ) or None

                    last_price = float(
                        pos.get("lastPrice")
                        or pos.get("last_price")
                        or pos.get("markPrice")
                        or 0
                    ) or None

                    avg_price = float(
                        pos.get("avgPrice")
                        or pos.get("averagePrice")
                        or pos.get("avg_price")
                        or 0
                    ) or None

                    # If Rithmic doesn't return P&L directly, calculate it
                    if unrealized_pnl is None and last_price and avg_price:
                        root = ''.join(c for c in symbol if c.isalpha())
                        mult = (
                            DEFAULT_FUTURES_MULTIPLIERS.get(symbol)
                            or DEFAULT_FUTURES_MULTIPLIERS.get(root, 1.0)
                        )
                        unrealized_pnl = (last_price - avg_price) * qty * mult

                    result.append({
                        "symbol":         symbol,
                        "symbol_root":    ''.join(c for c in symbol if c.isalpha()),
                        "last_price":     last_price,
                        "unrealized_pnl": unrealized_pnl,
                    })

                return result

            except Exception:
                logger.exception("Error fetching Rithmic open positions P&L")
                return []
