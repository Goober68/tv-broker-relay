# TradingView Broker Relay — Claude Context

## What this is
Multi-tenant SaaS that relays TradingView webhook alerts to broker APIs.
Live at https://tvbrokerrelay.com, deployed on EC2 at /opt/tv-broker-relay.

## Stack
- **Backend:** FastAPI + SQLAlchemy (async) + PostgreSQL + Alembic
- **Frontend:** React + Vite + Tailwind, built to app/static/, served by FastAPI SPA fallback
- **Infrastructure:** Docker Compose + Caddy (TLS) on Ubuntu EC2
- **Single uvicorn worker** (`--workers 1`) — required, background tasks use asyncio in-process

## Repo layout
```
app/
  main.py                  # FastAPI app, lifespan, router registration
  config.py                # pydantic-settings (reads .env)
  models/                  # SQLAlchemy ORM models
    order.py               # Order, enums, BROKER_INSTRUMENT_SUPPORT
    tenant.py              # Tenant, RefreshToken
    broker_account.py      # BrokerAccount (credentials encrypted)
    position.py            # Position (running P&L state)
    trail_trigger.py       # TrailTrigger (Oanda streaming TSL)
    plan.py                # Subscription
    api_key.py             # ApiKey
    webhook_delivery.py    # WebhookDelivery (audit log)
  brokers/
    base.py                # BrokerBase, BrokerOrderResult, OrderStatusResult
    oanda.py               # Oanda REST + FIFO avoidance
    tradovate.py           # placeOrder / placeOSO / startOrderStrategy routing
    ibkr.py                # IBKR Client Portal Gateway
    etrade.py              # E*Trade OAuth1
    alpaca.py
    tastytrade.py
    tradestation.py
    rithmic.py
    registry.py            # get_broker_for_tenant() — decrypts creds, injects fifo/alias
  routers/
    webhook.py             # POST /webhook/{tenant_id} — main entry point
    status.py              # orders, positions, deliveries, SSE /api/events
    auth.py
    broker_accounts.py
    billing.py
    admin.py
    api_keys.py
  services/
    order_processor.py     # Full webhook pipeline (dedup→risk→submit→state)
    offset_converter.py    # ticks/pips/points → absolute price conversion
    oanda_stream.py        # Oanda price + transaction SSE streams, trail trigger firing
    background_tasks.py    # fill_poll, pnl_poll, reconcile, auto_close, oanda_stream mgr
    events.py              # In-process SSE pub/sub bus (push to browser clients)
    auth.py                # JWT encode/decode, bcrypt
    credentials.py         # Fernet encrypt/decrypt for broker credentials
    plan_enforcer.py       # Rate limits, order type restrictions, monthly volume
    plans.py               # Plan definitions and order counter
    state.py               # apply_fill_to_position, P&L calculation
    email_service.py       # SMTP notifications
    stripe_service.py      # Stripe webhook handling
  dependencies/
    auth.py                # get_current_tenant, require_admin FastAPI deps
  schemas/
    webhook.py             # WebhookPayload (Pydantic), OrderResponse
frontend/src/
  pages/
    Dashboard.jsx          # Live positions + P&L + recent orders
    Orders.jsx             # Full order history with expand/detail
    WebhookSetup.jsx       # Setup guide + live delivery log (3-panel JSON, SSE-driven)
    BrokerAccounts.jsx     # Broker credential management, FIFO toggle, auto-close
    Billing.jsx
    ApiKeys.jsx
    AdminTenants.jsx
    AdminStats.jsx
  hooks/
    useApi.js              # useApi (one-shot) + usePolling (interval)
    useEventSource.js      # SSE client with exponential backoff reconnect
  lib/
    api.js                 # Typed API client, JWT memory storage, auto-refresh
    auth-context.jsx
  components/
    ui.jsx                 # StatCard, PnlValue, StatusBadge, Mono, etc.
    PnlCharts.jsx          # Recharts P&L bar/line charts
alembic/versions/          # Migration chain: 0001→0009 (head: trail_triggers)
```

## Key architectural decisions

### Database
- Tenant ID: UUID PK
- SQLAlchemy enums: `native_enum=False` (VARCHAR strings) — no migration needed when adding enum values
- bcrypt direct (no passlib)
- Alembic single chain, never branch

### Auth
- JWT access tokens (15min) in memory, refresh tokens (7 days) in httpOnly cookies
- Webhook auth: `secret` field in payload only (no X-Webhook-Secret header)
- SSE endpoint `/api/events` accepts `?token=` query param (EventSource can't set headers)

### Webhook pipeline (order_processor.py)
1. Deduplication (in-memory cache, configurable window)
2. Cancel-replace lookup
3. Plan enforcement (rate limit, order type, monthly volume)
4. Risk checks (position size, daily loss)
5. SL/TP offset conversion (offset_converter.py)
6. FIFO quantity resolution (Oanda only, queries /openTrades for live sizes)
7. Broker submission
8. Position state update
9. SSE push to connected browser clients

### SL/TP conversion
- `sl_tp_type`: `"absolute"` | `"ticks"` | `"pips"` | `"points"` | `null` (infer)
- For Oanda market orders with offset sl_tp_type: uses live stream mid price as entry price
- Stream mid always overrides payload `price` field for Oanda when sl_tp_type is an offset type

### Oanda specifics
- FIFO avoidance: queries /openTrades, walks ±1,±2,... from base qty to find first gap
- Price stream: updates last_price only — unrealized_pnl comes from REST poll (/openPositions)
- Trail triggers: stored as TrailTrigger rows, fired by price stream when mid crosses trigger_price
- trailingStopLossOnFill NOT sent at order time — trail stop placed by stream when trigger hits
- Market orders: FOK only. Limit/stop orders: GFD or GTC
- `account_alias` passed through registry → broker for stream manager lookup

### Tradovate specifics
- Order routing: placeOrder (plain) → placeOSO (SL/TP) → startOrderStrategy (trailing)
- startOrderStrategy: bracket values are RELATIVE offsets, not absolute prices
- params field is a JSON-encoded string (Tradovate API requirement)
- entryVersion includes price for limit/stop orders

### P&L
- realized_pnl / daily_realized_pnl stored in account currency (multiplier already applied)
- unrealized_pnl from broker API (Oanda: /openPositions unrealizedPL field, already in USD)
- Never multiply P&L fields by multiplier on display — it's already baked in

### SSE real-time updates
- events.py: asyncio.Queue per connected client, push_delivery_event() fans out
- Heartbeat every 15s (": heartbeat") to keep connections alive through Caddy
- X-Accel-Buffering: no header tells Caddy not to buffer the stream
- Frontend falls back to 30s polling if SSE connection fails

## Deployment commands

```bash
# Full rebuild and restart
docker compose up --build -d

# App only (Python changes)
docker compose build app && docker compose up -d app

# Frontend only
docker compose build app && docker compose up -d app
# (frontend is built inside the app Docker image)

# View logs
docker compose logs -f app
docker compose logs --tail 200 app | grep ERROR

# Run migrations
docker compose run --rm migrate alembic upgrade head

# New migration after model change
docker compose run --rm migrate alembic revision --autogenerate -m "description"

# Database queries
docker compose exec db psql -U relay -d relay -c "SELECT ..."

# Restart without rebuild
docker compose restart app
```

## Frontend build
```bash
# Dockerfile build command (PATH workaround):
RUN node node_modules/vite/bin/vite.js build --outDir /frontend/dist
# NOT: RUN npm run build  (vite not on PATH in Alpine)
```

## Common DB queries

```bash
# Clear stale Oanda unrealized P&L (after deploying pnl fix)
docker compose exec db psql -U relay -d relay -c "UPDATE positions SET unrealized_pnl = NULL, last_price = NULL, last_price_at = NULL WHERE broker = 'oanda';"

# Check trail triggers
docker compose exec db psql -U relay -d relay -c "SELECT id, symbol, direction, trigger_price, trail_distance, trade_id, status, fired_at FROM trail_triggers ORDER BY id DESC LIMIT 10;"

# Recent orders
docker compose exec db psql -U relay -d relay -c "SELECT id, symbol, broker, status, error_message FROM orders ORDER BY id DESC LIMIT 20;"

# Promote user to admin
docker compose exec db psql -U relay -d relay -c "UPDATE tenants SET is_admin = true WHERE email = 'you@example.com';"
```

## Testing
```bash
pytest -v                          # full suite
pytest tests/test_oanda.py -v      # single file
pytest -k "test_sl_tp" -v          # by name pattern
```

## Things to be careful about

- **Never multiply P&L by multiplier on display** — already in account currency in DB
- **native_enum=False** — always pass this to SAEnum, never use PostgreSQL native enums
- **Single worker** — in-memory state (dedup cache, SSE queues, stream manager registry) is safe; would break with multiple workers
- **broker_request saved on ALL paths** — including success, not just failures; needed for 3-panel delivery view
- **Oanda account_alias vs account_id** — registry injects account_alias into creds; use self.account_alias for stream manager lookup, not self.account_id
- **TrailTrigger.trail_distance** is a price distance (already converted from ticks/pips by offset_converter), not a tick count
- **Tradovate startOrderStrategy bracket values** are relative offsets signed by direction, not absolute prices
- **GFD is valid for Oanda limit/stop orders** but NOT market orders (FOK only for market)
- **SSE /api/events auth** uses ?token= query param, not Authorization header
