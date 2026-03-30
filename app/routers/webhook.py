import json
import logging
import time
import uuid
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.db import get_db
from app.models.tenant import Tenant
from app.models.webhook_delivery import WebhookDelivery
from app.schemas.webhook import WebhookPayload, OrderResponse
from app.services.order_processor import process_webhook
from app.services.api_keys import verify_api_key
from app.services.plan_enforcer import PlanEnforcer, PlanLimitExceeded

logger = logging.getLogger(__name__)
router = APIRouter()

_403 = HTTPException(status_code=403, detail="Invalid or missing API key")


async def _resolve_tenant(
    tenant_id: uuid.UUID,
    payload_secret: str | None,
    db: AsyncSession,
) -> Tenant:
    """
    Authenticate the webhook request via the 'secret' field in the payload.
    Rejects if missing or invalid.
    """
    if not payload_secret:
        raise _403

    result = await db.execute(
        select(Tenant).where(Tenant.id == tenant_id, Tenant.is_active == True)  # noqa: E712
    )
    tenant = result.scalar_one_or_none()
    if tenant is None:
        raise _403

    key = await verify_api_key(db, payload_secret, tenant_id)
    if key is None:
        logger.warning(f"Invalid API key attempt for tenant {tenant_id}")
        raise _403

    return tenant


async def _log_delivery(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    source_ip: str | None,
    user_agent: str | None,
    raw_payload: str | None,
    http_status: int,
    auth_passed: bool,
    outcome: str,
    error_detail: str | None = None,
    order_id: int | None = None,
    duration_ms: float | None = None,
) -> None:
    """Write a WebhookDelivery row. Errors here must never propagate to the caller."""
    try:
        delivery = WebhookDelivery(
            tenant_id=tenant_id,
            source_ip=source_ip,
            user_agent=user_agent,
            raw_payload=raw_payload,
            http_status=http_status,
            auth_passed=auth_passed,
            outcome=outcome,
            error_detail=error_detail,
            order_id=order_id,
            duration_ms=duration_ms,
        )
        db.add(delivery)
        await db.commit()
    except Exception:
        logger.exception("Failed to write webhook delivery log — ignoring")


def _safe_payload_str(payload: WebhookPayload | None, raw_body: bytes) -> str | None:
    """Return the payload as JSON with the secret field stripped."""
    if payload is not None:
        try:
            d = payload.model_dump(exclude={"secret"}, mode="json")
            return json.dumps(d)
        except Exception:
            pass
    # Fall back to raw body with secret naively stripped
    try:
        body = json.loads(raw_body)
        body.pop("secret", None)
        return json.dumps(body)
    except Exception:
        return None


@router.post("/webhook/{tenant_id}", response_model=OrderResponse)
async def receive_webhook(
    tenant_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    t_start = time.monotonic()
    source_ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    raw_body = await request.body()

    # Parse payload (may fail if body is malformed JSON or fails validation)
    payload: WebhookPayload | None = None
    try:
        payload = WebhookPayload.model_validate_json(raw_body)
    except Exception as exc:
        duration_ms = (time.monotonic() - t_start) * 1000
        await _log_delivery(
            db, tenant_id=tenant_id, source_ip=source_ip, user_agent=user_agent,
            raw_payload=_safe_payload_str(None, raw_body),
            http_status=422, auth_passed=False,
            outcome="validation_error", error_detail=str(exc),
            duration_ms=duration_ms,
        )
        raise HTTPException(status_code=422, detail=str(exc))

    # Auth
    try:
        tenant = await _resolve_tenant(
            tenant_id,
            payload_secret=payload.secret if payload else None,
            db=db,
        )
    except HTTPException as exc:
        duration_ms = (time.monotonic() - t_start) * 1000
        await _log_delivery(
            db, tenant_id=tenant_id, source_ip=source_ip, user_agent=user_agent,
            raw_payload=_safe_payload_str(payload, raw_body),
            http_status=exc.status_code, auth_passed=False,
            outcome="auth_failed", error_detail=exc.detail,
            duration_ms=duration_ms,
        )
        raise

    # Plan checks
    try:
        enforcer = await PlanEnforcer.load(tenant_id, db)
        enforcer.check_rate_limit()
        enforcer.check_order_type(payload.order_type.value)
    except PlanLimitExceeded as e:
        duration_ms = (time.monotonic() - t_start) * 1000
        await _log_delivery(
            db, tenant_id=tenant_id, source_ip=source_ip, user_agent=user_agent,
            raw_payload=_safe_payload_str(payload, raw_body),
            http_status=429, auth_passed=True,
            outcome="rate_limited", error_detail=str(e),
            duration_ms=duration_ms,
        )
        raise HTTPException(status_code=429, detail=str(e))

    # Process
    try:
        order = await process_webhook(db, payload, tenant_id=tenant.id, enforcer=enforcer)
    except ValueError as e:
        duration_ms = (time.monotonic() - t_start) * 1000
        await _log_delivery(
            db, tenant_id=tenant_id, source_ip=source_ip, user_agent=user_agent,
            raw_payload=_safe_payload_str(payload, raw_body),
            http_status=422, auth_passed=True,
            outcome="validation_error", error_detail=str(e),
            duration_ms=duration_ms,
        )
        raise HTTPException(status_code=422, detail=str(e))
    except PlanLimitExceeded as e:
        duration_ms = (time.monotonic() - t_start) * 1000
        await _log_delivery(
            db, tenant_id=tenant_id, source_ip=source_ip, user_agent=user_agent,
            raw_payload=_safe_payload_str(payload, raw_body),
            http_status=429, auth_passed=True,
            outcome="rate_limited", error_detail=str(e),
            duration_ms=duration_ms,
        )
        raise HTTPException(status_code=429, detail=str(e))
    except Exception as exc:
        duration_ms = (time.monotonic() - t_start) * 1000
        logger.exception(f"Unexpected error processing webhook for tenant {tenant_id}")
        await _log_delivery(
            db, tenant_id=tenant_id, source_ip=source_ip, user_agent=user_agent,
            raw_payload=_safe_payload_str(payload, raw_body),
            http_status=500, auth_passed=True,
            outcome="error", error_detail=str(exc),
            duration_ms=duration_ms,
        )
        raise HTTPException(status_code=500, detail="Internal error processing order")

    duration_ms = (time.monotonic() - t_start) * 1000
    await _log_delivery(
        db, tenant_id=tenant_id, source_ip=source_ip, user_agent=user_agent,
        raw_payload=_safe_payload_str(payload, raw_body),
        http_status=200, auth_passed=True,
        outcome=order.status.value,
        error_detail=order.error_message if order.status.value in ("rejected", "error") else None,
        order_id=order.id,
        duration_ms=duration_ms,
    )

    return OrderResponse(
        order_id=order.id,
        status=order.status,
        broker_order_id=order.broker_order_id,
        message=order.error_message,
    )
