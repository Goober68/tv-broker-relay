"""
IBKR Adapter — Client Portal Gateway REST API.

Supports equities (STK), futures (FUT), and forex (CASH) via conid-based routing.
Each tenant configures their instrument_map on their BrokerAccount:

    {
        "AAPL":  {"conid": 265598,   "sec_type": "STK", "exchange": "NASDAQ"},
        "ES":    {"conid": 495512551, "sec_type": "FUT", "exchange": "CME",
                  "multiplier": 50.0},
        "EUR":   {"conid": 12087792,  "sec_type": "CASH", "exchange": "IDEALPRO"},
    }

The adapter falls back to the gateway's symbol search if conid is not pre-mapped.
"""
import httpx
import logging
from app.brokers.base import BrokerBase, BrokerOrderResult, OrderStatusResult
from app.models.order import (
    Order, OrderAction, OrderType, TimeInForce,
    InstrumentType, IBKR_SEC_TYPE,
)
from datetime import datetime

logger = logging.getLogger(__name__)

_TIF_MAP = {
    TimeInForce.DAY: "DAY",
    TimeInForce.GTC: "GTC",
    TimeInForce.IOC: "IOC",
    TimeInForce.FOK: "FOK",
    TimeInForce.GTD: "GTD",
}

_ORDER_TYPE_MAP = {
    OrderType.MARKET: "MKT",
    OrderType.LIMIT:  "LMT",
    OrderType.STOP:   "STP",
}


class IBKRBroker(BrokerBase):

    def __init__(
        self,
        gateway_url: str,
        account_id: str,
        instrument_map: dict | None = None,
    ):
        self.base_url = gateway_url.rstrip("/")
        self.account_id = account_id
        # instrument_map: symbol → {conid, sec_type, exchange, multiplier, ...}
        self.instrument_map: dict[str, dict] = instrument_map or {}
        self.client_kwargs = {"verify": False, "timeout": 15.0}

    @classmethod
    def from_credentials(cls, creds: dict) -> "IBKRBroker":
        return cls(
            gateway_url=creds.get("gateway_url", "https://localhost:5000/v1/api"),
            account_id=creds["account_id"],
            instrument_map=creds.get("instrument_map"),
        )

    @classmethod
    def from_settings(cls) -> "IBKRBroker":
        from app.config import get_settings
        s = get_settings()
        return cls(gateway_url=s.ibkr_gateway_url, account_id=s.ibkr_account_id)

    def _resolve_account(self, account: str) -> str:
        return self.account_id if account == "primary" else account

    def _get_instrument(self, symbol: str) -> dict | None:
        """Return instrument config from the tenant's map, or None."""
        return self.instrument_map.get(symbol)

    async def _search_conid(
        self, client: httpx.AsyncClient, symbol: str, sec_type: str
    ) -> int | None:
        """
        Dynamically resolve conid via IBKR's secdef search endpoint.
        Used as fallback when the symbol is not in instrument_map.
        Returns the first matching conid, or None.
        """
        try:
            resp = await client.get(
                f"{self.base_url}/iserver/secdef/search",
                params={"symbol": symbol, "secType": sec_type},
            )
            resp.raise_for_status()
            results = resp.json()
            if results and isinstance(results, list):
                return results[0].get("conid")
        except Exception:
            logger.exception(f"IBKR secdef search failed for {symbol}/{sec_type}")
        return None

    def _build_order_body(self, order: Order, account_id: str, conid: int) -> dict:
        """Construct the IBKR order request body."""
        side = "BUY" if order.action in (OrderAction.BUY,) else "SELL"
        tif = _TIF_MAP.get(order.time_in_force, "DAY")
        order_type = _ORDER_TYPE_MAP.get(order.order_type, "MKT")

        # Options use their own conidex format encoding the full contract spec
        if order.instrument_type == InstrumentType.OPTION and order.option_expiry:
            # IBKR conidex format: "conid@exchange:OPT:YYYYMMDD:strike:right"
            expiry_compact = order.option_expiry.replace("-", "")
            instr = self._get_instrument(order.symbol)
            exchange = (instr or {}).get("exchange", "SMART")
            strike = f"{order.option_strike:.1f}" if order.option_strike else "0"
            right = order.option_right or "C"
            conidex = f"{conid}@{exchange}:OPT:{expiry_compact}:{strike}:{right}"
            qty = order.quantity  # option contracts (each = 100 shares by default)
        elif order.instrument_type == InstrumentType.FUTURE:
            conidex = None
            qty = int(order.quantity)
        else:
            conidex = None
            qty = order.quantity

        body: dict = {
            "acctId": account_id,
            "orderType": order_type,
            "side": side,
            "quantity": qty,
            "tif": tif,
            "outsideRth": order.extended_hours,
        }

        if conidex:
            body["conidex"] = conidex
        else:
            body["conid"] = conid

        if order.price and order.order_type in (OrderType.LIMIT, OrderType.STOP):
            body["price"] = order.price

        if order.stop_loss is not None:
            body["auxPrice"] = order.stop_loss

        if order.time_in_force == TimeInForce.GTD and order.expire_at:
            body["tifExpiry"] = order.expire_at.strftime("%Y%m%d %H:%M:%S")

        return {"orders": [body]}

    async def submit_order(self, order: Order) -> BrokerOrderResult:
        account_id = self._resolve_account(order.account)

        if order.action == OrderAction.CLOSE:
            return await self._close_position(account_id, order.symbol, order.instrument_type)

        async with httpx.AsyncClient(**self.client_kwargs) as client:
            # Resolve conid
            instr = self._get_instrument(order.symbol)
            if instr and "conid" in instr:
                conid = instr["conid"]
            else:
                sec_type = IBKR_SEC_TYPE.get(order.instrument_type.value, "STK")
                conid = await self._search_conid(client, order.symbol, sec_type)
                if conid is None:
                    return BrokerOrderResult(
                        success=False,
                        error_message=(
                            f"Could not resolve IBKR conid for {order.symbol} "
                            f"({order.instrument_type.value}). "
                            f"Add it to your broker account instrument_map."
                        ),
                    )

            body = self._build_order_body(order, account_id, conid)
            import json as _json
            body_str = _json.dumps(body, default=str)

            try:
                resp = await client.post(
                    f"{self.base_url}/iserver/account/{account_id}/orders", json=body
                )
                resp.raise_for_status()
                data = resp.json()

                # IBKR may return a confirmation challenge
                if isinstance(data, list) and data:
                    item = data[0]
                    if "order_id" in item:
                        return BrokerOrderResult(
                            success=True,
                            broker_order_id=str(item["order_id"]),
                            order_open=order.order_type != OrderType.MARKET,
                            broker_request=body_str,
                            broker_response=resp.text,
                        )
                    if "messageIds" in item:
                        confirmed = await self._confirm_order(client, item["id"])
                        if confirmed:
                            return BrokerOrderResult(
                                success=True,
                                broker_order_id=str(item["id"]),
                                order_open=order.order_type != OrderType.MARKET,
                                broker_request=body_str,
                                broker_response=resp.text,
                            )
                        return BrokerOrderResult(
                            success=False, error_message="IBKR order confirmation failed"
                        )

                return BrokerOrderResult(success=False, error_message=str(data))

            except httpx.HTTPStatusError as e:
                logger.error(f"IBKR order error {e.response.status_code}: {e.response.text}")
                return BrokerOrderResult(success=False, error_message=e.response.text)
            except Exception as e:
                logger.exception("Unexpected error submitting to IBKR")
                return BrokerOrderResult(success=False, error_message=str(e))

    async def _confirm_order(self, client: httpx.AsyncClient, order_id: str) -> bool:
        try:
            resp = await client.post(
                f"{self.base_url}/iserver/reply/{order_id}", json={"confirmed": True}
            )
            return resp.status_code == 200
        except Exception:
            return False

    async def _close_position(
        self, account_id: str, symbol: str, instrument_type: InstrumentType
    ) -> BrokerOrderResult:
        """Close position by submitting an opposing market order for the current quantity."""
        qty = await self.get_position(account_id, symbol)
        if abs(qty) < 1e-9:
            return BrokerOrderResult(success=True, filled_quantity=0.0,
                                     error_message="Position already flat")

        instr = self._get_instrument(symbol)
        conid = instr.get("conid") if instr else None
        if conid is None:
            return BrokerOrderResult(
                success=False,
                error_message=f"Cannot close {symbol}: conid not in instrument_map",
            )

        side = "SELL" if qty > 0 else "BUY"
        close_body = {"orders": [{
            "acctId": account_id, "conid": conid,
            "orderType": "MKT", "side": side,
            "quantity": abs(int(qty)) if instrument_type == InstrumentType.FUTURE else abs(qty),
            "tif": "DAY",
        }]}

        async with httpx.AsyncClient(**self.client_kwargs) as client:
            try:
                resp = await client.post(
                    f"{self.base_url}/iserver/account/{account_id}/orders", json=close_body
                )
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, list) and data and "order_id" in data[0]:
                    return BrokerOrderResult(
                        success=True,
                        broker_order_id=str(data[0]["order_id"]),
                        filled_quantity=abs(qty),
                    )
                # Handle confirmation challenge
                if isinstance(data, list) and data and "messageIds" in data[0]:
                    confirmed = await self._confirm_order(client, data[0]["id"])
                    if confirmed:
                        return BrokerOrderResult(
                            success=True, broker_order_id=str(data[0]["id"]),
                            filled_quantity=abs(qty),
                        )
                return BrokerOrderResult(success=False, error_message=str(data))
            except httpx.HTTPStatusError as e:
                logger.error(f"IBKR close error {e.response.status_code}: {e.response.text}")
                return BrokerOrderResult(success=False, error_message=e.response.text)

    async def get_position(self, account: str, symbol: str) -> float:
        account_id = self._resolve_account(account)
        instr = self._get_instrument(symbol)
        conid = instr.get("conid") if instr else None

        async with httpx.AsyncClient(**self.client_kwargs) as client:
            try:
                resp = await client.get(
                    f"{self.base_url}/portfolio/{account_id}/positions/0"
                )
                resp.raise_for_status()
                for pos in resp.json():
                    # Match by conid if available, fall back to ticker
                    if conid and pos.get("conid") == conid:
                        return float(pos.get("position", 0))
                    elif not conid and pos.get("ticker", "").upper() == symbol.upper():
                        return float(pos.get("position", 0))
                return 0.0
            except Exception:
                logger.exception(f"Error fetching IBKR position for {symbol}")
                return 0.0

    async def poll_order_status(
        self, broker_order_id: str, account: str
    ) -> OrderStatusResult:
        """Poll IBKR order status via the live orders endpoint."""
        account_id = self._resolve_account(account)
        async with httpx.AsyncClient(**self.client_kwargs) as client:
            try:
                resp = await client.get(
                    f"{self.base_url}/iserver/account/orders",
                    params={"filters": "Submitted,PreSubmitted,Filled,Cancelled"}
                )
                resp.raise_for_status()
                data = resp.json()
                orders = data.get("orders", [])
                for order in orders:
                    if str(order.get("orderId")) == str(broker_order_id):
                        status = order.get("status", "")
                        if status == "Filled":
                            filled = float(order.get("filledQuantity", 0))
                            avg = float(order.get("avgPrice", 0)) or None
                            return OrderStatusResult(
                                found=True, is_filled=True,
                                filled_quantity=filled, avg_fill_price=avg
                            )
                        elif status in ("Cancelled", "Inactive"):
                            return OrderStatusResult(found=True, is_cancelled=True)
                        else:
                            return OrderStatusResult(found=True, is_open=True)
                return OrderStatusResult(found=False)
            except Exception as e:
                logger.exception(f"Error polling IBKR order {broker_order_id}")
                return OrderStatusResult(found=False, error_message=str(e))

    async def cancel_order(self, broker_order_id: str, account: str) -> bool:
        account_id = self._resolve_account(account)
        async with httpx.AsyncClient(**self.client_kwargs) as client:
            try:
                resp = await client.delete(
                    f"{self.base_url}/iserver/account/{account_id}/order/{broker_order_id}"
                )
                return resp.status_code == 200
            except Exception:
                return False
