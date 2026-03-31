"""
TradovateStreamManager — persistent WebSocket connection for live market data.

Connects to the Tradovate market data WebSocket and subscribes to real-time
quotes (md/subscribeQuote) for symbols with open positions.

Updates position.last_price on each tick and fires trail triggers when
the price crosses the trigger level — same pattern as OandaStreamManager.

Lifecycle:
  - Started by background task when Tradovate positions exist
  - Stopped when all positions are flat
  - Reloads pending TrailTrigger rows from DB on (re)connect
"""
import asyncio
import json
import logging
from datetime import datetime, timezone

import websockets

logger = logging.getLogger(__name__)

# Registry of active stream managers: (broker, account_alias) -> TradovateStreamManager
_managers: dict[tuple[str, str], "TradovateStreamManager"] = {}


def get_manager(broker: str, account: str) -> "TradovateStreamManager | None":
    return _managers.get((broker, account))


def get_or_create_manager(
    broker: str,
    account: str,
    access_token: str,
    base_url: str,
    max_total_drawdown: float | None = None,
    tenant_id: str | None = None,
) -> "TradovateStreamManager":
    key = (broker, account)
    if key not in _managers:
        _managers[key] = TradovateStreamManager(
            broker=broker,
            account=account,
            access_token=access_token,
            base_url=base_url,
            max_total_drawdown=max_total_drawdown,
            tenant_id=tenant_id,
        )
    else:
        # Update mutable fields
        _managers[key].access_token = access_token
        _managers[key].max_total_drawdown = max_total_drawdown
    return _managers[key]


def remove_manager(broker: str, account: str):
    _managers.pop((broker, account), None)


def _parse_ws_messages(raw: str) -> list[dict]:
    """Parse SockJS-framed WebSocket messages from Tradovate."""
    if not isinstance(raw, str):
        raw = str(raw)

    if raw.startswith("a["):
        try:
            outer = json.loads(raw[1:])  # strip 'a' prefix
            results = []
            for item in outer:
                if isinstance(item, dict):
                    results.append(item)
                elif isinstance(item, str):
                    # "endpoint\nid\n\njson_body"
                    parts = item.split("\n", 3)
                    if len(parts) >= 4:
                        try:
                            body = json.loads(parts[3])
                            results.append(body)
                        except json.JSONDecodeError:
                            pass
                    else:
                        try:
                            results.append(json.loads(item))
                        except json.JSONDecodeError:
                            pass
            return results
        except (json.JSONDecodeError, TypeError):
            pass
    return []


class TradovateStreamManager:
    """Manages a market data WebSocket for a single Tradovate account."""

    def __init__(
        self,
        broker: str,
        account: str,
        access_token: str,
        base_url: str,
        max_total_drawdown: float | None = None,
        tenant_id: str | None = None,
    ):
        self.broker = broker
        self.account = account
        self.access_token = access_token
        self.base_url = base_url.rstrip("/")
        # Convert REST base URL to WebSocket URL
        # https://live.tradovateapi.com/v1 -> wss://md.tradovateapi.com/v1/websocket
        # https://demo.tradovateapi.com/v1 -> wss://md-d.tradovateapi.com/v1/websocket
        if "demo" in base_url:
            self._ws_url = "wss://md-d.tradovateapi.com/v1/websocket"
        else:
            self._ws_url = "wss://md.tradovateapi.com/v1/websocket"

        self._task: asyncio.Task | None = None
        self._subscribed: set[str] = set()
        self._running: bool = False

        # In-memory cache of pending trail triggers: symbol -> list of trigger dicts
        self._trail_triggers: dict[str, list[dict]] = {}

        # Latest prices: symbol -> {"bid": float, "ask": float, "mid": float, "last": float}
        self._prices: dict[str, dict] = {}

        # Drawdown HWM tracking
        self.max_total_drawdown = max_total_drawdown
        self.tenant_id = tenant_id
        self._cached_balance: float | None = None  # current balance from cashBalance API
        self._balance_fetched_at: datetime | None = None
        self._balance_hwm: float | None = None  # highest observed balance (including unrealized)

    # ── Public API ─────────────────────────────────────────────────────────────

    def is_running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    async def start(self, symbols: set[str]):
        """Start streaming for the given symbols. Idempotent."""
        if self._running and symbols == self._subscribed:
            return
        self._subscribed = set(symbols)
        self._running = True
        await self._reload_trail_triggers()
        await self._init_balance_hwm()
        self._restart_task()
        logger.info(
            f"Tradovate stream started for {self.broker}/{self.account}: {symbols}"
        )

    async def stop(self):
        """Stop the stream."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        logger.info(f"Tradovate stream stopped for {self.broker}/{self.account}")

    async def update_symbols(self, symbols: set[str]):
        """Update subscribed symbols — restart if changed."""
        if symbols == self._subscribed:
            return
        self._subscribed = set(symbols)
        self._restart_task()

    def get_price(self, symbol: str) -> dict | None:
        return self._prices.get(symbol)

    async def add_trail_trigger(self, trigger: dict):
        """Add a pending trail trigger (called from order_processor after fill)."""
        symbol = trigger["symbol"]
        if symbol not in self._trail_triggers:
            self._trail_triggers[symbol] = []
        self._trail_triggers[symbol].append(trigger)
        logger.info(
            f"Tradovate trail trigger added: {symbol} trigger={trigger['trigger_price']} "
            f"dist={trigger['trail_distance']}"
        )

    # ── Private ────────────────────────────────────────────────────────────────

    def _restart_task(self):
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = asyncio.create_task(
            self._run_stream(),
            name=f"tradovate_md_{self.account}",
        )

    async def _reload_trail_triggers(self):
        """Load pending trail triggers from DB into memory."""
        from app.models.db import AsyncSessionLocal
        from app.models.trail_trigger import TrailTrigger
        from sqlalchemy import select

        self._trail_triggers = {}
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(TrailTrigger).where(
                    TrailTrigger.broker == self.broker,
                    TrailTrigger.account == self.account,
                    TrailTrigger.status == "pending",
                )
            )
            triggers = result.scalars().all()
            for t in triggers:
                if t.symbol not in self._trail_triggers:
                    self._trail_triggers[t.symbol] = []
                self._trail_triggers[t.symbol].append({
                    "id": t.id,
                    "symbol": t.symbol,
                    "direction": t.direction,
                    "trigger_price": t.trigger_price,
                    "trail_distance": t.trail_distance,
                    "trade_id": t.trade_id,
                    "tenant_id": str(t.tenant_id),
                })
        count = sum(len(v) for v in self._trail_triggers.values())
        if count:
            logger.info(
                f"Loaded {count} pending trail triggers for Tradovate {self.account}"
            )

    async def _run_stream(self):
        """Connect to Tradovate market data WebSocket and stream quotes."""
        retry_delay = 5
        while self._running:
            if not self._subscribed:
                await asyncio.sleep(5)
                continue
            try:
                async with websockets.connect(
                    self._ws_url, close_timeout=5
                ) as ws:
                    # Wait for SockJS open frame
                    open_msg = await asyncio.wait_for(ws.recv(), timeout=10)
                    logger.debug(f"Tradovate MD WS open ({self.account}): {str(open_msg)[:100]}")

                    # Authenticate
                    auth_msg = f"authorize\n1\n\n{self.access_token}"
                    await ws.send(auth_msg)
                    auth_resp = await asyncio.wait_for(ws.recv(), timeout=10)
                    auth_str = str(auth_resp)
                    if "error" in auth_str.lower() or "unauthorized" in auth_str.lower():
                        logger.warning(
                            f"Tradovate MD WS auth failed ({self.account}): {auth_str[:200]}"
                        )
                        await asyncio.sleep(retry_delay)
                        retry_delay = min(retry_delay * 2, 60)
                        continue

                    logger.info(
                        f"Tradovate MD WS connected: {self.account} "
                        f"subscribing to {self._subscribed}"
                    )
                    retry_delay = 5  # reset on successful connect

                    # Subscribe to quotes for each symbol
                    req_id = 10  # start after auth ID
                    for symbol in self._subscribed:
                        sub_msg = f"md/subscribeQuote\n{req_id}\n\n{json.dumps({'symbol': symbol})}"
                        await ws.send(sub_msg)
                        req_id += 1

                    # Read messages forever
                    while self._running:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        except asyncio.TimeoutError:
                            # No data in 30s — send heartbeat ping
                            try:
                                await ws.send("[]")
                            except Exception:
                                break
                            continue

                        raw_str = str(raw)
                        if raw_str in ("h", "o"):
                            continue  # heartbeat or open frame

                        messages = _parse_ws_messages(raw_str)
                        for msg in messages:
                            await self._handle_message(msg)

            except asyncio.CancelledError:
                return
            except Exception as e:
                if self._running:
                    logger.warning(
                        f"Tradovate MD stream error ({self.account}): {e}. "
                        f"Retrying in {retry_delay}s"
                    )
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 60)

    async def _handle_message(self, msg: dict):
        """Process a message from the Tradovate market data WebSocket."""
        # Quote updates come as: {"s": 200, "d": {"quotes": [...]}}
        # or directly as quote objects with "entries" key
        if isinstance(msg, dict):
            # Response wrapper: {"s": 200, "d": {...}}
            data = msg.get("d", msg)

            # Handle quote subscription responses and updates
            quotes = data.get("quotes", [])
            if quotes:
                for q in quotes:
                    await self._process_quote(q)
                return

            # Direct quote update (from subscription push)
            # These have "entries" with bid/ask data and a "contractMaturity" or "timestamp"
            if "entries" in data:
                await self._process_quote(data)
                return

            # md/subscribeQuote success response wrapping a single quote
            if "contractSymbol" in data or "contractId" in data:
                await self._process_quote(data)

    async def _process_quote(self, q: dict):
        """Extract bid/ask/last from a Tradovate quote object and update state."""
        # Tradovate quote format:
        # {
        #   "contractSymbol": "ESH5",
        #   "entries": {
        #     "Bid": {"price": 5000.25, "size": 10},
        #     "Offer": {"price": 5000.50, "size": 8},
        #     "Trade": {"price": 5000.25, "size": 1}
        #   },
        #   "timestamp": "2024-..."
        # }
        symbol = q.get("contractSymbol", "")
        if not symbol:
            return

        entries = q.get("entries", {})
        bid_entry = entries.get("Bid", {})
        ask_entry = entries.get("Offer", {})
        trade_entry = entries.get("Trade", entries.get("TotalTradeVolume", {}))

        bid = bid_entry.get("price")
        ask = ask_entry.get("price")
        last = trade_entry.get("price")

        if bid is not None:
            bid = float(bid)
        if ask is not None:
            ask = float(ask)
        if last is not None:
            last = float(last)

        mid = None
        if bid and ask:
            mid = (bid + ask) / 2
        elif last:
            mid = float(last)

        if mid is None:
            return  # no usable price

        self._prices[symbol] = {"bid": bid, "ask": ask, "mid": mid, "last": last}

        # Also store under root symbol (e.g. ESH5 -> ES) for lookup
        root = ''.join(c for c in symbol if c.isalpha())
        if root != symbol:
            self._prices[root] = self._prices[symbol]

        # Update position last_price in DB
        await self._update_position_price(symbol, mid)

        # Track balance HWM for drawdown floor
        if self.max_total_drawdown:
            await self._update_drawdown_hwm()

        # Check trail triggers
        for sym_key in (symbol, root):
            if sym_key in self._trail_triggers:
                await self._check_trail_triggers(sym_key, bid, ask, mid)

    async def _update_position_price(self, symbol: str, mid: float):
        """Update last_price for open positions matching this symbol."""
        from app.models.db import AsyncSessionLocal
        from app.models.position import Position
        from sqlalchemy import select, func, or_

        root = ''.join(c for c in symbol if c.isalpha())

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Position).where(
                    Position.broker == self.broker,
                    Position.account == self.account,
                    func.abs(Position.quantity) > 1e-9,
                    or_(
                        Position.symbol == symbol,
                        Position.symbol == root,
                    ),
                )
            )
            positions = result.scalars().all()
            if not positions:
                return

            now = datetime.now(timezone.utc)
            for pos in positions:
                pos.last_price = mid
                pos.last_price_at = now
            await db.commit()

    async def _init_balance_hwm(self):
        """Initialize balance HWM from existing drawdown_floor in DB.
        Ensures we never regress the floor on restart."""
        if not self.max_total_drawdown:
            return
        from app.models.db import AsyncSessionLocal
        from app.models.broker_account import BrokerAccount
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(BrokerAccount).where(
                    BrokerAccount.broker == self.broker,
                    BrokerAccount.account_alias == self.account,
                )
            )
            acct = result.scalar_one_or_none()
            if acct and acct.drawdown_floor is not None:
                # Derive HWM from existing floor: hwm = floor + max_drawdown
                self._balance_hwm = acct.drawdown_floor + self.max_total_drawdown
                logger.info(
                    f"Drawdown HWM initialized: {self.account} "
                    f"floor={acct.drawdown_floor} hwm={self._balance_hwm:.2f}"
                )

    async def _fetch_balance(self) -> float | None:
        """Fetch current account balance from Tradovate cashBalance API.
        Caches for 30 seconds to avoid hammering the API on every tick."""
        import httpx

        now = datetime.now(timezone.utc)
        if (
            self._cached_balance is not None
            and self._balance_fetched_at
            and (now - self._balance_fetched_at).total_seconds() < 30
        ):
            return self._cached_balance

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                headers = {"Authorization": f"Bearer {self.access_token}"}
                resp = await client.get(
                    f"{self.base_url}/cashBalance/list", headers=headers
                )
                if resp.status_code != 200:
                    return self._cached_balance

                # Find balance for this account
                # Need numeric account ID — resolve from account/list
                acct_resp = await client.get(
                    f"{self.base_url}/account/list", headers=headers
                )
                if acct_resp.status_code != 200:
                    return self._cached_balance

                numeric_id = None
                for a in acct_resp.json():
                    if a["name"] == self.account:
                        numeric_id = a["id"]
                        break

                for bal in resp.json():
                    if bal.get("accountId") == numeric_id:
                        self._cached_balance = float(bal.get("amount", 0))
                        self._balance_fetched_at = now
                        return self._cached_balance

        except Exception:
            pass
        return self._cached_balance

    async def _update_drawdown_hwm(self):
        """Compute live balance (cash + unrealized) and update drawdown floor if new HWM."""
        from app.models.db import AsyncSessionLocal
        from app.models.position import Position
        from app.models.broker_account import BrokerAccount
        from sqlalchemy import select, func
        import re

        _FUTURES_RE = re.compile(r'^(.+?)[FGHJKMNQUVXZ]\d{1,2}$')

        balance = await self._fetch_balance()
        if balance is None:
            return

        # Compute total unrealized P&L from live prices and open positions
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Position).where(
                    Position.broker == self.broker,
                    Position.account == self.account,
                    func.abs(Position.quantity) > 1e-9,
                )
            )
            positions = result.scalars().all()

            total_unrealized = 0.0
            for pos in positions:
                price_data = self._prices.get(pos.symbol)
                if not price_data:
                    # Try root
                    m = _FUTURES_RE.match(pos.symbol)
                    root = m.group(1) if m else pos.symbol
                    price_data = self._prices.get(root)
                if not price_data or not price_data.get("mid"):
                    continue

                mid = price_data["mid"]
                unrealized = (mid - (pos.avg_price or 0)) * pos.quantity * pos.multiplier
                total_unrealized += unrealized

            # Live balance = cash balance (includes realized) + unrealized
            live_balance = balance + total_unrealized

            # Update HWM
            if self._balance_hwm is None or live_balance > self._balance_hwm:
                self._balance_hwm = live_balance

                # Compute new drawdown floor and persist if changed
                new_floor = round(self._balance_hwm - self.max_total_drawdown, 2)

                result = await db.execute(
                    select(BrokerAccount).where(
                        BrokerAccount.broker == self.broker,
                        BrokerAccount.account_alias == self.account,
                    )
                )
                acct = result.scalar_one_or_none()
                if acct:
                    old_floor = acct.drawdown_floor
                    # Only update if floor increased (trailing drawdown only goes up)
                    if old_floor is None or new_floor > old_floor:
                        acct.drawdown_floor = new_floor
                        await db.commit()
                        logger.info(
                            f"Drawdown floor updated: {self.account} "
                            f"balance={live_balance:.2f} hwm={self._balance_hwm:.2f} "
                            f"floor={old_floor} -> {new_floor}"
                        )

    async def _check_trail_triggers(
        self, symbol: str, bid: float | None, ask: float | None, mid: float
    ):
        """Fire trailing stop when trigger price is hit."""
        triggers = self._trail_triggers.get(symbol, [])
        fired = []

        for trigger in triggers:
            direction = trigger["direction"]
            trigger_price = trigger["trigger_price"]

            hit = (
                (direction == "buy" and mid >= trigger_price)
                or (direction == "sell" and mid <= trigger_price)
            )
            if not hit:
                continue

            # Verify position is still open
            from app.models.db import AsyncSessionLocal
            from app.models.position import Position
            from sqlalchemy import select, func

            async with AsyncSessionLocal() as db:
                root = ''.join(c for c in symbol if c.isalpha())
                from sqlalchemy import or_
                result = await db.execute(
                    select(Position).where(
                        Position.broker == self.broker,
                        Position.account == self.account,
                        or_(
                            Position.symbol == symbol,
                            Position.symbol == root,
                        ),
                        func.abs(Position.quantity) > 1e-9,
                    )
                )
                pos = result.scalar_one_or_none()
                if not pos:
                    logger.info(f"Trail trigger skipped — position flat: {symbol}")
                    fired.append(trigger)
                    await self._cancel_trail_trigger_db(trigger.get("id"))
                    continue

            logger.info(
                f"Tradovate trail trigger hit: {symbol} mid={mid:.2f} "
                f"trigger={trigger_price:.2f} dir={direction}"
            )
            success = await self._place_trailing_stop(trigger)
            if success:
                fired.append(trigger)

        for t in fired:
            self._trail_triggers[symbol].remove(t)

    async def _cancel_trail_trigger_db(self, trigger_id: int | None):
        """Mark a trail trigger as cancelled in the DB."""
        if not trigger_id:
            return
        from app.models.db import AsyncSessionLocal
        from app.models.trail_trigger import TrailTrigger
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(TrailTrigger).where(TrailTrigger.id == trigger_id)
            )
            t = result.scalar_one_or_none()
            if t:
                t.status = "cancelled"
                await db.commit()

    async def _place_trailing_stop(self, trigger: dict) -> bool:
        """Place a trailing stop via the Tradovate REST API (startOrderStrategy)."""
        import httpx
        from app.models.db import AsyncSessionLocal
        from app.models.trail_trigger import TrailTrigger
        from sqlalchemy import select

        symbol = trigger["symbol"]
        trail_dist = trigger["trail_distance"]
        trigger_db_id = trigger.get("id")

        # Tradovate trailing stop uses startOrderStrategy with bracket offsets
        # trail_distance is already a price distance (converted by offset_converter)
        try:
            base_url = self._ws_url.replace("wss://md", "https://live").replace(
                "wss://md-d", "https://demo"
            ).replace("/websocket", "")
            # Use the main API URL instead
            if "md-d" in self._ws_url or "demo" in self._ws_url:
                api_url = "https://demo.tradovateapi.com/v1"
            else:
                api_url = "https://live.tradovateapi.com/v1"

            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
            }

            # First resolve the contract ID for this symbol
            async with httpx.AsyncClient(headers=headers, timeout=10.0) as client:
                # Find contract
                find_resp = await client.get(
                    f"{api_url}/contract/find",
                    params={"name": symbol},
                )
                if find_resp.status_code != 200:
                    logger.error(f"Trail stop: failed to find contract {symbol}: {find_resp.text}")
                    return False
                contract = find_resp.json()
                contract_id = contract.get("id")

                # Get account ID
                acct_resp = await client.get(f"{api_url}/account/list")
                if acct_resp.status_code != 200:
                    logger.error(f"Trail stop: failed to get accounts: {acct_resp.text}")
                    return False
                accounts = acct_resp.json()
                account_id = None
                for a in accounts:
                    if a["name"] == self.account:
                        account_id = a["id"]
                        break
                if not account_id:
                    logger.error(f"Trail stop: account {self.account} not found")
                    return False

                # Use placeOrder with trailing stop strategy
                direction = trigger["direction"]
                # For a trailing stop: if long, sell stop; if short, buy stop
                action = "Sell" if direction == "buy" else "Buy"

                body = {
                    "accountSpec": self.account,
                    "accountId": account_id,
                    "action": action,
                    "symbol": symbol,
                    "orderStrategyTypeId": 2,  # trailing stop
                    "params": json.dumps({
                        "entryVersion": {
                            "orderQty": 1,
                            "orderType": "TrailingStop",
                        },
                        "bracket1": {
                            "qty": 1,
                            "profitTarget": -1,
                            "stopLoss": trail_dist,
                            "trailingStop": True,
                        },
                    }),
                }
                resp = await client.post(f"{api_url}/orderStrategy/startOrderStrategy", json=body)

                if resp.status_code in (200, 201):
                    logger.info(
                        f"Tradovate trail stop placed: {symbol} dist={trail_dist} "
                        f"account={self.account}"
                    )
                    if trigger_db_id:
                        async with AsyncSessionLocal() as db:
                            result = await db.execute(
                                select(TrailTrigger).where(TrailTrigger.id == trigger_db_id)
                            )
                            t = result.scalar_one_or_none()
                            if t:
                                t.status = "fired"
                                t.fired_at = datetime.now(timezone.utc)
                                await db.commit()
                    return True
                else:
                    logger.error(
                        f"Tradovate trail stop failed: {resp.status_code} {resp.text}"
                    )
                    if trigger_db_id:
                        async with AsyncSessionLocal() as db:
                            result = await db.execute(
                                select(TrailTrigger).where(TrailTrigger.id == trigger_db_id)
                            )
                            t = result.scalar_one_or_none()
                            if t:
                                t.status = "error"
                                t.error_detail = resp.text
                                await db.commit()
                    return False

        except Exception as e:
            logger.exception(f"Error placing Tradovate trail stop for {symbol}: {e}")
            return False
