"""
Email notification service.

Uses Python's built-in smtplib — no extra dependency.
All sends are async via asyncio.get_event_loop().run_in_executor
so they don't block the event loop.

Templates are plain-text with an HTML alternative. Simple and deliverable.

Enable/disable via EMAIL_ENABLED=false in .env (default true).
All SMTP settings are in config.py.
"""
import asyncio
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timezone
from functools import partial

from app.config import get_settings

logger = logging.getLogger(__name__)


def _send_smtp(subject: str, to: str, text_body: str, html_body: str) -> None:
    """Synchronous SMTP send — called via executor to avoid blocking."""
    settings = get_settings()
    if not settings.smtp_host:
        logger.warning("Email not sent: SMTP_HOST not configured")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from
    msg["To"] = to
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        if settings.smtp_use_tls:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port)
            server.ehlo()
            server.starttls()
        else:
            server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port)

        if settings.smtp_username:
            server.login(settings.smtp_username, settings.smtp_password)

        server.sendmail(settings.smtp_from, to, msg.as_string())
        server.quit()
        logger.info(f"Email sent: {subject!r} → {to}")
    except Exception:
        logger.exception(f"Failed to send email to {to}: {subject!r}")


async def send_email(subject: str, to: str, text_body: str, html_body: str) -> None:
    """Non-blocking email send."""
    settings = get_settings()
    if not settings.email_enabled:
        logger.debug(f"Email disabled — would send: {subject!r} → {to}")
        return

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None, partial(_send_smtp, subject, to, text_body, html_body)
    )


# ── Templates ──────────────────────────────────────────────────────────────────

async def send_order_filled(
    to: str,
    order_id: int,
    symbol: str,
    action: str,
    filled_qty: float,
    avg_price: float | None,
    broker: str,
    broker_order_id: str | None,
) -> None:
    price_str = f"{avg_price:.5f}" if avg_price else "N/A"
    subject = f"Order filled: {action.upper()} {filled_qty} {symbol}"
    text = (
        f"Your order has been filled.\n\n"
        f"  Symbol:   {symbol}\n"
        f"  Action:   {action.upper()}\n"
        f"  Quantity: {filled_qty}\n"
        f"  Price:    {price_str}\n"
        f"  Broker:   {broker}\n"
        f"  Order ID: {broker_order_id or 'N/A'}\n"
        f"  Relay ID: {order_id}\n\n"
        f"View your positions at your dashboard."
    )
    html = f"""
<html><body style="font-family:sans-serif;color:#222;max-width:480px;margin:0 auto">
<h2 style="color:#16a34a">Order Filled ✓</h2>
<table style="border-collapse:collapse;width:100%">
  <tr><td style="padding:6px 0;color:#666">Symbol</td><td style="padding:6px 0"><strong>{symbol}</strong></td></tr>
  <tr><td style="padding:6px 0;color:#666">Action</td><td style="padding:6px 0"><strong>{action.upper()}</strong></td></tr>
  <tr><td style="padding:6px 0;color:#666">Quantity</td><td style="padding:6px 0">{filled_qty}</td></tr>
  <tr><td style="padding:6px 0;color:#666">Fill price</td><td style="padding:6px 0">{price_str}</td></tr>
  <tr><td style="padding:6px 0;color:#666">Broker</td><td style="padding:6px 0">{broker}</td></tr>
  <tr><td style="padding:6px 0;color:#666">Broker order ID</td><td style="padding:6px 0">{broker_order_id or "N/A"}</td></tr>
</table>
</body></html>
"""
    await send_email(subject, to, text, html)


async def send_payment_failed(to: str, plan_name: str) -> None:
    subject = "Action required: payment failed for your subscription"
    text = (
        f"Your payment for the {plan_name} plan failed.\n\n"
        f"Please update your payment method to avoid losing access to your current plan.\n\n"
        f"Update payment: go to your billing dashboard and click 'Manage subscription'."
    )
    html = f"""
<html><body style="font-family:sans-serif;color:#222;max-width:480px;margin:0 auto">
<h2 style="color:#dc2626">Payment Failed ⚠️</h2>
<p>Your payment for the <strong>{plan_name}</strong> plan failed.</p>
<p>Please update your payment method to avoid losing access to your current plan features.</p>
<p><a href="#" style="background:#2563eb;color:white;padding:10px 20px;text-decoration:none;border-radius:4px;display:inline-block">
  Update payment method
</a></p>
<p style="color:#666;font-size:12px">Stripe will retry the payment automatically. If it continues to fail, your plan will be downgraded to Free.</p>
</body></html>
"""
    await send_email(subject, to, text, html)


async def send_daily_summary(
    to: str,
    date_str: str,
    positions: list[dict],
    daily_pnl: float,
    orders_today: int,
) -> None:
    pnl_sign = "+" if daily_pnl >= 0 else ""
    pnl_color = "#16a34a" if daily_pnl >= 0 else "#dc2626"
    subject = f"Daily P&L summary: {pnl_sign}{daily_pnl:.2f} — {date_str}"

    rows_text = ""
    rows_html = ""
    for pos in positions:
        qty = pos.get("quantity", 0)
        symbol = pos.get("symbol", "")
        pnl = pos.get("daily_realized_pnl", 0.0)
        sign = "+" if pnl >= 0 else ""
        rows_text += f"  {symbol:<12} qty={qty:>10.2f}  daily P&L={sign}{pnl:.2f}\n"
        color = "#16a34a" if pnl >= 0 else "#dc2626"
        rows_html += (
            f"<tr>"
            f"<td style='padding:6px 8px'>{symbol}</td>"
            f"<td style='padding:6px 8px;text-align:right'>{qty:.2f}</td>"
            f"<td style='padding:6px 8px;text-align:right;color:{color}'>{sign}{pnl:.2f}</td>"
            f"</tr>"
        )

    text = (
        f"Daily Summary — {date_str}\n"
        f"{'─'*40}\n"
        f"Total daily P&L: {pnl_sign}{daily_pnl:.2f}\n"
        f"Orders executed: {orders_today}\n\n"
        f"Open Positions:\n"
        f"{rows_text or '  (none)'}\n"
    )
    html = f"""
<html><body style="font-family:sans-serif;color:#222;max-width:560px;margin:0 auto">
<h2>Daily Summary — {date_str}</h2>
<p style="font-size:24px;margin:8px 0">
  <span style="color:{pnl_color};font-weight:bold">{pnl_sign}{daily_pnl:.2f}</span>
  <span style="color:#666;font-size:14px"> daily realized P&L</span>
</p>
<p style="color:#666">Orders executed today: {orders_today}</p>
{"<h3>Open Positions</h3><table style='border-collapse:collapse;width:100%'><thead><tr style='background:#f3f4f6'><th style='padding:6px 8px;text-align:left'>Symbol</th><th style='padding:6px 8px;text-align:right'>Quantity</th><th style='padding:6px 8px;text-align:right'>Daily P&L</th></tr></thead><tbody>" + rows_html + "</tbody></table>" if positions else "<p style='color:#666'>No open positions.</p>"}
</body></html>
"""
    await send_email(subject, to, text, html)
