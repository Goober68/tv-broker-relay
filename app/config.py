from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # ── Database ───────────────────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://relay:relay@localhost:5432/relay"
    postgres_password: str = "sBerk8me$"  # used by docker-compose, not the app directly

    # ── JWT ───────────────────────────────────────────────────────────────────
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 15
    jwt_refresh_token_expire_days: int = 7

    # ── Credential Encryption ─────────────────────────────────────────────────
    credential_encryption_key: str = "change-me-generate-a-real-fernet-key-=="

    # ── Stripe ────────────────────────────────────────────────────────────────
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_success_url: str = "https://yourdomain.com/billing/success"
    stripe_cancel_url: str = "https://yourdomain.com/billing/cancel"

    # ── Risk Defaults (global fallback, overridden by plan) ───────────────────
    max_position_size: float = 100_000
    max_daily_loss: float = 5_000
    duplicate_window_seconds: int = 10

    # ── Background Tasks ──────────────────────────────────────────────────────
    fill_poll_interval_seconds: int = 30       # how often to check open order status
    pnl_poll_interval_seconds: int = 60        # how often to poll live P&L from brokers
    reconcile_interval_seconds: int = 300      # how often to sync positions vs broker
    ibkr_keepalive_interval_seconds: int = 55  # how often to tickle IBKR gateway
    daily_summary_hour_utc: int = 7            # UTC hour to send daily P&L emails (0-23)

    # ── Email (SMTP) ──────────────────────────────────────────────────────────
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = "noreply@yourdomain.com"
    smtp_use_tls: bool = True
    # Set to false to disable all outbound email (useful in dev/test)
    email_enabled: bool = True

    # ── Legacy single-tenant config (dev/testing) ─────────────────────────────
    webhook_secret: str = "dev-secret"
    oanda_api_key: str = ""
    oanda_account_id: str = ""
    oanda_base_url: str = "https://api-fxpractice.oanda.com/v3"
    ibkr_gateway_url: str = "https://localhost:5000/v1/api"
    ibkr_account_id: str = ""
    tradovate_username: str = ""
    tradovate_password: str = ""
    tradovate_app_id: str = ""
    tradovate_app_version: str = "1.0"
    tradovate_base_url: str = "https://demo.tradovateapi.com/v1"
    etrade_consumer_key: str = ""
    etrade_consumer_secret: str = ""
    etrade_oauth_token: str = ""
    etrade_oauth_token_secret: str = ""
    etrade_account_id: str = ""
    etrade_base_url: str = "https://apisb.etrade.com"

    class Config:
        env_file = ".env"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
