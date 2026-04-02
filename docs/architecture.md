# TV Broker Relay — Architecture

## Service Architecture

```mermaid
graph TB
    TV[TradingView Webhooks] -->|HTTPS| Caddy
    Browser[Browser Dashboard] -->|HTTPS| Caddy
    
    subgraph Caddy["Caddy (TLS + Routing)"]
        direction LR
        R1["/webhook/*"]
        R2["everything else"]
    end
    
    R1 -->|:8001| Relay
    R2 -->|:8002| Dashboard

    subgraph Relay["Relay Service :8001"]
        WH[Webhook Handler]
        OP[Order Processor]
        BA[Broker Adapters]
        WH --> OP --> BA
    end

    subgraph Dashboard["Dashboard Service :8002"]
        API[REST API]
        SSE[SSE Endpoint]
        SPA[Frontend SPA]
        RL[Redis Listener]
        RL -->|fan-out| SSE
    end

    subgraph Worker["Worker Service :8003"]
        BG[Background Tasks]
        SM[Stream Managers]
        FP[Fill Poll]
        PE[P&L Engine]
        GE[GTD Expiry]
    end

    subgraph Infrastructure
        DB[(PostgreSQL)]
        RD[(Redis)]
    end

    Relay -->|read/write| DB
    Dashboard -->|read/write| DB
    Worker -->|read/write| DB

    Relay -->|publish SSE events| RD
    Relay -->|dedup, rate limit, read prices| RD
    Dashboard -->|subscribe SSE events| RD
    Worker -->|write stream prices| RD

    BA -->|HTTP| Brokers[Broker APIs]
    SM -->|WebSocket| Brokers
    FP -->|HTTP| Brokers
    GE -->|HTTP cancel| Brokers

    SSE -->|Server-Sent Events| Browser
```

## Webhook Processing Flow

```mermaid
sequenceDiagram
    participant TV as TradingView
    participant C as Caddy
    participant R as Relay
    participant RD as Redis
    participant DB as PostgreSQL
    participant B as Broker API
    participant W as Worker
    participant D as Dashboard
    participant BR as Browser

    TV->>C: POST /webhook/{tenant_id}
    C->>R: proxy :8001

    Note over R: Parse + validate payload

    R->>DB: SELECT Tenant (auth)
    R->>DB: SELECT ApiKey (verify secret)
    R->>RD: ZRANGEBYSCORE (rate limit check)
    R->>RD: SETNX dedup:{key} (dedup check)
    R->>DB: SELECT Subscription+Plan (plan enforcement)
    R->>DB: SELECT BrokerAccount (instrument map + creds)
    R->>DB: SELECT Position (risk check)
    R->>DB: SELECT SUM pending exposure

    opt SL/TP offset conversion needed
        R->>RD: HGET prices:{broker}:{account} {symbol}
        Note over R: Use stream mid price<br/>or fall back to payload price
    end

    R->>DB: INSERT Order (flush)

    R->>B: Submit order (HTTP)
    B-->>R: Order result

    alt Order filled
        R->>DB: UPDATE Position (apply fill)
    end
    R->>DB: UPDATE Subscription (increment counter)
    R->>DB: COMMIT

    R->>RD: PUBLISH sse:delivery {event}
    R-->>C: OrderResponse
    C-->>TV: 200 OK

    Note over R: Background (async)
    R->>DB: INSERT WebhookDelivery (delivery log)
    R->>DB: UPDATE ApiKey.last_used_at

    Note over W,D: Async event flow
    RD-->>D: sse:delivery message
    D->>D: Fan out to subscriber queues
    D-->>BR: SSE event: delivery
```

## Stream Price Flow

```mermaid
sequenceDiagram
    participant B as Broker API
    participant W as Worker (Stream Manager)
    participant RD as Redis
    participant R as Relay
    participant DB as PostgreSQL

    loop Every tick
        B->>W: Price tick (WebSocket)
        W->>W: Update in-memory cache
        W->>RD: HSET prices:{broker}:{account} {symbol} {json}
        W->>DB: UPDATE position.last_price
        
        opt Trail trigger check
            W->>W: Check if mid crosses trigger_price
            alt Trigger fired
                W->>B: Place trailing stop order
                W->>DB: UPDATE trail_trigger SET fired
            end
        end
    end

    Note over R: On webhook with offset SL/TP
    R->>RD: HGET prices:{broker}:{account} {symbol}
    alt Cache hit
        Note over R: Use stream mid for conversion
    else Cache miss
        Note over R: Fall back to payload price
    end
```

## Data Model (ERD)

```mermaid
erDiagram
    TENANTS {
        uuid id PK
        timestamp created_at
        timestamp updated_at
        varchar email UK
        varchar password_hash
        boolean is_active
        boolean is_admin
        boolean email_verified
    }

    REFRESH_TOKENS {
        int id PK
        uuid tenant_id FK
        varchar token_hash UK
        timestamp created_at
        timestamp expires_at
        boolean revoked
        varchar user_agent
        varchar ip_address
    }

    API_KEYS {
        int id PK
        uuid tenant_id FK
        timestamp created_at
        varchar name
        varchar key_hash UK
        varchar key_prefix
        boolean is_active
        timestamp last_used_at
    }

    PLANS {
        int id PK
        varchar name UK
        varchar display_name
        varchar stripe_price_id
        int max_broker_accounts
        int max_monthly_orders
        int max_open_orders
        int requests_per_minute
        json allowed_order_types
        float max_position_size
        float max_daily_loss
        boolean is_active
    }

    SUBSCRIPTIONS {
        int id PK
        uuid tenant_id FK UK
        int plan_id FK
        timestamp created_at
        timestamp updated_at
        varchar stripe_customer_id
        varchar stripe_subscription_id UK
        varchar status
        timestamp current_period_start
        timestamp current_period_end
        int orders_this_period
    }

    BROKER_ACCOUNTS {
        int id PK
        uuid tenant_id FK
        timestamp created_at
        timestamp updated_at
        varchar broker
        varchar account_alias
        varchar display_name
        text credentials_encrypted
        json instrument_map
        boolean is_active
        boolean fifo_randomize
        int fifo_max_offset
        boolean auto_close_enabled
        varchar auto_close_time
        varchar account_type
        float max_total_drawdown
        float max_daily_drawdown
        float drawdown_floor
        float commission_per_contract
    }

    ORDERS {
        int id PK
        uuid tenant_id FK
        timestamp created_at
        timestamp updated_at
        varchar broker
        varchar account
        varchar symbol
        enum instrument_type "forex equity future cfd option"
        varchar exchange
        varchar currency
        enum action "buy sell close"
        enum order_type "market limit stop stop_limit"
        float quantity
        float price
        enum time_in_force "GTC GTD DAY GFD IOC FOK"
        timestamp expire_at
        float multiplier
        boolean extended_hours
        varchar option_expiry
        float option_strike
        varchar option_right
        float option_multiplier
        float stop_loss
        float take_profit
        float trailing_distance
        float trail_trigger
        float trail_dist
        float trail_update
        enum status "pending submitted open filled partial cancelled rejected error"
        varchar broker_order_id
        varchar client_trade_id
        float broker_quantity
        float filled_quantity
        float avg_fill_price
        float commission
        varchar algo_id
        varchar algo_version
        text raw_payload
        varchar comment
        text error_message
        text broker_request
        text broker_response
    }

    POSITIONS {
        int id PK
        uuid tenant_id FK
        timestamp updated_at
        varchar broker
        varchar account
        varchar symbol
        varchar instrument_type
        float quantity
        float avg_price
        float multiplier
        float realized_pnl
        float daily_realized_pnl
        timestamp daily_pnl_date
        float last_price
        float unrealized_pnl
        timestamp last_price_at
    }

    TRAIL_TRIGGERS {
        int id PK
        uuid tenant_id
        int broker_account_id FK
        int order_id FK
        timestamp created_at
        timestamp updated_at
        varchar broker
        varchar account
        varchar symbol
        varchar direction
        float trigger_price
        float trail_distance
        varchar trade_id
        varchar status "pending fired cancelled error"
        timestamp fired_at
        text error_detail
    }

    WEBHOOK_DELIVERIES {
        int id PK
        uuid tenant_id FK
        timestamp created_at
        varchar source_ip
        varchar user_agent
        text raw_payload
        int http_status
        boolean auth_passed
        int order_id FK
        varchar outcome
        text error_detail
        float duration_ms
        float broker_latency_ms
    }

    TENANTS ||--o{ REFRESH_TOKENS : "has"
    TENANTS ||--o{ API_KEYS : "has"
    TENANTS ||--o| SUBSCRIPTIONS : "has"
    TENANTS ||--o{ BROKER_ACCOUNTS : "has"
    TENANTS ||--o{ ORDERS : "places"
    TENANTS ||--o{ POSITIONS : "holds"
    TENANTS ||--o{ WEBHOOK_DELIVERIES : "receives"
    PLANS ||--o{ SUBSCRIPTIONS : "assigned to"
    BROKER_ACCOUNTS ||--o{ TRAIL_TRIGGERS : "has"
    ORDERS ||--o{ TRAIL_TRIGGERS : "triggers"
    ORDERS ||--o{ WEBHOOK_DELIVERIES : "logged in"
```

## GTD Expiry Flow

```mermaid
sequenceDiagram
    participant W as Worker (GTD Expiry)
    participant DB as PostgreSQL
    participant B as Broker API

    loop Every 5 seconds
        W->>DB: SELECT orders WHERE status=open<br/>AND time_in_force=GTD<br/>AND expire_at <= NOW()
        
        alt Expired orders found
            loop Each expired order
                W->>B: poll_order_status(broker_order_id)
                alt Already filled
                    W->>DB: UPDATE order SET status=FILLED
                    W->>DB: UPDATE position (apply fill)
                    Note over W: Skip cancel —<br/>preserve bracket SL/TP legs
                else Still open
                    W->>B: cancel_order / interruptOrderStrategy
                    W->>DB: UPDATE order SET status=CANCELLED
                end
            end
        end
    end
```
