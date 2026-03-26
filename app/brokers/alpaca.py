"""
Alpaca Broker Adapter.

Uses the Alpaca Markets REST API v2.
Supports US equities, options, and crypto.

Credentials required:
    api_key:    Alpaca API key ID
    api_secret: Alpaca API secret key
    base_url:   API base URL
                Live:  https://api.alpaca.markets
                Paper: https://paper-api.alpaca.markets
    data_url:   Market data URL (default: https://data.alpaca.markets)

API docs: https://docs.alpaca.markets
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
    TimeInForce.DAY: "day",
    TimeInForce.GTC: "gtc",
    TimeInForce.IOC: "ioc",
    TimeInForce.FOK: "fok",
    TimeInForce.GTD: "gtd",
}

_FILLED_STATUSES    = {"filled", "partially_filled"}
_CANCELLED_STATUSES = {"canceled", "expired", "replaced", "rejected", "suspended"}
_OPEN_STATUSES      = {"new", "partially_filled", "done_for_day", "accepted",
                        "pending_new", "accepted_for_bidding", "stopped",
                        "calculated", "held"}


class AlpacaBroker(BrokerBase):

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str = "https://api.alpaca.markets",
        data_url: str = "https://data.alpaca.markets",
    ):
        self.api_key    = api_key
        self.api_secret = api_secret
        self.base_url   = base_url.rstrip("/")
        self.data_url   = data_url.rstrip("/")

    @classmethod
    def from_credentials(cls, creds: dict) -> "AlpacaBroker":
        return cls(
            api_key    = creds["api_key"],
            api_secret = creds["api_secret"],
            base_url   = creds.get("base_url", "https://api.alpaca.markets"),
            data_url   = creds.get("data_url", "https://data.alpaca.markets"),
        )

    @classmethod
    def from_settings(cls) -> "AlpacaBroker":
        from app.config import get_settings
        s = get_settings()
        return cls(
            api_key    = s.alpaca_api_key,
            api_secret = s.alpaca_api_secret,
            base_url   = s.alpaca_base_url,
        )

    def _headers(self) -> dict:
        return {
            "APCA-API-KEY-ID":     self.api_key,
            "APCA-API-SECRET-KEY": self.api_secret,
            "Content-Type":        "application/json",
        }

    # ── Order Submission ───────────────────────────────────────────────────────

    async def submit_order(self, order: Order) -> BrokerOrderResult:
        if order.action == OrderAction.CLOSE:
            return await self._close_position(order.account, order.symbol)

        body = self._build_order_body(order)

        async with httpx.AsyncClient(
            headers=self._headers(), timeout=15.0
        ) as client:
            try:
                resp = await client.post(
                    f"{self.base_url}/v2/orders", json=body
                )
                resp.raise_for_status()
                data = resp.json()

                order_id = data.get("id", "")
                status   = data.get("status", "")
                is_open  = order.order_type != OrderType.MARKET

                if status in _FILLED_STATUSES:
                    filled = float(data.get("filled_qty", 0))
                    avg    = float(data.get("filled_avg_price") or 0) or None
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
                logger.error(f"Alpaca order error {e.response.status_code}: {e.response.text}")
                return BrokerOrderResult(success=False, error_message=e.response.text)
            except Exception as e:
                logger.exception("Unexpected error submitting to Alpaca")
                return BrokerOrderResult(success=False, error_message=str(e))

    def _build_order_body(self, order: Order) -> dict:
        is_buy = order.action == OrderAction.BUY

        body: dict = {
            "symbol":        order.symbol,
            "qty":           str(order.quantity),
            "side":          "buy" if is_buy else "sell",
            "type":          "market" if order.order_type == OrderType.MARKET
                             else "limit" if order.order_type == OrderType.LIMIT
                             else "stop",
            "time_in_force": _TIF_MAP.get(order.time_in_force, "day"),
        }

        if order.order_type == OrderType.LIMIT and order.price:
            body["limit_price"] = str(order.price)

        if order.order_type == OrderType.STOP and order.price:
            body["stop_price"] = str(order.price)

        if order.time_in_force == TimeInForce.GTD and order.expire_at:
            body["expire_at"] = order.expire_at.strftime("%Y-%m-%dT%H:%M:%SZ")

        if order.extended_hours:
            body["extended_hours"] = True

        # Bracket order (SL + TP together)
        if order.stop_loss is not None or order.take_profit is not None:
            body["order_class"] = "bracket"
            if order.stop_loss is not None:
                body["stop_loss"]   = {"stop_price": str(order.stop_loss)}
            if order.take_profit is not None:
                body["take_profit"] = {"limit_price": str(order.take_profit)}

        if order.comment:
            body["client_order_id"] = f"relay_{order.id}_{order.comment[:20]}" if order.id else order.comment[:40]

        return body

    # ── Close Position ─────────────────────────────────────────────────────────

    async def _close_position(self, account: str, symbol: str) -> BrokerOrderResult:
        async with httpx.AsyncClient(
            headers=self._headers(), timeout=15.0
        ) as client:
            try:
                resp = await client.delete(
                    f"{self.base_url}/v2/positions/{symbol}"
                )
                if resp.status_code == 404:
                    return BrokerOrderResult(
                        success=True, filled_quantity=0,
                        error_message="Position already flat"
                    )
                resp.raise_for_status()
                data = resp.json()
                order_id = data.get("id", "")
                return BrokerOrderResult(success=True, broker_order_id=order_id)
            except Exception as e:
                return BrokerOrderResult(success=False, error_message=str(e))

    # ── Get Position ───────────────────────────────────────────────────────────

    async def get_position(self, account: str, symbol: str) -> float:
        async with httpx.AsyncClient(
            headers=self._headers(), timeout=10.0
        ) as client:
            try:
                resp = await client.get(f"{self.base_url}/v2/positions/{symbol}")
                if resp.status_code == 404:
                    return 0.0
                resp.raise_for_status()
                data = resp.json()
                qty = float(data.get("qty", 0))
                side = data.get("side", "long")
                return qty if side == "long" else -qty
            except Exception:
                logger.exception(f"Error fetching Alpaca position for {symbol}")
                return 0.0

    # ── Cancel Order ───────────────────────────────────────────────────────────

    async def cancel_order(self, broker_order_id: str, account: str) -> bool:
        async with httpx.AsyncClient(
            headers=self._headers(), timeout=10.0
        ) as client:
            try:
                resp = await client.delete(
                    f"{self.base_url}/v2/orders/{broker_order_id}"
                )
                return resp.status_code in (200, 204)
            except Exception:
                logger.exception(f"Error cancelling Alpaca order {broker_order_id}")
                return False

    # ── Cancel-Replace ─────────────────────────────────────────────────────────

    async def cancel_replace_order(
        self, broker_order_id: str, account: str, new_order: Order
    ) -> BrokerOrderResult:
        try:
            token_headers = self._headers()
        except Exception as e:
            return BrokerOrderResult(success=False, error_message=str(e))

        body = {
            "qty":         str(new_order.quantity),
            "time_in_force": _TIF_MAP.get(new_order.time_in_force, "day"),
        }
        if new_order.price:
            body["limit_price"] = str(new_order.price)

        async with httpx.AsyncClient(
            headers=token_headers, timeout=15.0
        ) as client:
            try:
                resp = await client.patch(
                    f"{self.base_url}/v2/orders/{broker_order_id}",
                    json=body,
                )
                if resp.status_code in (200, 201):
                    data = resp.json()
                    return BrokerOrderResult(
                        success=True,
                        broker_order_id=data.get("id", broker_order_id),
                        order_open=True,
                    )
            except Exception:
                pass

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
        async with httpx.AsyncClient(
            headers=self._headers(), timeout=10.0
        ) as client:
            try:
                resp = await client.get(
                    f"{self.base_url}/v2/orders/{broker_order_id}"
                )
                if resp.status_code == 404:
                    return OrderStatusResult(found=False)
                resp.raise_for_status()
                data = resp.json()
                status = data.get("status", "").lower()

                if status == "filled":
                    filled = float(data.get("filled_qty", 0))
                    avg    = float(data.get("filled_avg_price") or 0) or None
                    return OrderStatusResult(
                        found=True, is_filled=True,
                        filled_quantity=filled, avg_fill_price=avg,
                    )
                if status in _CANCELLED_STATUSES:
                    return OrderStatusResult(found=True, is_cancelled=True)
                return OrderStatusResult(found=True, is_open=True)

            except Exception as e:
                logger.exception(f"Error polling Alpaca order {broker_order_id}")
                return OrderStatusResult(found=False, error_message=str(e))

    # ── Live P&L Polling ───────────────────────────────────────────────────────

    async def get_open_positions_pnl(self, account: str) -> list[dict]:
        async with httpx.AsyncClient(
            headers=self._headers(), timeout=10.0
        ) as client:
            try:
                resp = await client.get(f"{self.base_url}/v2/positions")
                resp.raise_for_status()
                result = []
                for pos in resp.json():
                    qty = float(pos.get("qty", 0))
                    if abs(qty) < 1e-9:
                        continue
                    if pos.get("side", "long") == "short":
                        qty = -qty
                    result.append({
                        "symbol":         pos.get("symbol", ""),
                        "last_price":     float(pos.get("current_price") or 0) or None,
                        "unrealized_pnl": float(pos.get("unrealized_pl") or 0) or None,
                    })
                return result
            except Exception:
                logger.exception("Error fetching Alpaca positions P&L")
                return []
