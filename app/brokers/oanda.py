import httpx
from app.brokers.base import BrokerBase, BrokerOrderResult, OrderStatusResult
from app.models.order import Order, OrderAction, OrderType, InstrumentType
import logging

logger = logging.getLogger(__name__)


def _fmt_price(symbol: str, price: float) -> str:
    """
    Format a price to the correct decimal precision for Oanda.
    JPY pairs: 3dp  (e.g. 149.500)
    Most others: 5dp (e.g. 1.07500)
    """
    sym = symbol.upper().replace("_", "")
    # JPY is quote currency for pairs like USD_JPY, EUR_JPY, GBP_JPY
    if len(sym) >= 6 and sym[3:6] == "JPY":
        return f"{round(price, 3):.3f}"
    return f"{round(price, 5):.5f}"


class OandaBroker(BrokerBase):

    def __init__(
        self,
        api_key: str,
        account_id: str,
        base_url: str,
        fifo_randomize: bool = False,
        fifo_max_offset: int = 3,
        account_alias: str | None = None,
    ):
        self.api_key         = api_key
        self.account_id      = account_id
        self.base_url        = base_url.rstrip("/")
        self.fifo_randomize  = fifo_randomize
        self.fifo_max_offset = fifo_max_offset
        # Relay account alias — used to look up the stream manager.
        # Falls back to account_id if not provided (e.g. in from_settings / tests).
        self.account_alias   = account_alias or account_id
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept-Datetime-Format": "RFC3339",
        }

    @classmethod
    def from_credentials(cls, creds: dict) -> "OandaBroker":
        return cls(
            api_key         = creds["api_key"],
            account_id      = creds["account_id"],
            base_url        = creds.get("base_url", "https://api-fxtrade.oanda.com/v3"),
            fifo_randomize  = creds.get("fifo_randomize", False),
            fifo_max_offset = int(creds.get("fifo_max_offset", 3)),
            account_alias   = creds.get("account_alias"),
        )

    @classmethod
    def from_settings(cls) -> "OandaBroker":
        """Convenience constructor for single-tenant / dev use."""
        from app.config import get_settings
        s = get_settings()
        return cls(api_key=s.oanda_api_key, account_id=s.oanda_account_id, base_url=s.oanda_base_url)

    @staticmethod
    def _extract_order_id(tx: dict) -> str | None:
        return tx.get("orderID") or tx.get("id")

    def _resolve_account(self, account: str) -> str:
        return self.account_id if account == "primary" else account

    def _build_order_body(self, order: Order) -> dict:
        from app.models.order import TimeInForce

        # broker_quantity is pre-set by order_processor._resolve_fifo_quantity()
        # for FIFO-enabled accounts before submission. Use it if available,
        # otherwise fall back to the requested quantity (non-FIFO accounts).
        if order.broker_quantity is not None:
            qty = int(order.broker_quantity)
        else:
            qty = int(order.quantity)
            order.broker_quantity = float(qty)

        units = str(qty)
        if order.action == OrderAction.SELL:
            units = f"-{units}"
        elif order.action == OrderAction.CLOSE:
            return {}

        body: dict = {"order": {"instrument": order.symbol, "units": units}}
        tif = order.time_in_force if order.time_in_force else TimeInForce.GTC

        # positionFill DEFAULT — lot size randomization handles FIFO uniqueness.

        # Tag every order with a unique clientTradeID so Oanda can identify
        # individual legs when pyramiding same-size positions (FIFO avoidance).
        # Uses the relay's internal order ID as the tag — stored on the order row
        # so we can reference specific trades on close if needed.
        if order.id:
            body["order"]["clientExtensions"] = {
                "id":      f"relay_{order.id}",
                "comment": order.comment or f"relay_{order.id}",
            }
            body["order"]["tradeClientExtensions"] = {
                "id":      f"relay_{order.id}",
                "comment": order.comment or f"relay_{order.id}",
            }

        if order.order_type == OrderType.MARKET:
            body["order"]["type"] = "MARKET"
            body["order"]["timeInForce"] = tif if tif in (TimeInForce.FOK, TimeInForce.IOC) else TimeInForce.FOK
        elif order.order_type == OrderType.LIMIT:
            body["order"]["type"] = "LIMIT"
            body["order"]["price"] = _fmt_price(order.symbol, order.price)
            body["order"]["timeInForce"] = tif
            if tif == TimeInForce.GTD and order.expire_at:
                body["order"]["gtdTime"] = order.expire_at.strftime("%Y-%m-%dT%H:%M:%S.000000Z")
        elif order.order_type == OrderType.STOP:
            body["order"]["type"] = "STOP"
            body["order"]["price"] = _fmt_price(order.symbol, order.price)
            body["order"]["timeInForce"] = tif
            if tif == TimeInForce.GTD and order.expire_at:
                body["order"]["gtdTime"] = order.expire_at.strftime("%Y-%m-%dT%H:%M:%S.000000Z")

        # trailing_distance is intentionally NOT sent to Oanda at order time.
        # The stream manager places the native trailing stop only when the
        # trail_trigger price is hit (via TrailTrigger row + _place_trailing_stop).
        if order.stop_loss is not None:
            body["order"]["stopLossOnFill"] = {
                "price": _fmt_price(order.symbol, order.stop_loss),
                "timeInForce": "GTC",
            }
        if order.take_profit is not None:
            body["order"]["takeProfitOnFill"] = {
                "price": _fmt_price(order.symbol, order.take_profit),
                "timeInForce": "GTC",
            }
        return body

    async def submit_order(self, order: Order) -> BrokerOrderResult:
        account_id = self._resolve_account(order.account)
        # Oanda only supports forex and CFDs
        if order.instrument_type not in (InstrumentType.FOREX, InstrumentType.CFD):
            return BrokerOrderResult(
                success=False,
                error_message=(
                    f"Oanda does not support instrument_type={order.instrument_type.value!r}. "
                    f"Use IBKR or E*Trade for equities, Tradovate/IBKR for futures."
                ),
            )
        if order.action == OrderAction.CLOSE:
            return await self._close_position(account_id, order.symbol)

        import json as _json
        body = self._build_order_body(order)
        body_str = _json.dumps(body, default=str)
        async with httpx.AsyncClient(headers=self.headers, timeout=15.0) as client:
            try:
                resp = await client.post(f"{self.base_url}/accounts/{account_id}/orders", json=body)
                resp.raise_for_status()
                data = resp.json()
                if "orderFillTransaction" in data:
                    fill_tx = data["orderFillTransaction"]
                    fill_price = fill_tx.get("price")
                    # Extract clientTradeID from the trade opened by this fill
                    client_trade_id = None
                    trades_opened = fill_tx.get("tradeOpened", {})
                    if trades_opened:
                        client_ext = trades_opened.get("clientExtensions", {})
                        client_trade_id = client_ext.get("id")
                    return BrokerOrderResult(
                        success=True,
                        broker_order_id=self._extract_order_id(fill_tx),
                        filled_quantity=abs(float(fill_tx.get("units", order.quantity))),
                        avg_fill_price=float(fill_price) if fill_price else None,
                        client_trade_id=client_trade_id,
                        broker_request=body_str,
                        broker_response=resp.text,
                    )
                elif "orderCancelTransaction" in data:
                    reason = data["orderCancelTransaction"].get("reason", "CANCELLED")
                    return BrokerOrderResult(success=False, error_message=f"Order cancelled by Oanda: {reason}",
                                            broker_request=body_str, broker_response=resp.text)
                elif "orderCreateTransaction" in data:
                    create_tx = data["orderCreateTransaction"]
                    return BrokerOrderResult(
                        success=True,
                        broker_order_id=self._extract_order_id(create_tx),
                        filled_quantity=0.0,
                        order_open=True,
                        broker_request=body_str,
                        broker_response=resp.text,
                    )
                else:
                    logger.error(f"Unexpected Oanda response: {data}")
                    return BrokerOrderResult(success=False, error_message=str(data), broker_request=body_str, broker_response=resp.text)
            except httpx.HTTPStatusError as e:
                logger.error(f"Oanda order error {e.response.status_code}: {e.response.text}")
                return BrokerOrderResult(
                    success=False,
                    error_message=e.response.text,
                    broker_request=body_str,
                    broker_response=e.response.text,
                )
            except Exception as e:
                logger.exception("Unexpected error submitting to Oanda")
                return BrokerOrderResult(
                    success=False,
                    error_message=str(e),
                    broker_request=body_str,
                )

    async def cancel_replace_order(self, broker_order_id: str, account: str, new_order: Order) -> BrokerOrderResult:
        account_id = self._resolve_account(account)
        order_body = self._build_order_body(new_order).get("order", {})
        async with httpx.AsyncClient(headers=self.headers, timeout=15.0) as client:
            try:
                resp = await client.put(
                    f"{self.base_url}/accounts/{account_id}/orders/{broker_order_id}",
                    json={"order": order_body},
                )
                resp.raise_for_status()
                data = resp.json()
                if "orderCreateTransaction" in data:
                    create_tx = data["orderCreateTransaction"]
                    return BrokerOrderResult(
                        success=True, broker_order_id=self._extract_order_id(create_tx),
                        filled_quantity=0.0, order_open=True,
                    )
                elif "orderFillTransaction" in data:
                    fill_tx = data["orderFillTransaction"]
                    return BrokerOrderResult(
                        success=True, broker_order_id=self._extract_order_id(fill_tx),
                        filled_quantity=abs(float(fill_tx.get("units", new_order.quantity))),
                        avg_fill_price=float(fill_tx["price"]) if fill_tx.get("price") else None,
                    )
                else:
                    return BrokerOrderResult(success=False, error_message=str(data))
            except httpx.HTTPStatusError as e:
                logger.error(f"Oanda cancel-replace error {e.response.status_code}: {e.response.text}")
                return BrokerOrderResult(success=False, error_message=e.response.text)

    async def _close_position(self, account_id: str, symbol: str) -> BrokerOrderResult:
        async with httpx.AsyncClient(headers=self.headers, timeout=15.0) as client:
            try:
                pos_resp = await client.get(f"{self.base_url}/accounts/{account_id}/positions/{symbol}")
                if pos_resp.status_code == 404:
                    return BrokerOrderResult(success=True, filled_quantity=0.0, error_message="No open position")
                pos_resp.raise_for_status()
                pos_data = pos_resp.json().get("position", {})
                long_units = float(pos_data.get("long", {}).get("units", 0))
                short_units = float(pos_data.get("short", {}).get("units", 0))
            except Exception as e:
                return BrokerOrderResult(success=False, error_message=f"Failed to fetch position: {e}")

            if long_units == 0 and short_units == 0:
                return BrokerOrderResult(success=True, filled_quantity=0.0, error_message="Position already flat")

            close_body = {
                "longUnits": "ALL" if long_units > 0 else "NONE",
                "shortUnits": "ALL" if short_units < 0 else "NONE",
            }
            try:
                resp = await client.put(
                    f"{self.base_url}/accounts/{account_id}/positions/{symbol}/close",
                    json=close_body,
                )
                resp.raise_for_status()
                data = resp.json()
                tx = data.get("longOrderFillTransaction") or data.get("shortOrderFillTransaction") or {}
                filled = abs(float(tx.get("units", 0))) if tx else abs(long_units) + abs(short_units)
                fill_price = tx.get("price")
                return BrokerOrderResult(
                    success=True,
                    broker_order_id=self._extract_order_id(tx) if tx else None,
                    filled_quantity=filled,
                    avg_fill_price=float(fill_price) if fill_price else None,
                )
            except httpx.HTTPStatusError as e:
                logger.error(f"Oanda close position error {e.response.status_code}: {e.response.text}")
                return BrokerOrderResult(success=False, error_message=e.response.text)

    async def get_position(self, account: str, symbol: str) -> float:
        account_id = self._resolve_account(account)
        async with httpx.AsyncClient(headers=self.headers, timeout=10.0) as client:
            try:
                resp = await client.get(f"{self.base_url}/accounts/{account_id}/positions/{symbol}")
                if resp.status_code == 404:
                    return 0.0
                resp.raise_for_status()
                pos = resp.json().get("position")
                if not pos:
                    return 0.0
                return float(pos.get("long", {}).get("units", 0)) + float(pos.get("short", {}).get("units", 0))
            except httpx.HTTPStatusError as e:
                logger.error(f"Oanda get_position error {e.response.status_code}: {e.response.text}")
                return 0.0
            except Exception:
                logger.exception(f"Error fetching Oanda position for {symbol}")
                return 0.0

    async def get_open_trade_quantities(self, account: str, symbol: str) -> set[int]:
        """
        Return the set of unit sizes for all currently open trades on this
        instrument. Used by FIFO quantity resolution to find a unique size.

        Oanda /openTrades returns individual trade legs (not the net position),
        so we get the exact sizes Oanda is tracking for FIFO purposes.
        Returns absolute (unsigned) unit counts.
        """
        account_id = self._resolve_account(account)
        async with httpx.AsyncClient(headers=self.headers, timeout=10.0) as client:
            try:
                resp = await client.get(
                    f"{self.base_url}/accounts/{account_id}/openTrades",
                    params={"instrument": symbol},
                )
                resp.raise_for_status()
                trades = resp.json().get("trades", [])
                return {abs(int(float(t.get("currentUnits", 0)))) for t in trades}
            except Exception:
                logger.exception(f"Error fetching open trades for {symbol} — FIFO will use base quantity")
                return set()

    async def get_open_positions_pnl(self, account: str) -> list[dict]:
        """
        Fetch live unrealized P&L from Oanda's openPositions endpoint.
        Oanda returns unrealizedPL in account currency directly — no calculation needed.
        """
        account_id = self._resolve_account(account)
        async with httpx.AsyncClient(headers=self.headers, timeout=10.0) as client:
            try:
                resp = await client.get(
                    f"{self.base_url}/accounts/{account_id}/openPositions"
                )
                resp.raise_for_status()
                positions = resp.json().get("positions", [])
                result = []
                for pos in positions:
                    symbol = pos.get("instrument", "")
                    # Oanda returns separate long/short sides — combine
                    long_units  = float(pos.get("long",  {}).get("units", 0))
                    short_units = float(pos.get("short", {}).get("units", 0))
                    net_units   = long_units + short_units

                    # Prefer the top-level unrealizedPL (account currency, net of both sides).
                    # Fall back to summing long + short side fields for older API versions.
                    top_level_pnl = pos.get("unrealizedPL")
                    if top_level_pnl is not None:
                        unrealized_pnl = float(top_level_pnl)
                    else:
                        long_pnl  = float(pos.get("long",  {}).get("unrealizedPL", 0))
                        short_pnl = float(pos.get("short", {}).get("unrealizedPL", 0))
                        unrealized_pnl = long_pnl + short_pnl

                    # last_price: use the live mid from the stream cache if available,
                    # otherwise fall back to the average fill price from Oanda.
                    from app.services.oanda_stream import get_manager
                    manager = get_manager("oanda", self.account_alias)
                    cached = manager.get_price(symbol) if manager else None
                    if cached:
                        last_price = cached["mid"]
                    elif long_units > 0:
                        last_price = float(pos.get("long", {}).get("averagePrice", 0)) or None
                    elif short_units < 0:
                        last_price = float(pos.get("short", {}).get("averagePrice", 0)) or None
                    else:
                        last_price = None

                    if net_units != 0:
                        result.append({
                            "symbol":         symbol,
                            "last_price":     last_price,
                            "unrealized_pnl": unrealized_pnl,
                        })
                return result
            except Exception:
                logger.exception("Error fetching Oanda open positions P&L")
                return []

    async def poll_order_status(
        self, broker_order_id: str, account: str
    ) -> OrderStatusResult:
        """
        Poll an open order's current state from Oanda.
        Called periodically for resting limit/stop orders.
        """
        account_id = self._resolve_account(account)
        async with httpx.AsyncClient(headers=self.headers, timeout=10.0) as client:
            try:
                resp = await client.get(
                    f"{self.base_url}/accounts/{account_id}/orders/{broker_order_id}"
                )
                if resp.status_code == 404:
                    return OrderStatusResult(found=False)
                resp.raise_for_status()
                data = resp.json()
                order = data.get("order", {})
                state = order.get("state", "")

                if state == "FILLED":
                    # Fetch the fill transaction for price/qty
                    fill_tx_id = order.get("fillingTransactionID")
                    fill_price = None
                    filled_qty = float(order.get("units", 0))
                    if fill_tx_id:
                        tx_resp = await client.get(
                            f"{self.base_url}/accounts/{account_id}/transactions/{fill_tx_id}"
                        )
                        if tx_resp.status_code == 200:
                            tx = tx_resp.json().get("transaction", {})
                            fill_price = float(tx.get("price", 0)) or None
                            filled_qty = abs(float(tx.get("units", filled_qty)))
                    return OrderStatusResult(
                        found=True, is_filled=True,
                        filled_quantity=filled_qty,
                        avg_fill_price=fill_price,
                    )
                elif state in ("CANCELLED", "EXPIRED", "TRIGGERED"):
                    return OrderStatusResult(found=True, is_cancelled=True)
                elif state == "PENDING":
                    return OrderStatusResult(found=True, is_open=True)
                else:
                    return OrderStatusResult(found=True, is_open=True)

            except httpx.HTTPStatusError as e:
                logger.error(f"Oanda poll_order_status error {e.response.status_code}")
                return OrderStatusResult(found=False, error_message=e.response.text)
            except Exception as e:
                logger.exception(f"Error polling Oanda order {broker_order_id}")
                return OrderStatusResult(found=False, error_message=str(e))

    async def cancel_order(self, broker_order_id: str, account: str) -> bool:
        account_id = self._resolve_account(account)
        async with httpx.AsyncClient(headers=self.headers, timeout=10.0) as client:
            try:
                resp = await client.put(
                    f"{self.base_url}/accounts/{account_id}/orders/{broker_order_id}/cancel"
                )
                return resp.status_code == 200
            except Exception:
                logger.exception(f"Error cancelling Oanda order {broker_order_id}")
                return False
