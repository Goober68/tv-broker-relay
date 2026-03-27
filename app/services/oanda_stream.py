"""
OandaStreamManager — manages persistent HTTP streaming connections to Oanda.

Two streams per account:
  1. Pricing stream  — real-time bid/ask for open position symbols
     → Updates position.last_price and unrealized_pnl
     → Checks pending TrailTrigger rows and fires trailing stops

  2. Transaction stream — real-time account events (fills, closes, TP/SL hits)
     → Immediately reconciles positions on close/reduce events

Lifecycle:
  - Started by background task when Oanda positions exist
  - Stopped when all positions are flat
  - Reloads pending TrailTrigger rows from DB on (re)connect
"""
import asyncio
import logging
import httpx
import json
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Registry of active stream managers: (broker, account) → OandaStreamManager
_managers: dict[tuple[str, str], "OandaStreamManager"] = {}


def get_manager(broker: str, account: str) -> "OandaStreamManager | None":
    return _managers.get((broker, account))


def get_or_create_manager(
    broker: str,
    account: str,
    api_key: str,
    account_id: str,
    base_url: str,
) -> "OandaStreamManager":
    key = (broker, account)
    if key not in _managers:
        _managers[key] = OandaStreamManager(
            broker=broker,
            account=account,
            api_key=api_key,
            account_id=account_id,
            base_url=base_url,
        )
    return _managers[key]


def remove_manager(broker: str, account: str):
    _managers.pop((broker, account), None)


class OandaStreamManager:
    """
    Manages price + transaction streams for a single Oanda account.
    """

    def __init__(
        self,
        broker: str,
        account: str,
        api_key: str,
        account_id: str,
        base_url: str,
    ):
        self.broker     = broker
        self.account    = account
        self.api_key    = api_key
        self.account_id = account_id
        # Oanda streaming uses stream-fxtrade.oanda.com not api-fxtrade
        # Strip any path suffix (e.g. /v3) — we add it explicitly in the stream URLs
        stream_base = base_url.replace("api-fxtrade", "stream-fxtrade").replace(
            "api-fxpractice", "stream-fxpractice"
        )
        # Remove /v3 or any path component — stream URLs are built with full paths
        from urllib.parse import urlparse
        parsed = urlparse(stream_base)
        self.stream_url = f"{parsed.scheme}://{parsed.netloc}"
        self.api_url    = base_url.rstrip("/")

        self._headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept-Datetime-Format": "RFC3339",
        }

        self._price_task:  asyncio.Task | None = None
        self._tx_task:     asyncio.Task | None = None
        self._subscribed:  set[str] = set()  # symbols currently subscribed
        self._running:     bool = False

        # In-memory cache of pending trail triggers: symbol → list of trigger dicts
        self._trail_triggers: dict[str, list[dict]] = {}

        # Latest prices: symbol → {"bid": float, "ask": float, "mid": float}
        self._prices: dict[str, dict] = {}

    # ── Public API ─────────────────────────────────────────────────────────────

    def is_running(self) -> bool:
        return self._running and (
            self._price_task is not None and not self._price_task.done()
        )

    async def start(self, symbols: set[str]):
        """Start streaming for the given symbols. Idempotent."""
        if self._running and symbols == self._subscribed:
            return
        self._subscribed = set(symbols)
        self._running    = True
        await self._reload_trail_triggers()
        self._restart_tasks()
        logger.info(
            f"Oanda stream started for {self.broker}/{self.account}: {symbols}"
        )

    async def stop(self):
        """Stop all streams."""
        self._running = False
        if self._price_task and not self._price_task.done():
            self._price_task.cancel()
        if self._tx_task and not self._tx_task.done():
            self._tx_task.cancel()
        self._price_task = None
        self._tx_task    = None
        logger.info(f"Oanda stream stopped for {self.broker}/{self.account}")

    async def update_symbols(self, symbols: set[str]):
        """Update subscribed symbols — restart price stream if changed."""
        if symbols != self._subscribed:
            self._subscribed = set(symbols)
            if self._price_task and not self._price_task.done():
                self._price_task.cancel()
            self._price_task = asyncio.create_task(
                self._run_price_stream(),
                name=f"oanda_price_{self.account}",
            )

    def get_price(self, symbol: str) -> dict | None:
        return self._prices.get(symbol)

    async def add_trail_trigger(self, trigger: dict):
        """Add a pending trail trigger (called from order_processor after fill)."""
        symbol = trigger["symbol"]
        if symbol not in self._trail_triggers:
            self._trail_triggers[symbol] = []
        self._trail_triggers[symbol].append(trigger)
        logger.info(
            f"Trail trigger added: {symbol} trigger={trigger['trigger_price']} "
            f"dist={trigger['trail_distance']}"
        )

    # ── Private ────────────────────────────────────────────────────────────────

    def _restart_tasks(self):
        if self._price_task and not self._price_task.done():
            self._price_task.cancel()
        if self._tx_task and not self._tx_task.done():
            self._tx_task.cancel()
        self._price_task = asyncio.create_task(
            self._run_price_stream(),
            name=f"oanda_price_{self.account}",
        )
        self._tx_task = asyncio.create_task(
            self._run_transaction_stream(),
            name=f"oanda_tx_{self.account}",
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
                    TrailTrigger.broker  == self.broker,
                    TrailTrigger.account == self.account,
                    TrailTrigger.status  == "pending",
                )
            )
            triggers = result.scalars().all()
            for t in triggers:
                if t.symbol not in self._trail_triggers:
                    self._trail_triggers[t.symbol] = []
                self._trail_triggers[t.symbol].append({
                    "id":             t.id,
                    "symbol":         t.symbol,
                    "direction":      t.direction,
                    "trigger_price":  t.trigger_price,
                    "trail_distance": t.trail_distance,
                    "trade_id":       t.trade_id,
                    "tenant_id":      str(t.tenant_id),
                })
        logger.info(
            f"Loaded {sum(len(v) for v in self._trail_triggers.values())} "
            f"pending trail triggers for {self.account}"
        )

    async def _run_price_stream(self):
        """Stream real-time prices, update P&L, check trail triggers."""
        retry_delay = 5
        while self._running:
            if not self._subscribed:
                await asyncio.sleep(5)
                continue
            instruments = ",".join(self._subscribed)
            url = (
                f"{self.stream_url}/v3/accounts/{self.account_id}"
                f"/pricing/stream?instruments={instruments}"
            )
            try:
                async with httpx.AsyncClient(
                    headers=self._headers, timeout=None
                ) as client:
                    async with client.stream("GET", url) as resp:
                        resp.raise_for_status()
                        retry_delay = 5  # reset on successful connect
                        logger.info(
                            f"Oanda price stream connected: {self.account} {instruments}"
                        )
                        async for line in resp.aiter_lines():
                            if not self._running:
                                return
                            if not line.strip():
                                continue
                            try:
                                msg = json.loads(line)
                            except json.JSONDecodeError:
                                continue

                            if msg.get("type") == "PRICE":
                                await self._handle_price(msg)
                            elif msg.get("type") == "HEARTBEAT":
                                pass  # keepalive — ignore

            except asyncio.CancelledError:
                return
            except Exception as e:
                if self._running:
                    logger.warning(
                        f"Oanda price stream error ({self.account}): {e}. "
                        f"Retrying in {retry_delay}s"
                    )
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 60)

    async def _run_transaction_stream(self):
        """Stream account transactions for instant position reconciliation."""
        retry_delay = 5
        while self._running:
            url = (
                f"{self.stream_url}/v3/accounts/{self.account_id}"
                f"/transactions/stream"
            )
            try:
                async with httpx.AsyncClient(
                    headers=self._headers, timeout=None
                ) as client:
                    async with client.stream("GET", url) as resp:
                        resp.raise_for_status()
                        retry_delay = 5
                        logger.info(
                            f"Oanda transaction stream connected: {self.account}"
                        )
                        async for line in resp.aiter_lines():
                            if not self._running:
                                return
                            if not line.strip():
                                continue
                            try:
                                msg = json.loads(line)
                            except json.JSONDecodeError:
                                continue

                            tx_type = msg.get("type", "")
                            if tx_type in (
                                "ORDER_FILL",
                                "TRADE_CLOSE",
                                "TAKE_PROFIT_ORDER_FILL",
                                "STOP_LOSS_ORDER_FILL",
                                "TRAILING_STOP_LOSS_ORDER_FILL",
                                "MARKET_ORDER_TRADE_CLOSE",
                            ):
                                await self._handle_transaction(msg)
                            elif tx_type == "HEARTBEAT":
                                pass

            except asyncio.CancelledError:
                return
            except Exception as e:
                if self._running:
                    logger.warning(
                        f"Oanda transaction stream error ({self.account}): {e}. "
                        f"Retrying in {retry_delay}s"
                    )
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 60)

    async def _handle_price(self, msg: dict):
        """Process a price tick — update P&L and check trail triggers."""
        symbol   = msg.get("instrument", "")
        bids     = msg.get("bids", [{}])
        asks     = msg.get("asks", [{}])
        bid      = float(bids[0].get("price", 0)) if bids else 0.0
        ask      = float(asks[0].get("price", 0)) if asks else 0.0
        mid      = (bid + ask) / 2 if bid and ask else 0.0

        self._prices[symbol] = {"bid": bid, "ask": ask, "mid": mid}

        # Update position P&L in DB
        await self._update_position_pnl(symbol, mid)

        # Check trail triggers for this symbol
        if symbol in self._trail_triggers:
            await self._check_trail_triggers(symbol, bid, ask, mid)

    async def _update_position_pnl(self, symbol: str, mid: float):
        """Update last_price and unrealized_pnl for open positions."""
        from app.models.db import AsyncSessionLocal
        from app.models.position import Position
        from sqlalchemy import select, func

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Position).where(
                    Position.broker  == self.broker,
                    Position.account == self.account,
                    Position.symbol  == symbol,
                    func.abs(Position.quantity) > 1e-9,
                )
            )
            positions = result.scalars().all()
            if not positions:
                return

            for pos in positions:
                pos.last_price    = mid
                pos.last_price_at = datetime.now(timezone.utc)
                if pos.avg_price and pos.quantity and pos.multiplier:
                    direction = 1 if pos.quantity > 0 else -1
                    pos.unrealized_pnl = (
                        (mid - pos.avg_price)
                        * abs(pos.quantity)
                        * pos.multiplier
                        * direction
                    )
            await db.commit()

    async def _check_trail_triggers(
        self, symbol: str, bid: float, ask: float, mid: float
    ):
        """Fire trailing stop when trigger price is hit."""
        triggers = self._trail_triggers.get(symbol, [])
        fired = []

        for trigger in triggers:
            direction     = trigger["direction"]
            trigger_price = trigger["trigger_price"]

            # Buy position: trail triggers when price rises above trigger
            # Sell position: trail triggers when price falls below trigger
            hit = (
                (direction == "buy"  and mid >= trigger_price) or
                (direction == "sell" and mid <= trigger_price)
            )
            if not hit:
                continue

            # Verify position is still open before firing
            from app.models.db import AsyncSessionLocal
            from app.models.position import Position
            from sqlalchemy import select, func
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(Position).where(
                        Position.broker  == self.broker,
                        Position.account == self.account,
                        Position.symbol  == symbol,
                        func.abs(Position.quantity) > 1e-9,
                    )
                )
                pos = result.scalar_one_or_none()
                if not pos:
                    logger.info(
                        f"Trail trigger skipped — position flat: {symbol}"
                    )
                    fired.append(trigger)  # Remove stale trigger
                    await self._cancel_trail_trigger_db(trigger.get("id"))
                    continue

            logger.info(
                f"Trail trigger hit: {symbol} mid={mid:.5f} "
                f"trigger={trigger_price:.5f} dir={direction}"
            )
            success = await self._place_trailing_stop(trigger, mid)
            if success:
                fired.append(trigger)

        # Remove fired/cancelled triggers
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

    async def _place_trailing_stop(self, trigger: dict, current_price: float) -> bool:
        """Place a native Oanda trailing stop order for a triggered position."""
        from app.models.db import AsyncSessionLocal
        from app.models.trail_trigger import TrailTrigger
        from app.brokers.oanda import _fmt_price
        from sqlalchemy import select

        symbol        = trigger["symbol"]
        trail_dist    = trigger["trail_distance"]
        trade_id      = trigger.get("trade_id")
        trigger_db_id = trigger.get("id")

        try:
            async with httpx.AsyncClient(
                headers=self._headers, timeout=10.0
            ) as client:
                if trade_id:
                    # PATCH /v3/accounts/{id}/trades/{tradeID}/orders
                    # Attaches a trailing stop to a specific open trade
                    url  = f"{self.api_url}/v3/accounts/{self.account_id}/trades/{trade_id}/orders"
                    body = {
                        "trailingStopLoss": {
                            "distance":    _fmt_price(symbol, trail_dist),
                            "timeInForce": "GTC",
                        }
                    }
                    resp = await client.patch(url, json=body)
                else:
                    # POST /v3/accounts/{id}/orders
                    # Places a TRAILING_STOP_LOSS order covering all open trades
                    url  = f"{self.api_url}/v3/accounts/{self.account_id}/orders"
                    body = {
                        "order": {
                            "type":        "TRAILING_STOP_LOSS",
                            "distance":    _fmt_price(symbol, trail_dist),
                            "timeInForce": "GTC",
                        }
                    }
                    resp = await client.post(url, json=body)

                if resp.status_code in (200, 201):
                    logger.info(
                        f"Trail stop placed: {symbol} dist={trail_dist:.5f} "
                        f"trade_id={trade_id}"
                    )
                    # Mark DB record as fired
                    if trigger_db_id:
                        async with AsyncSessionLocal() as db:
                            result = await db.execute(
                                select(TrailTrigger).where(
                                    TrailTrigger.id == trigger_db_id
                                )
                            )
                            t = result.scalar_one_or_none()
                            if t:
                                t.status   = "fired"
                                t.fired_at = datetime.now(timezone.utc)
                                await db.commit()
                    return True
                else:
                    logger.error(
                        f"Trail stop placement failed: {resp.status_code} {resp.text}"
                    )
                    if trigger_db_id:
                        async with AsyncSessionLocal() as db:
                            result = await db.execute(
                                select(TrailTrigger).where(
                                    TrailTrigger.id == trigger_db_id
                                )
                            )
                            t = result.scalar_one_or_none()
                            if t:
                                t.status       = "error"
                                t.error_detail = resp.text
                                await db.commit()
                    return False

        except Exception as e:
            logger.exception(f"Error placing trail stop for {symbol}: {e}")
            return False

    async def _handle_transaction(self, msg: dict):
        """Process a transaction event — reconcile positions instantly."""
        from app.models.db import AsyncSessionLocal
        from app.models.position import Position
        from sqlalchemy import select, func

        tx_type    = msg.get("type", "")
        instrument = msg.get("instrument") or msg.get("tradesClosed", [{}])[0].get("instrument", "")

        # For close events, extract instrument from trades closed
        if not instrument and "tradesClosed" in msg:
            for trade in msg.get("tradesClosed", []):
                instrument = trade.get("instrument", "")
                if instrument:
                    break

        if not instrument:
            return

        logger.info(
            f"Oanda transaction: {tx_type} {instrument} account={self.account}"
        )

        # Fetch current broker position quantity
        try:
            async with httpx.AsyncClient(
                headers=self._headers, timeout=10.0
            ) as client:
                resp = await client.get(
                    f"{self.api_url}/v3/accounts/{self.account_id}"
                    f"/positions/{instrument}"
                )
                if resp.status_code == 200:
                    data      = resp.json()
                    pos_data  = data.get("position", {})
                    long_qty  = float(pos_data.get("long",  {}).get("units", 0))
                    short_qty = float(pos_data.get("short", {}).get("units", 0))
                    net_qty   = long_qty + short_qty  # short_qty is negative

                    async with AsyncSessionLocal() as db:
                        result = await db.execute(
                            select(Position).where(
                                Position.broker  == self.broker,
                                Position.account == self.account,
                                Position.symbol  == instrument,
                            )
                        )
                        pos = result.scalar_one_or_none()
                        if pos:
                            old_qty       = pos.quantity
                            pos.quantity  = net_qty
                            if abs(net_qty) < 1e-9:
                                pos.unrealized_pnl = None
                                pos.last_price     = None
                                pos.last_price_at  = None
                                # Remove symbol from price subscription if flat
                                self._subscribed.discard(instrument)
                                logger.info(
                                    f"Position closed via stream: {instrument} "
                                    f"(was {old_qty:.0f})"
                                )
                            await db.commit()

        except Exception as e:
            logger.exception(
                f"Error reconciling position from transaction stream: {e}"
            )
