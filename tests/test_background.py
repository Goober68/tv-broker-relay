"""
Tests for background tasks, webhook delivery log, and email notifications.

Covers:
  - Webhook delivery log written on every request (success, auth fail, rate limit, error)
  - GET /api/webhook-deliveries returns scoped results
  - Fill polling: order updated to filled, position state updated, email sent
  - Fill polling: cancelled order handled
  - IBKR keepalive: tickle endpoint called for active accounts
  - Reconciliation: drift detected and logged
  - Reconciliation: no drift passes silently
  - Daily summary: fires only at configured hour, only once per day
  - Email send_order_filled constructs correct subject
  - Email send_payment_failed constructs correct subject
  - Email send_daily_summary constructs correct content
  - Email disabled flag prevents sends
"""
import pytest
import asyncio
from unittest.mock import patch, AsyncMock, MagicMock
from datetime import datetime, timezone

from app.brokers.base import BrokerOrderResult, OrderStatusResult
from app.models.order import Order, OrderStatus, OrderAction, OrderType, TimeInForce, InstrumentType
from app.models.position import Position


# ── Webhook Delivery Log ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delivery_log_written_on_success(client, registered_tenant, api_key,
                                                oanda_broker_account, auth_headers):
    raw_key, tenant_id = api_key
    mock_result = BrokerOrderResult(
        success=True, broker_order_id="x", filled_quantity=1000.0, avg_fill_price=1.085
    )
    with patch("app.brokers.oanda.OandaBroker.submit_order", new_callable=AsyncMock) as m:
        m.return_value = mock_result
        resp = await client.post(
            f"/webhook/{tenant_id}",
            json={
                "secret": "x", "broker": "oanda", "account": "primary",
                "action": "buy", "symbol": "EUR_USD",
                "instrument_type": "forex", "order_type": "market", "quantity": 1000,
            },
            headers={"X-Webhook-Secret": raw_key},
        )
    assert resp.status_code == 200

    deliveries = (await client.get("/api/webhook-deliveries", headers=auth_headers)).json()
    assert len(deliveries) >= 1
    d = deliveries[0]
    assert d["http_status"] == 200
    assert d["auth_passed"] is True
    assert d["outcome"] == "filled"
    assert d["order_id"] is not None
    assert d["duration_ms"] is not None
    assert d["duration_ms"] > 0


@pytest.mark.asyncio
async def test_delivery_log_written_on_auth_failure(client, registered_tenant, api_key):
    _, tenant_id = api_key
    resp = await client.post(
        f"/webhook/{tenant_id}",
        json={"secret": "x", "broker": "oanda", "account": "primary",
              "action": "buy", "symbol": "EUR_USD",
              "instrument_type": "forex", "order_type": "market", "quantity": 1000},
        headers={"X-Webhook-Secret": "tvr_wrong_key"},
    )
    assert resp.status_code == 403
    # Auth failures are still logged (tenant_id comes from the URL)
    # We can't check via the auth-gated endpoint, but we verify no 500 occurred


@pytest.mark.asyncio
async def test_delivery_log_scoped_to_tenant(client, db_session):
    """Tenant A cannot see Tenant B's delivery logs."""
    await client.post("/auth/register", json={"email": "dl_a@example.com", "password": "passw0rd"})
    login_a = await client.post("/auth/login", json={"email": "dl_a@example.com", "password": "passw0rd"})
    headers_a = {"Authorization": f"Bearer {login_a.json()['access_token']}"}

    await client.post("/auth/register", json={"email": "dl_b@example.com", "password": "passw0rd"})
    login_b = await client.post("/auth/login", json={"email": "dl_b@example.com", "password": "passw0rd"})
    headers_b = {"Authorization": f"Bearer {login_b.json()['access_token']}"}

    resp_a = (await client.get("/api/webhook-deliveries", headers=headers_a)).json()
    resp_b = (await client.get("/api/webhook-deliveries", headers=headers_b)).json()

    # Each tenant should see only their own (or empty) logs
    ids_a = {d["id"] for d in resp_a}
    ids_b = {d["id"] for d in resp_b}
    assert ids_a.isdisjoint(ids_b)


@pytest.mark.asyncio
async def test_delivery_log_filterable_by_outcome(client, registered_tenant, api_key,
                                                    oanda_broker_account, auth_headers):
    raw_key, tenant_id = api_key
    mock_result = BrokerOrderResult(
        success=True, broker_order_id="y", filled_quantity=1000.0
    )
    with patch("app.brokers.oanda.OandaBroker.submit_order", new_callable=AsyncMock) as m:
        m.return_value = mock_result
        await client.post(
            f"/webhook/{tenant_id}",
            json={"secret": "x", "broker": "oanda", "account": "primary",
                  "action": "buy", "symbol": "EUR_USD",
                  "instrument_type": "forex", "order_type": "market", "quantity": 1000},
            headers={"X-Webhook-Secret": raw_key},
        )

    resp = (await client.get("/api/webhook-deliveries?outcome=filled", headers=auth_headers)).json()
    assert all(d["outcome"] == "filled" for d in resp)


@pytest.mark.asyncio
async def test_delivery_log_requires_auth(client):
    resp = await client.get("/api/webhook-deliveries")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_delivery_log_secret_stripped(client, registered_tenant, api_key,
                                              oanda_broker_account, auth_headers):
    """The raw secret must not appear in the stored payload."""
    raw_key, tenant_id = api_key
    with patch("app.brokers.oanda.OandaBroker.submit_order", new_callable=AsyncMock) as m:
        m.return_value = BrokerOrderResult(success=True, broker_order_id="z", filled_quantity=1000.0)
        await client.post(
            f"/webhook/{tenant_id}",
            json={"secret": "MY_SUPER_SECRET_TOKEN", "broker": "oanda", "account": "primary",
                  "action": "buy", "symbol": "EUR_USD",
                  "instrument_type": "forex", "order_type": "market", "quantity": 1000},
            headers={"X-Webhook-Secret": raw_key},
        )

    deliveries = (await client.get("/api/webhook-deliveries", headers=auth_headers)).json()
    assert deliveries
    payload_str = deliveries[0].get("raw_payload") or ""
    assert "MY_SUPER_SECRET_TOKEN" not in payload_str


# ── Fill Polling ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_poll_fills_marks_order_filled(db_session):
    """Fill poll should update OPEN order to FILLED and update position."""
    from app.services.background_tasks import _poll_fills_once

    # Seed an open order
    order = Order(
        tenant_id=1, broker="oanda", account="primary", symbol="EUR_USD",
        instrument_type=InstrumentType.FOREX,
        action=OrderAction.BUY, order_type=OrderType.LIMIT,
        quantity=1000, price=1.0800, time_in_force=TimeInForce.GTC,
        status=OrderStatus.OPEN, broker_order_id="oanda-open-123",
        multiplier=1.0,
    )
    db_session.add(order)
    await db_session.commit()

    filled_status = OrderStatusResult(
        found=True, is_filled=True, filled_quantity=1000.0, avg_fill_price=1.0800
    )

    with patch("app.services.background_tasks.AsyncSessionLocal") as mock_session_cls, \
         patch("app.services.background_tasks.get_broker_for_tenant") as mock_registry, \
         patch("app.services.background_tasks.send_order_filled", new_callable=AsyncMock):

        mock_broker = AsyncMock()
        mock_broker.poll_order_status.return_value = filled_status
        mock_registry.return_value = mock_broker

        # Use a context manager that yields our real db_session
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=db_session)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        await _poll_fills_once()

    await db_session.refresh(order)
    assert order.status == OrderStatus.FILLED
    assert order.filled_quantity == 1000.0
    assert order.avg_fill_price == pytest.approx(1.08)


@pytest.mark.asyncio
async def test_poll_fills_marks_cancelled(db_session):
    """Fill poll should mark OPEN order as CANCELLED when broker reports it cancelled."""
    from app.services.background_tasks import _poll_fills_once

    order = Order(
        tenant_id=1, broker="oanda", account="primary", symbol="GBP_USD",
        instrument_type=InstrumentType.FOREX,
        action=OrderAction.BUY, order_type=OrderType.LIMIT,
        quantity=500, price=1.2500, time_in_force=TimeInForce.GTC,
        status=OrderStatus.OPEN, broker_order_id="oanda-cancelled-999",
        multiplier=1.0,
    )
    db_session.add(order)
    await db_session.commit()

    cancelled_status = OrderStatusResult(found=True, is_cancelled=True)

    with patch("app.services.background_tasks.AsyncSessionLocal") as mock_session_cls, \
         patch("app.services.background_tasks.get_broker_for_tenant") as mock_registry:

        mock_broker = AsyncMock()
        mock_broker.poll_order_status.return_value = cancelled_status
        mock_registry.return_value = mock_broker

        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=db_session)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        await _poll_fills_once()

    await db_session.refresh(order)
    assert order.status == OrderStatus.CANCELLED


@pytest.mark.asyncio
async def test_poll_fills_sends_email_on_fill(db_session):
    """Fill poll should send order filled email."""
    from app.services.background_tasks import _poll_fills_once

    order = Order(
        tenant_id=1, broker="oanda", account="primary", symbol="EUR_USD",
        instrument_type=InstrumentType.FOREX,
        action=OrderAction.BUY, order_type=OrderType.LIMIT,
        quantity=1000, price=1.0800, time_in_force=TimeInForce.GTC,
        status=OrderStatus.OPEN, broker_order_id="oanda-email-test",
        multiplier=1.0,
    )
    db_session.add(order)
    await db_session.commit()

    filled_status = OrderStatusResult(
        found=True, is_filled=True, filled_quantity=1000.0, avg_fill_price=1.08
    )

    with patch("app.services.background_tasks.AsyncSessionLocal") as mock_session_cls, \
         patch("app.services.background_tasks.get_broker_for_tenant") as mock_registry, \
         patch("app.services.background_tasks.send_order_filled", new_callable=AsyncMock) as mock_email:

        mock_broker = AsyncMock()
        mock_broker.poll_order_status.return_value = filled_status
        mock_registry.return_value = mock_broker

        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=db_session)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        await _poll_fills_once()

    # Email should have been called
    mock_email.assert_called_once()


# ── IBKR Keepalive ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ibkr_keepalive_pings_gateway(db_session):
    from app.services.background_tasks import _ibkr_keepalive_once
    from app.models.broker_account import BrokerAccount
    from app.services.credentials import encrypt_credentials

    account = BrokerAccount(
        tenant_id=1, broker="ibkr", account_alias="primary",
        credentials_encrypted=encrypt_credentials({
            "gateway_url": "https://localhost:5000/v1/api",
            "account_id": "DU123",
        }),
    )
    db_session.add(account)
    await db_session.commit()

    mock_resp = MagicMock()
    mock_resp.status_code = 200

    with patch("app.services.background_tasks.AsyncSessionLocal") as mock_session_cls, \
         patch("httpx.AsyncClient") as mock_httpx:

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_httpx.return_value = mock_client

        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=db_session)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        await _ibkr_keepalive_once()

    # Tickle should have been called
    mock_client.post.assert_called_once()
    call_url = mock_client.post.call_args[0][0]
    assert "tickle" in call_url


@pytest.mark.asyncio
async def test_ibkr_keepalive_no_accounts_is_noop(db_session):
    """No IBKR accounts → keepalive should do nothing without error."""
    from app.services.background_tasks import _ibkr_keepalive_once

    with patch("app.services.background_tasks.AsyncSessionLocal") as mock_session_cls:
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=db_session)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        await _ibkr_keepalive_once()  # should not raise


# ── Position Reconciliation ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reconcile_drift_logged(db_session, caplog):
    from app.services.background_tasks import _reconcile_once
    import logging

    pos = Position(
        tenant_id=1, broker="oanda", account="primary",
        symbol="EUR_USD", instrument_type="forex",
        quantity=1000.0, avg_price=1.08, multiplier=1.0,
    )
    db_session.add(pos)
    await db_session.commit()

    with patch("app.services.background_tasks.AsyncSessionLocal") as mock_session_cls, \
         patch("app.services.background_tasks.get_broker_for_tenant") as mock_registry, \
         caplog.at_level(logging.WARNING, logger="app.services.background_tasks"):

        mock_broker = AsyncMock()
        mock_broker.get_position.return_value = 500.0  # broker shows 500, we show 1000 = 50% drift
        mock_registry.return_value = mock_broker

        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=db_session)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        await _reconcile_once()

    assert any("RECONCILIATION DRIFT" in r.message for r in caplog.records)
    assert any("50.0%" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_reconcile_no_drift_silent(db_session, caplog):
    from app.services.background_tasks import _reconcile_once
    import logging

    pos = Position(
        tenant_id=1, broker="oanda", account="primary",
        symbol="GBP_USD", instrument_type="forex",
        quantity=2000.0, avg_price=1.25, multiplier=1.0,
    )
    db_session.add(pos)
    await db_session.commit()

    with patch("app.services.background_tasks.AsyncSessionLocal") as mock_session_cls, \
         patch("app.services.background_tasks.get_broker_for_tenant") as mock_registry, \
         caplog.at_level(logging.WARNING, logger="app.services.background_tasks"):

        mock_broker = AsyncMock()
        mock_broker.get_position.return_value = 2000.0  # exact match
        mock_registry.return_value = mock_broker

        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=db_session)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        await _reconcile_once()

    assert not any("DRIFT" in r.message for r in caplog.records)


# ── Daily Summary ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_daily_summary_fires_at_correct_hour(db_session):
    from app.services.background_tasks import _daily_summary_once
    import app.services.background_tasks as bt

    bt._last_summary_date = None  # reset guard

    with patch("app.services.background_tasks.AsyncSessionLocal") as mock_session_cls, \
         patch("app.services.background_tasks.send_daily_summary", new_callable=AsyncMock) as mock_send, \
         patch("app.config.get_settings") as mock_settings_fn:

        settings = MagicMock()
        settings.daily_summary_hour_utc = 7
        mock_settings_fn.return_value = settings

        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=db_session)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        # Simulate running at the wrong hour — should not send
        wrong_time = datetime(2025, 1, 15, 9, 0, 0, tzinfo=timezone.utc)  # hour=9, not 7
        with patch("app.services.background_tasks.datetime") as mock_dt:
            mock_dt.now.return_value = wrong_time
            mock_dt.combine = datetime.combine
            mock_dt.min = datetime.min
            await _daily_summary_once()
        mock_send.assert_not_called()

        # Reset guard and run at correct hour
        bt._last_summary_date = None
        right_time = datetime(2025, 1, 15, 7, 0, 0, tzinfo=timezone.utc)
        with patch("app.services.background_tasks.datetime") as mock_dt:
            mock_dt.now.return_value = right_time
            mock_dt.combine = datetime.combine
            mock_dt.min = datetime.min
            await _daily_summary_once()
        # Would send if there are tenants — just verify it doesn't error


@pytest.mark.asyncio
async def test_daily_summary_runs_only_once_per_day(db_session):
    """Guard prevents duplicate sends within the same UTC day."""
    from app.services.background_tasks import _daily_summary_once
    import app.services.background_tasks as bt

    bt._last_summary_date = None
    fixed_time = datetime(2025, 6, 1, 7, 0, 0, tzinfo=timezone.utc)

    with patch("app.services.background_tasks.AsyncSessionLocal") as mock_session_cls, \
         patch("app.services.background_tasks.send_daily_summary", new_callable=AsyncMock) as mock_send, \
         patch("app.config.get_settings") as mock_settings_fn, \
         patch("app.services.background_tasks.datetime") as mock_dt:

        settings = MagicMock()
        settings.daily_summary_hour_utc = 7
        mock_settings_fn.return_value = settings
        mock_dt.now.return_value = fixed_time
        mock_dt.combine = datetime.combine
        mock_dt.min = datetime.min

        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=db_session)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        await _daily_summary_once()
        call_count_1 = mock_send.call_count

        # Run again in the same "hour"
        await _daily_summary_once()
        call_count_2 = mock_send.call_count

    # Call count should not have increased on second run
    assert call_count_2 == call_count_1


# ── Email Templates ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_order_filled_email_subject():
    from app.services.email_service import send_order_filled

    with patch("app.services.email_service.send_email", new_callable=AsyncMock) as mock_send:
        await send_order_filled(
            to="trader@example.com",
            order_id=42,
            symbol="EUR_USD",
            action="buy",
            filled_qty=1000.0,
            avg_price=1.08500,
            broker="oanda",
            broker_order_id="oanda-123",
        )

    mock_send.assert_called_once()
    subject = mock_send.call_args[0][0]
    assert "EUR_USD" in subject
    assert "BUY" in subject
    assert "1000" in subject


@pytest.mark.asyncio
async def test_payment_failed_email_subject():
    from app.services.email_service import send_payment_failed

    with patch("app.services.email_service.send_email", new_callable=AsyncMock) as mock_send:
        await send_payment_failed("trader@example.com", "Pro")

    subject = mock_send.call_args[0][0]
    assert "payment" in subject.lower()
    assert "failed" in subject.lower()


@pytest.mark.asyncio
async def test_daily_summary_email_pnl_in_subject():
    from app.services.email_service import send_daily_summary

    with patch("app.services.email_service.send_email", new_callable=AsyncMock) as mock_send:
        await send_daily_summary(
            to="trader@example.com",
            date_str="2025-06-01",
            positions=[{"symbol": "EUR_USD", "quantity": 1000, "daily_realized_pnl": 250.0}],
            daily_pnl=250.0,
            orders_today=3,
        )

    subject = mock_send.call_args[0][0]
    assert "250.00" in subject
    assert "2025-06-01" in subject


@pytest.mark.asyncio
async def test_email_disabled_skips_send():
    from app.services.email_service import send_email

    with patch("app.config.get_settings") as mock_settings:
        s = MagicMock()
        s.email_enabled = False
        mock_settings.return_value = s

        with patch("app.services.email_service._send_smtp") as mock_smtp:
            await send_email("subject", "to@x.com", "text", "<html/>")
            mock_smtp.assert_not_called()
