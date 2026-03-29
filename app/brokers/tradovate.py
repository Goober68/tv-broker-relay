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
    TimeInForce.GTD: "GoodTillDate",  # Tradovate native GTD support
}


class TradovateBroker(BrokerBase):

    def __init__(
        self,
        username: str,
        password: str,
        app_id: str,
        app_version: str,
        base_url: str,
        device_id: str = "",
        cid: str = "0",
        sec: str = "",
        instrument_map: dict | None = None,
    ):
        self.username    = username
        self.password    = password
        self.app_id      = app_id
        self.app_version = app_version
        self.base_url    = base_url.rstrip("/")
        self.device_id   = device_id
        self.cid         = cid
        self.sec         = sec
        self.instrument_map: dict[str, dict] = instrument_map or {}
        self._access_token: str | None = None
        self._token_expiry: datetime | None = None
        self._account_id: int | None = None      # numeric account ID fetched after auth
        self._account_id_map: dict[str, int] = {}  # name → numeric ID map

    @classmethod
    def from_credentials(cls, creds: dict) -> "TradovateBroker":
        instance = cls(
            username       = creds.get("username", ""),
            password       = creds.get("password", ""),
            app_id         = creds.get("app_id", ""),
            app_version    = creds.get("app_version", "1.0"),
            base_url       = creds.get("base_url", "https://live.tradovateapi.com/v1"),
            device_id      = creds.get("device_id", ""),
            cid            = str(creds.get("cid", "0")),
            sec            = creds.get("sec", ""),
            instrument_map = creds.get("instrument_map"),
        )
        # OAuth flow — pre-set the access token and refresh token
        if creds.get("auth_method") == "oauth" and creds.get("access_token"):
            instance._access_token = creds["access_token"]
            instance._token_expiry = datetime.now(timezone.utc) + timedelta(minutes=80)
            instance._refresh_token = creds.get("refresh_token")
            instance._auth_method = "oauth"
            instance._oauth_creds = creds  # keep reference for persisting refreshed tokens
            instance._broker_account_id = creds.get("_broker_account_id")
        return instance

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

        # OAuth accounts: use refresh token to get a new access token
        if getattr(self, '_auth_method', None) == "oauth" and getattr(self, '_refresh_token', None):
            return await self._refresh_oauth_token()

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{self.base_url}/auth/accesstokenrequest",
                json={
                    "name":       self.username,
                    "password":   self.password,
                    "appId":      self.app_id,
                    "appVersion": self.app_version,
                    "deviceId":   self.device_id,
                    "cid":        self.cid,
                    "sec":        self.sec,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            if "errorText" in data:
                raise RuntimeError(f"Tradovate auth failed: {data['errorText']}")
            self._access_token = data["accessToken"]
            self._token_expiry = datetime.now(timezone.utc) + timedelta(minutes=55)

            # Fetch numeric account IDs for all accounts under this login
            if self._account_id is None:
                try:
                    acct_resp = await client.get(
                        f"{self.base_url}/account/list",
                        headers={"Authorization": f"Bearer {self._access_token}"},
                    )
                    acct_resp.raise_for_status()
                    accounts = acct_resp.json()
                    # Store all account name→id mappings
                    self._account_id_map = {a["name"]: a["id"] for a in accounts}
                    # Default to first account
                    if accounts:
                        self._account_id = accounts[0]["id"]
                except Exception:
                    logger.warning("Could not fetch Tradovate numeric account IDs")
                    self._account_id_map = {}

            return self._access_token

    async def _refresh_oauth_token(self) -> str:
        """Use the refresh token to obtain a new access token."""
        from app.config import get_settings
        settings = get_settings()

        # Determine token endpoint from base_url
        if "demo" in self.base_url:
            token_url = "https://demo.tradovateapi.com/auth/oauthtoken"
        else:
            token_url = "https://live.tradovateapi.com/auth/oauthtoken"

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                token_url,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                    "client_id": settings.tradovate_oauth_client_id,
                    "client_secret": settings.tradovate_oauth_client_secret,
                },
            )
            resp.raise_for_status()
            data = resp.json()

            if "errorText" in data:
                raise RuntimeError(f"Tradovate OAuth refresh failed: {data['errorText']}")

            self._access_token = data.get("accessToken") or data.get("access_token")
            if not self._access_token:
                raise RuntimeError(f"No access token in refresh response: {list(data.keys())}")

            # Update refresh token if a new one was issued
            new_refresh = data.get("refresh_token") or data.get("refreshToken")
            if new_refresh:
                self._refresh_token = new_refresh

            expires_in = data.get("expires_in", 5400)
            self._token_expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in - 300)

            logger.info(f"Tradovate OAuth token refreshed, expires in {expires_in}s")

            # Persist refreshed tokens to DB so they survive restarts
            if getattr(self, '_broker_account_id', None) and getattr(self, '_oauth_creds', None):
                try:
                    self._oauth_creds["access_token"] = self._access_token
                    if new_refresh:
                        self._oauth_creds["refresh_token"] = new_refresh
                    from app.services.credentials import encrypt_credentials
                    from app.models.db import AsyncSessionLocal as async_session_factory
                    from app.models.broker_account import BrokerAccount
                    async with async_session_factory() as session:
                        from sqlalchemy import update
                        await session.execute(
                            update(BrokerAccount)
                            .where(BrokerAccount.id == self._broker_account_id)
                            .values(credentials_encrypted=encrypt_credentials(self._oauth_creds))
                        )
                        await session.commit()
                    logger.info(f"Persisted refreshed OAuth token for BrokerAccount {self._broker_account_id}")
                except Exception:
                    logger.exception("Failed to persist refreshed OAuth token — will work until restart")

            # Fetch account IDs if not yet loaded
            if self._account_id is None:
                try:
                    acct_resp = await client.get(
                        f"{self.base_url}/account/list",
                        headers={"Authorization": f"Bearer {self._access_token}"},
                    )
                    acct_resp.raise_for_status()
                    accounts = acct_resp.json()
                    self._account_id_map = {a["name"]: a["id"] for a in accounts}
                    if accounts:
                        self._account_id = accounts[0]["id"]
                except Exception:
                    logger.warning("Could not fetch Tradovate account IDs after refresh")

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

        # Get numeric account ID for this account alias
        acct_id = getattr(self, '_account_id_map', {}).get(order.account) or self._account_id or 0

        base_body: dict = {
            "accountSpec": order.account,
            "accountId":   acct_id,  # numeric ID required by placeOSO
            "symbol":      order.symbol,
            "action":      action_map[order.action],
            "orderQty":    int(order.quantity),
            "orderType":   order_type_map[order.order_type],
            "timeInForce": tif,
            "isAutomated": True,
        }
        if order.price and order.order_type != OrderType.MARKET:
            base_body["price"] = order.price
        if order.time_in_force == TimeInForce.GTD and order.expire_at:
            base_body["expireTime"] = order.expire_at.strftime("%Y-%m-%dT%H:%M:%SZ")

        has_trail   = order.trail_dist is not None
        has_bracket = (order.stop_loss is not None
                       or order.take_profit is not None
                       or has_trail)

        import json as _json
        if has_trail:
            # Trailing stop — use /orderStrategy/startOrderStrategy with params JSON
            # IMPORTANT: startOrderStrategy bracket values are RELATIVE offsets from
            # entry price, not absolute prices. The offset converter has already
            # converted trail_dist/trail_trigger/trail_update to price differences.
            # take_profit and stop_loss must also be relative here — subtract entry price.
            # startOrderStrategy bracket values are signed relative offsets:
            # positive = profit direction, negative = loss direction
            # For a buy: TP is positive (+25), SL/trail is negative (-25)
            # For a sell: TP is negative (-25), SL/trail is positive (+25)
            is_buy = order.action == OrderAction.BUY
            sign   =  1 if is_buy else -1  # profit direction
            loss   = -1 if is_buy else  1  # loss direction

            trail_dist = order.trail_dist * loss  # negative for buy, positive for sell

            bracket: dict = {
                "qty":          int(order.quantity),
                "trailingStop": True,
                "stopLoss":     trail_dist,
            }
            if order.take_profit is not None:
                # startOrderStrategy bracket values are RELATIVE offsets from entry.
                # order.take_profit is already an absolute price (post offset_converter).
                # We must subtract the entry price to get the relative offset.
                ref_price = order.price or order.avg_fill_price
                if ref_price:
                    bracket["profitTarget"] = abs(order.take_profit - ref_price)
                else:
                    # Market order with no reference price — log warning.
                    # take_profit is absolute; best we can do is use it as-is and
                    # hope the caller provided a sensible value.
                    logger.warning(
                        f"{order.symbol}: no reference price for market order — "
                        f"cannot compute relative profitTarget from absolute {order.take_profit}"
                    )
                    bracket["profitTarget"] = order.take_profit
            if order.stop_loss is not None:
                ref_price = order.price or order.avg_fill_price
                if ref_price:
                    bracket["stopLoss"] = abs(order.stop_loss - ref_price) * loss
                else:
                    logger.warning(
                        f"{order.symbol}: no reference price for market order — "
                        f"cannot compute relative stopLoss from absolute {order.stop_loss}"
                    )
                    bracket["stopLoss"] = order.stop_loss * loss
            auto_trail: dict = {"stopLoss": abs(order.trail_dist)}
            if order.trail_trigger is not None:
                auto_trail["trigger"] = order.trail_trigger
            if order.trail_update is not None:
                auto_trail["freq"] = order.trail_update
            bracket["autoTrail"] = auto_trail

            entry_version: dict = {
                "orderQty":    int(order.quantity),
                "orderType":   order_type_map[order.order_type],
                "timeInForce": tif,
            }
            if order.price and order.order_type != OrderType.MARKET:
                entry_version["price"] = order.price
            params_str = _json.dumps({
                "entryVersion": entry_version,
                "brackets": [bracket],
            })
            strat_body = {
                "accountId":          acct_id,
                "accountSpec":        order.account,
                "orderStrategyTypeId": 2,
                "action":             action_map[order.action],
                "symbol":             order.symbol,
                "params":             params_str,
            }
            endpoint = "/orderStrategy/startOrderStrategy"
            body = strat_body

        elif has_bracket:
            # TP/SL only — use /order/placeOSO with flat bracket1/bracket2 fields
            close_action = "Sell" if order.action == OrderAction.BUY else "Buy"
            body = dict(base_body)
            if order.take_profit is not None:
                body["bracket1"] = {
                    "action":    close_action,
                    "orderType": "Limit",
                    "price":     order.take_profit,
                }
            if order.stop_loss is not None:
                key = "bracket2" if order.take_profit is not None else "bracket1"
                body[key] = {
                    "action":     close_action,
                    "orderType":  "Stop",
                    "stopPrice":  order.stop_loss,
                }
            endpoint = "/order/placeOSO"

        else:
            # Plain order — /order/placeOrder
            body = base_body
            endpoint = "/order/placeOrder"

        body_str = _json.dumps(body, default=str)
        async with httpx.AsyncClient(headers=self._headers(token), timeout=15.0) as client:
            try:
                resp = await client.post(f"{self.base_url}{endpoint}", json=body)
                resp.raise_for_status()
                data = resp.json()
                if data.get("failureReason"):
                    return BrokerOrderResult(
                        success=False,
                        error_message=data["failureReason"],
                        broker_request=body_str,
                        broker_response=_json.dumps(data, default=str),
                    )
                order_id = str(data.get("orderId", ""))
                is_open = order.order_type != OrderType.MARKET
                return BrokerOrderResult(
                    success=True, broker_order_id=order_id, order_open=is_open,
                    broker_request=body_str,
                    broker_response=resp.text,
                )
            except httpx.HTTPStatusError as e:
                logger.error(f"Tradovate order error {e.response.status_code}: {e.response.text}")
                return BrokerOrderResult(
                    success=False,
                    error_message=e.response.text,
                    broker_request=body_str,
                    broker_response=e.response.text,
                )
            except Exception as e:
                logger.exception("Unexpected error submitting to Tradovate")
                return BrokerOrderResult(
                    success=False,
                    error_message=str(e),
                    broker_request=body_str,
                )

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
                    return BrokerOrderResult(
                        success=False,
                        error_message=data["failureReason"],
                        broker_request=_json.dumps(body, default=str),
                        broker_response=_json.dumps(data, default=str),
                    )
                return BrokerOrderResult(success=True, broker_order_id=str(data.get("orderId", "")),
                                         broker_request=_json.dumps(body, default=str),
                                         broker_response=resp.text)
            except Exception as e:
                return BrokerOrderResult(success=False, error_message=str(e))

    async def get_position(self, account: str, symbol: str) -> float:
        token = await self._ensure_authenticated()
        async with httpx.AsyncClient(headers=self._headers(token), timeout=10.0) as client:
            try:
                resp = await client.get(f"{self.base_url}/position/list")
                resp.raise_for_status()
                for pos in resp.json():
                    # Match both symbol and account to handle multiple accounts per login
                    pos_account = (
                        pos.get("accountName")
                        or pos.get("accountSpec")
                        or pos.get("account")
                        or ""
                    )
                    pos_symbol = pos.get("contractName") or pos.get("symbol") or ""
                    if pos_symbol == symbol and pos_account == account:
                        return float(pos.get("netPos", 0))
                return 0.0
            except Exception:
                logger.exception(f"Error fetching Tradovate position for {symbol}")
                return 0.0

    async def get_open_positions_pnl(self, account: str) -> list[dict]:
        """
        Fetch live unrealized P&L from Tradovate.
        Uses position list (avg price + qty) + quote endpoint for current price,
        then calculates unrealized P&L using the futures multiplier.
        """
        token = await self._ensure_authenticated()
        async with httpx.AsyncClient(headers=self._headers(token), timeout=10.0) as client:
            try:
                # Get open positions
                resp = await client.get(f"{self.base_url}/position/list")
                resp.raise_for_status()
                pos_list = [
                    p for p in resp.json()
                    if p.get("netPos", 0) != 0
                    and (
                        p.get("accountName") or
                        p.get("accountSpec") or
                        p.get("account") or ""
                    ) == account
                ]
                if not pos_list:
                    return []

                result = []
                for pos in pos_list:
                    symbol    = pos.get("contractName", pos.get("symbol", ""))
                    net_pos   = float(pos.get("netPos", 0))
                    avg_price = float(pos.get("avgPrice", 0))

                    # Fetch current quote for this symbol
                    last_price = None
                    try:
                        q_resp = await client.get(
                            f"{self.base_url}/md/getQuote",
                            params={"symbol": symbol}
                        )
                        if q_resp.status_code == 200:
                            q = q_resp.json()
                            # Use mid price if available, else last trade
                            bid = q.get("bid")
                            ask = q.get("ask")
                            last = q.get("last")
                            if bid and ask:
                                last_price = (float(bid) + float(ask)) / 2
                            elif last:
                                last_price = float(last)
                    except Exception:
                        pass

                    mult = self._get_multiplier(symbol)
                    if last_price and avg_price:
                        unrealized_pnl = (last_price - avg_price) * net_pos * mult
                    else:
                        unrealized_pnl = None

                    # Strip contract month suffix for symbol matching (ESH5 -> ES)
                    root = ''.join(c for c in symbol if c.isalpha())

                    result.append({
                        "symbol":         symbol,
                        "symbol_root":    root,
                        "last_price":     last_price,
                        "unrealized_pnl": unrealized_pnl,
                    })
                return result
            except Exception:
                logger.exception("Error fetching Tradovate open positions P&L")
                return []

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
