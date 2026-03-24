# TradingView Broker Relay

Multi-tenant service that accepts TradingView webhook alerts and executes trades against Oanda, IBKR, Tradovate, and E*Trade.

## Architecture

```
TradingView Alert
      │  POST /webhook/{tenant_id}
      │  X-Webhook-Secret: tvr_...
      ▼
   Caddy (TLS)
      │
      ▼
  FastAPI app
      │
      ├── Plan enforcement (rate limit, order type, volume)
      ├── Risk checks (position size, daily loss)
      ├── Broker adapter (credentials from encrypted DB)
      └── Position state tracking
      │
      ▼
  PostgreSQL
```

## Deployment

### Prerequisites

- Ubuntu 22.04+ or Debian 12+ VPS (2GB RAM minimum, 4GB recommended)
- A domain name with DNS A record pointing to your server
- Ports 80 and 443 open in your firewall

### 1. Get the code onto your server

```bash
scp -r ./tv-broker-relay user@yourserver:/opt/tv-broker-relay
# or clone from git:
# git clone https://github.com/you/tv-broker-relay /opt/tv-broker-relay
```

### 2. Configure

```bash
cd /opt/tv-broker-relay
cp .env.example .env
nano .env          # fill in all required values (see below)
nano Caddyfile     # replace yourdomain.com with your actual domain
```

Required `.env` values before first deploy:

| Variable | How to generate |
|---|---|
| `POSTGRES_PASSWORD` | `openssl rand -base64 32` |
| `JWT_SECRET` | `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| `CREDENTIAL_ENCRYPTION_KEY` | `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `STRIPE_SECRET_KEY` | From Stripe dashboard → Developers → API keys |
| `STRIPE_WEBHOOK_SECRET` | From Stripe dashboard → Webhooks (after registering endpoint) |
| `STRIPE_SUCCESS_URL` | `https://yourdomain.com/billing/success` |
| `STRIPE_CANCEL_URL` | `https://yourdomain.com/billing/cancel` |

### 3. Deploy

```bash
# First time (installs Docker, sets up everything):
bash deploy.sh --setup

# Subsequent updates:
bash deploy.sh
```

The deploy script:
1. Installs Docker if needed
2. Auto-generates any missing secrets
3. Validates `.env` for placeholder values
4. Builds the Docker image
5. Runs database migrations (`alembic upgrade head`)
6. Starts all services
7. Waits for health check to pass

### 4. Register the Stripe webhook

In the Stripe dashboard → Developers → Webhooks → Add endpoint:
- URL: `https://yourdomain.com/billing/webhook`
- Events: `checkout.session.completed`, `customer.subscription.updated`, `customer.subscription.deleted`, `invoice.paid`, `invoice.payment_failed`

Copy the signing secret into `STRIPE_WEBHOOK_SECRET` in `.env`, then redeploy.

### 5. Set Stripe price IDs on plans

```bash
# After creating products/prices in Stripe dashboard:
curl -X PATCH https://yourdomain.com/admin/plans/2/stripe-price \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -d "stripe_price_id=price_abc123"
```

---

## TradingView Alert Setup

1. Register and log in at `https://yourdomain.com`
2. Create an API key: `POST /api-keys`
3. Add your broker account: `POST /broker-accounts`
4. In TradingView, create an alert:
   - **Webhook URL:** `https://yourdomain.com/webhook/{your_tenant_id}`
   - **Header:** `X-Webhook-Secret: tvr_{tenant_id}_{...your key...}`
   - **Message body:**

```json
{
  "secret": "ignored",
  "broker": "oanda",
  "account": "primary",
  "action": "{{strategy.order.action}}",
  "symbol": "{{ticker}}",
  "instrument_type": "forex",
  "order_type": "market",
  "quantity": 1000,
  "comment": "{{strategy.order.comment}}"
}
```

---

## Operations

### View logs

```bash
docker compose logs -f app        # app logs
docker compose logs -f caddy      # access logs / TLS events
docker compose logs -f db         # postgres logs
```

### Database backup

```bash
# Backup
docker compose exec db pg_dump -U relay relay | gzip > backup_$(date +%Y%m%d).sql.gz

# Restore
gunzip -c backup_20250101.sql.gz | docker compose exec -T db psql -U relay relay
```

### Run a manual migration

```bash
docker compose run --rm migrate alembic upgrade head

# Generate a new migration after model changes:
docker compose run --rm migrate alembic revision --autogenerate -m "add xyz column"
```

### Rollback a deployment

```bash
bash deploy.sh --rollback
# Note: this rolls back the app image only, not the database migration
```

### Scale app workers

```bash
# Edit docker-compose.yml CMD to increase --workers, then:
docker compose up -d app
```

### Create the first admin user

```bash
# Register normally, then promote via psql:
docker compose exec db psql -U relay relay \
  -c "UPDATE tenants SET is_admin = true WHERE email = 'you@example.com';"
```

---

## Local Development

```bash
# Start with hot reload, no Caddy:
docker compose -f docker-compose.yml -f docker-compose.dev.yml up

# Run tests:
pip install -r requirements-test.txt
pytest -v

# Run migrations manually in dev:
docker compose exec app alembic upgrade head

# Stripe webhook forwarding (local dev):
stripe listen --forward-to localhost:8000/billing/webhook
```

---

## API Reference

Interactive docs available at `https://yourdomain.com/docs` (Swagger UI).

### Auth
| Method | Path | Description |
|---|---|---|
| POST | `/auth/register` | Create account |
| POST | `/auth/login` | Get access token |
| POST | `/auth/refresh` | Rotate refresh token |
| POST | `/auth/logout` | Revoke session |
| GET | `/auth/me` | Current user + plan |

### Webhook
| Method | Path | Description |
|---|---|---|
| POST | `/webhook/{tenant_id}` | Receive TradingView alert |

### API Keys
| Method | Path | Description |
|---|---|---|
| GET | `/api-keys` | List keys |
| POST | `/api-keys` | Create key (raw shown once) |
| DELETE | `/api-keys/{id}` | Revoke key |

### Broker Accounts
| Method | Path | Description |
|---|---|---|
| GET | `/broker-accounts` | List accounts |
| POST | `/broker-accounts` | Add broker account |
| PATCH | `/broker-accounts/{id}` | Update credentials |
| DELETE | `/broker-accounts/{id}` | Remove account |
| GET | `/broker-accounts/{id}/instruments` | View instrument map |
| PUT | `/broker-accounts/{id}/instruments/{symbol}` | Add/update instrument |
| DELETE | `/broker-accounts/{id}/instruments/{symbol}` | Remove instrument |

### Orders & Positions
| Method | Path | Description |
|---|---|---|
| GET | `/api/orders` | Order history |
| GET | `/api/orders/open` | Open limit/stop orders |
| GET | `/api/positions` | Current positions |
| GET | `/api/positions/{broker}/{account}/{symbol}` | Single position |

### Billing
| Method | Path | Description |
|---|---|---|
| GET | `/billing/subscription` | Current plan + usage |
| GET | `/billing/plans` | Available plans |
| POST | `/billing/checkout` | Start Stripe checkout |
| POST | `/billing/portal` | Open Stripe portal |

### Admin (requires is_admin)
| Method | Path | Description |
|---|---|---|
| GET | `/admin/tenants` | List all tenants |
| POST | `/admin/tenants/{id}/plan` | Assign plan |
| PATCH | `/admin/tenants/{id}/active` | Enable/disable tenant |
| GET | `/admin/stats` | Platform stats |
| GET | `/admin/plans` | All plan definitions |
| PATCH | `/admin/plans/{id}/stripe-price` | Set Stripe price ID |
