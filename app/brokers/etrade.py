"""
E*Trade Adapter — equities only (stocks, ETFs).
Uses OAuth 1.0a. Futures not supported.
"""
import hmac as hmac_lib
import hashlib
import base64
import time
import uuid
import urllib.parse
import httpx
import logging
from app.brokers.base import BrokerBase, BrokerOrderResult, OrderStatusResult
from app.models.order import Order, OrderAction, OrderType, InstrumentType

logger = logging.getLogger(__name__)

# E*Trade security type mapping
_ETRADE_SEC_TYPE: dict[str, str] = {
    "equity": "EQ",
    "forex":  "EQ",  # not really, but not supported — will be caught by schema validation
}


class EtradeBroker(BrokerBase):

    def __init__(
        self,
        consumer_key: str,
        consumer_secret: str,
        oauth_token: str,
        oauth_token_secret: str,
        account_id: str,
        base_url: str,
    ):
        self.consumer_key = consumer_key
        self.consumer_secret = consumer_secret
        self.oauth_token = oauth_token
        self.oauth_token_secret = oauth_token_secret
        self.account_id = account_id
        self.base_url = base_url.rstrip("/")

    @classmethod
    def from_credentials(cls, creds: dict) -> "EtradeBroker":
        return cls(
            consumer_key=creds["consumer_key"],
            consumer_secret=creds["consumer_secret"],
            oauth_token=creds["oauth_token"],
            oauth_token_secret=creds["oauth_token_secret"],
            account_id=creds["account_id"],
            base_url=creds.get("base_url", "https://api.etrade.com"),
        )

    @classmethod
    def from_settings(cls) -> "EtradeBroker":
        from app.config import get_settings
        s = get_settings()
        return cls(
            consumer_key=s.etrade_consumer_key,
            consumer_secret=s.etrade_consumer_secret,
            oauth_token=s.etrade_oauth_token,
            oauth_token_secret=s.etrade_oauth_token_secret,
            account_id=s.etrade_account_id,
            base_url=s.etrade_base_url,
        )

    def _resolve_account(self, account: str) -> str:
        return self.account_id if account == "primary" else account

    def _oauth_header(self, method: str, url: str, params: dict | None = None) -> str:
        oauth_params = {
            "oauth_consumer_key": self.consumer_key,
            "oauth_token": self.oauth_token,
            "oauth_signature_method": "HMAC-SHA1",
            "oauth_timestamp": str(int(time.time())),
            "oauth_nonce": uuid.uuid4().hex,
            "oauth_version": "1.0",
        }
        all_params = {**oauth_params, **(params or {})}
        param_string = "&".join(
            f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(str(v), safe='')}"
            for k, v in sorted(all_params.items())
        )
        base_string = "&".join([
            method.upper(),
            urllib.parse.quote(url, safe=""),
            urllib.parse.quote(param_string, safe=""),
        ])
        signing_key = (
            f"{urllib.parse.quote(self.consumer_secret, safe='')}"
            f"&{urllib.parse.quote(self.oauth_token_secret, safe='')}"
        )
        signature = base64.b64encode(
            hmac_lib.new(signing_key.encode(), base_string.encode(), hashlib.sha1).digest()
        ).decode()
        oauth_params["oauth_signature"] = signature
        return "OAuth " + ", ".join(
            f'{urllib.parse.quote(k, safe="")}="{urllib.parse.quote(str(v), safe="")}"'
            for k, v in sorted(oauth_params.items())
        )

    async def submit_order(self, order: Order) -> BrokerOrderResult:
        # E*Trade supports equities only
        if order.instrument_type == InstrumentType.FUTURE:
            return BrokerOrderResult(
                success=False,
                error_message="E*Trade does not support futures. Use Tradovate or IBKR.",
            )

        account_id = self._resolve_account(order.account)

        if order.action == OrderAction.CLOSE:
            return await self._close_position(account_id, order.symbol)

        url = f"{self.base_url}/v1/accounts/{account_id}/orders/place"
        action_map = {OrderAction.BUY: "BUY", OrderAction.SELL: "SELL"}
        order_type_map = {
            OrderType.MARKET: "MARKET",
            OrderType.LIMIT:  "LIMIT",
            OrderType.STOP:   "STOP",
        }

        # Extended hours session
        if order.extended_hours:
            market_session = "EXTENDED"
            order_term = "GOOD_FOR_DAY"  # extended hours always DAY
        else:
            market_session = "REGULAR"
            order_term = "GOOD_FOR_DAY"

        body = {
            "PlaceOrderRequest": {
                "orderType": order_type_map[order.order_type],
                "clientOrderId": uuid.uuid4().hex[:20],
                "Order": [{
                    "allOrNone": "false",
                    "price": order.price or 0,
                    "quantity": int(order.quantity),
                    "orderTerm": order_term,
                    "marketSession": market_session,
                    "stopPrice": order.price if order.order_type == OrderType.STOP else 0,
                    "Instrument": [{
                        "Product": {
                            "securityType": "EQ",
                            "symbol": order.symbol,
                        },
                        "orderAction": action_map[order.action],
                        "quantityType": "QUANTITY",
                        "quantity": int(order.quantity),
                    }],
                }],
            }
        }

        headers = {
            "Authorization": self._oauth_header("POST", url),
            "Content-Type": "application/json",
        }
        import json as _json
        body_str = _json.dumps(body, default=str)
        async with httpx.AsyncClient(headers=headers, timeout=15.0) as client:
            try:
                resp = await client.post(url, json=body)
                resp.raise_for_status()
                data = resp.json()
                order_id = str(
                    data.get("PlaceOrderResponse", {}).get("OrderIds", {}).get("orderId", "")
                )
                is_open = order.order_type != OrderType.MARKET
                return BrokerOrderResult(
                    success=True, broker_order_id=order_id, order_open=is_open,
                    broker_request=body_str,
                    broker_response=resp.text,
                )
            except httpx.HTTPStatusError as e:
                logger.error(f"E*Trade order error {e.response.status_code}: {e.response.text}")
                return BrokerOrderResult(success=False, error_message=e.response.text)
            except Exception as e:
                logger.exception("Unexpected error submitting to E*Trade")
                return BrokerOrderResult(success=False, error_message=str(e))

    async def _close_position(self, account_id: str, symbol: str) -> BrokerOrderResult:
        qty = await self.get_position(account_id, symbol)
        if abs(qty) < 1e-9:
            return BrokerOrderResult(success=True, filled_quantity=0)
        from app.models.order import Order as OrderModel
        close_order = OrderModel(
            broker="etrade", account=account_id, symbol=symbol,
            instrument_type=InstrumentType.EQUITY,
            action=OrderAction.SELL if qty > 0 else OrderAction.BUY,
            order_type=OrderType.MARKET, quantity=abs(qty),
        )
        return await self.submit_order(close_order)

    async def get_position(self, account: str, symbol: str) -> float:
        account_id = self._resolve_account(account)
        url = f"{self.base_url}/v1/accounts/{account_id}/portfolio"
        headers = {"Authorization": self._oauth_header("GET", url)}
        async with httpx.AsyncClient(headers=headers, timeout=10.0) as client:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                for portfolio in resp.json().get("PortfolioResponse", {}).get("AccountPortfolio", []):
                    for pos in portfolio.get("Position", []):
                        if pos.get("Product", {}).get("symbol", "").upper() == symbol.upper():
                            qty = float(pos.get("quantity", 0))
                            return qty if pos.get("positionType") == "LONG" else -qty
                return 0.0
            except Exception:
                logger.exception(f"Error fetching E*Trade position for {symbol}")
                return 0.0

    async def get_open_positions_pnl(self, account: str) -> list[dict]:
        """
        Fetch live unrealized P&L from E*Trade portfolio endpoint.
        E*Trade returns totalGain (unrealized P&L) and Quick.lastTrade directly.
        """
        account_id = self._resolve_account(account)
        url = f"{self.base_url}/v1/accounts/{account_id}/portfolio"
        headers = {"Authorization": self._oauth_header("GET", url)}
        async with httpx.AsyncClient(headers=headers, timeout=10.0) as client:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                result = []
                portfolios = (
                    resp.json()
                    .get("PortfolioResponse", {})
                    .get("AccountPortfolio", [])
                )
                for portfolio in portfolios:
                    for pos in portfolio.get("Position", []):
                        symbol = pos.get("Product", {}).get("symbol", "")
                        qty    = float(pos.get("quantity", 0))
                        if abs(qty) < 1e-9:
                            continue

                        # E*Trade provides unrealized gain directly
                        unrealized_pnl = float(pos.get("totalGain", 0) or 0)

                        # Last trade price from Quick section
                        last_price = None
                        quick = pos.get("Quick", {})
                        if quick.get("lastTrade"):
                            last_price = float(quick["lastTrade"])

                        result.append({
                            "symbol":         symbol,
                            "last_price":     last_price,
                            "unrealized_pnl": unrealized_pnl,
                        })
                return result
            except Exception:
                logger.exception("Error fetching E*Trade open positions P&L")
                return []

    async def poll_order_status(
        self, broker_order_id: str, account: str
    ) -> OrderStatusResult:
        """Poll E*Trade order status."""
        account_id = self._resolve_account(account)
        url = f"{self.base_url}/v1/accounts/{account_id}/orders/{broker_order_id}"
        headers = {"Authorization": self._oauth_header("GET", url)}
        async with httpx.AsyncClient(headers=headers, timeout=10.0) as client:
            try:
                resp = await client.get(url)
                if resp.status_code == 404:
                    return OrderStatusResult(found=False)
                resp.raise_for_status()
                data = resp.json()
                order_resp = data.get("OrdersResponse", {})
                orders = order_resp.get("Order", [])
                if not orders:
                    return OrderStatusResult(found=False)
                order = orders[0]
                status = order.get("orderStatus", "")
                if status in ("EXECUTED",):
                    filled = float(order.get("totalOrderValue", 0))
                    return OrderStatusResult(
                        found=True, is_filled=True, filled_quantity=filled
                    )
                elif status in ("CANCELLED", "EXPIRED", "REJECTED"):
                    return OrderStatusResult(found=True, is_cancelled=True)
                else:
                    return OrderStatusResult(found=True, is_open=True)
            except Exception as e:
                logger.exception(f"Error polling E*Trade order {broker_order_id}")
                return OrderStatusResult(found=False, error_message=str(e))

    async def cancel_order(self, broker_order_id: str, account: str) -> bool:
        account_id = self._resolve_account(account)
        url = f"{self.base_url}/v1/accounts/{account_id}/orders/cancel"
        headers = {
            "Authorization": self._oauth_header("PUT", url),
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(headers=headers, timeout=10.0) as client:
            try:
                resp = await client.put(
                    url, json={"CancelOrderRequest": {"orderId": broker_order_id}}
                )
                return resp.status_code == 200
            except Exception:
                return False
