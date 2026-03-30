from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from pydantic import BaseModel, field_validator
from datetime import datetime
from typing import Literal

from app.models.db import get_db
from app.models.tenant import Tenant
from app.config import get_settings
from app.brokers.registry import get_broker_for_tenant
from app.models.broker_account import BROKER_CREDENTIAL_FIELDS, BrokerAccount
from app.dependencies.auth import get_current_tenant
from app.services.broker_accounts import (
    create_broker_account,
    list_broker_accounts,
    get_broker_account,
    update_broker_account_credentials,
    delete_broker_account,
    safe_credential_summary,
)
from app.services.credentials import decrypt_credentials

router = APIRouter(prefix="/api/broker-accounts", tags=["broker-accounts"])

BrokerLiteral = Literal["oanda", "ibkr", "tradovate", "etrade"]


# ── Schemas ────────────────────────────────────────────────────────────────────

class CreateBrokerAccountRequest(BaseModel):
    broker: BrokerLiteral
    account_alias: str = "primary"
    display_name: str | None = None
    auto_close_enabled: bool = False
    auto_close_time: str | None = None
    fifo_randomize: bool = False
    fifo_max_offset: int = 3  # "HH:MM" ET, e.g. "16:50" for 4:50 PM
    credentials: dict  # validated against BROKER_CREDENTIAL_FIELDS in the service

    @field_validator("account_alias")
    @classmethod
    def alias_no_spaces(cls, v: str) -> str:
        if " " in v:
            raise ValueError("account_alias must not contain spaces")
        return v.strip()

    class Config:
        json_schema_extra = {
            "example": {
                "broker": "oanda",
                "account_alias": "primary",
                "display_name": "Oanda Live",
                "credentials": {
                    "api_key": "your-oanda-api-key",
                    "account_id": "101-001-1234567-001",
                    "base_url": "https://api-fxtrade.oanda.com/v3",
                },
            }
        }


class UpdateBrokerAccountRequest(BaseModel):
    credentials: dict
    display_name: str | None = None


class BrokerAccountOut(BaseModel):
    id: int
    broker: str
    account_alias: str
    display_name: str | None
    is_active: bool
    auto_close_enabled: bool = False
    auto_close_time: str | None = None
    fifo_randomize: bool = False
    fifo_max_offset: int = 3
    max_total_drawdown: float | None = None
    max_daily_drawdown: float | None = None
    commission_per_contract: float | None = None
    created_at: datetime
    updated_at: datetime
    # Redacted credential summary — never returns raw secrets
    credential_summary: dict

    class Config:
        from_attributes = True


def _to_out(account) -> BrokerAccountOut:
    try:
        creds = decrypt_credentials(account.credentials_encrypted)
        summary = safe_credential_summary(account.broker, creds)
    except Exception:
        summary = {"error": "credentials could not be read"}

    return BrokerAccountOut(
        id=account.id,
        broker=account.broker,
        account_alias=account.account_alias,
        display_name=account.display_name,
        is_active=account.is_active,
        auto_close_enabled=account.auto_close_enabled,
        auto_close_time=account.auto_close_time,
        fifo_randomize=account.fifo_randomize,
        fifo_max_offset=account.fifo_max_offset,
        max_total_drawdown=account.max_total_drawdown,
        max_daily_drawdown=account.max_daily_drawdown,
        commission_per_contract=account.commission_per_contract,
        created_at=account.created_at,
        updated_at=account.updated_at,
        credential_summary=summary,
    )


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.get("", response_model=list[BrokerAccountOut])
async def list_accounts(
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    accounts = await list_broker_accounts(db, tenant.id)
    return [_to_out(a) for a in accounts]


@router.post("", response_model=BrokerAccountOut, status_code=status.HTTP_201_CREATED)
async def create_account(
    body: CreateBrokerAccountRequest,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    from app.services.plan_enforcer import PlanEnforcer, PlanLimitExceeded
    from fastapi import HTTPException
    try:
        enforcer = await PlanEnforcer.load(tenant.id, db)
        await enforcer.check_broker_account_limit(db)
    except PlanLimitExceeded as e:
        raise HTTPException(status_code=429, detail=str(e))

    try:
        account = await create_broker_account(
            db,
            tenant_id=tenant.id,
            broker=body.broker,
            account_alias=body.account_alias,
            credentials=body.credentials,
            display_name=body.display_name,
            auto_close_enabled=body.auto_close_enabled,
            auto_close_time=body.auto_close_time,
            fifo_randomize=body.fifo_randomize,
            fifo_max_offset=body.fifo_max_offset,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    await db.commit()
    await db.refresh(account)
    return _to_out(account)


@router.get("/{account_id}", response_model=BrokerAccountOut)
async def get_account(
    account_id: int,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    account = await get_broker_account(db, account_id, tenant.id)
    if account is None:
        raise HTTPException(status_code=404, detail="Broker account not found")
    return _to_out(account)


@router.patch("/{account_id}", response_model=BrokerAccountOut)
async def update_account(
    account_id: int,
    body: UpdateBrokerAccountRequest,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Update credentials or display name. Existing credentials are fully replaced."""
    try:
        account = await update_broker_account_credentials(
            db, account_id, tenant.id, body.credentials, body.display_name
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    if account is None:
        raise HTTPException(status_code=404, detail="Broker account not found")

    await db.commit()
    await db.refresh(account)
    return _to_out(account)


@router.post("/{account_id}/import-history")
async def import_trade_history(
    account_id: int,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    Fetch fill history from Tradovate and import as orders.
    Skips fills that already exist (by broker_order_id).
    Only works for Tradovate accounts with valid OAuth tokens.
    """
    import httpx
    from app.services.credentials import decrypt_credentials
    from app.models.order import Order, OrderStatus, OrderAction, OrderType, InstrumentType, DEFAULT_FUTURES_MULTIPLIERS

    account = await get_broker_account(db, account_id, tenant.id)
    if account is None:
        raise HTTPException(status_code=404, detail="Broker account not found")
    if account.broker != "tradovate":
        raise HTTPException(status_code=400, detail="Import is only supported for Tradovate accounts")

    creds = decrypt_credentials(account.credentials_encrypted)
    token = creds.get("access_token")
    base_url = creds.get("base_url", "https://live.tradovateapi.com/v1")
    if not token:
        raise HTTPException(status_code=400, detail="No access token — reconnect via OAuth")

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Fetch fills
            fill_resp = await client.get(
                f"{base_url}/fill/list",
                headers={"Authorization": f"Bearer {token}"},
            )
            if fill_resp.status_code == 401:
                raise HTTPException(
                    status_code=401,
                    detail="Tradovate token expired — reconnect this account via OAuth"
                )
            fill_resp.raise_for_status()
            fills = fill_resp.json()

            # Fetch orders to get action/symbol/contract info
            order_resp = await client.get(
                f"{base_url}/order/list",
                headers={"Authorization": f"Bearer {token}"},
            )
            order_resp.raise_for_status()
            orders_by_id = {o["id"]: o for o in order_resp.json()}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Tradovate API error: {str(e)}")

    # Get existing broker_order_ids to skip duplicates
    existing = await db.execute(
        select(Order.broker_order_id).where(
            Order.tenant_id == tenant.id,
            Order.broker == "tradovate",
            Order.account == account.account_alias,
            Order.broker_order_id.isnot(None),
        )
    )
    existing_ids = {r[0] for r in existing.fetchall()}

    imported = 0
    skipped = 0
    for fill in fills:
        order_id = fill.get("orderId")
        order_data = orders_by_id.get(order_id, {})

        # Only import fills for this account
        acct_name = order_data.get("accountName") or order_data.get("accountSpec") or ""
        if acct_name != account.account_alias:
            continue

        fill_id = str(fill.get("id", ""))
        if fill_id in existing_ids:
            skipped += 1
            continue

        action_str = order_data.get("action", "").lower()  # "Buy" or "Sell"
        if action_str not in ("buy", "sell"):
            continue

        contract = order_data.get("contractName") or order_data.get("symbol") or ""
        # Extract product root (strip month/year suffix)
        root = ''.join(c for c in contract if c.isalpha())
        multiplier = DEFAULT_FUTURES_MULTIPLIERS.get(root, 1.0)

        price = float(fill.get("price", 0))
        qty = float(fill.get("qty", 0))
        if qty == 0 or price == 0:
            continue

        # Parse timestamp
        ts_str = fill.get("timestamp", "")
        try:
            from datetime import datetime as dt, timezone as tz
            ts = dt.fromisoformat(ts_str.replace("Z", "+00:00"))
        except:
            continue

        order_type_str = order_data.get("orderType", "Market").lower()
        ot = "market" if "market" in order_type_str else "limit" if "limit" in order_type_str else "stop"

        order = Order(
            created_at=ts,
            updated_at=ts,
            tenant_id=tenant.id,
            broker="tradovate",
            account=account.account_alias,
            symbol=contract,
            instrument_type="FUTURE",
            action=action_str.upper(),
            order_type=ot.upper(),
            quantity=qty,
            price=float(order_data.get("price")) if order_data.get("price") else None,
            multiplier=multiplier,
            status="FILLED",
            filled_quantity=qty,
            avg_fill_price=price,
            broker_order_id=fill_id,
            time_in_force="GTC",
        )
        db.add(order)
        imported += 1
        existing_ids.add(fill_id)

    await db.commit()
    return {"imported": imported, "skipped": skipped, "total_fills": len(fills)}


@router.post("/{account_id}/import-csv")
async def import_csv_history(
    account_id: int,
    file: UploadFile = File(...),
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    Import trade history from a Tradovate CSV export.
    Expects the standard Tradovate order export format with columns:
    orderId, Account, B/S, Contract, Product, avgPrice, filledQty, Fill Time, Status, Type
    """
    import csv
    import io
    from app.models.order import (
        Order, OrderStatus, OrderAction, OrderType,
        InstrumentType, DEFAULT_FUTURES_MULTIPLIERS,
    )

    account = await get_broker_account(db, account_id, tenant.id)
    if account is None:
        raise HTTPException(status_code=404, detail="Broker account not found")

    content = await file.read()
    text = content.decode("utf-8-sig")  # handle BOM from Windows exports
    reader = csv.DictReader(io.StringIO(text))

    # Get existing broker_order_ids to skip duplicates
    existing = await db.execute(
        select(Order.broker_order_id).where(
            Order.tenant_id == tenant.id,
            Order.broker == account.broker,
            Order.account == account.account_alias,
            Order.broker_order_id.isnot(None),
        )
    )
    existing_ids = {r[0] for r in existing.fetchall()}

    imported = 0
    skipped = 0
    errors = []

    for row_num, row in enumerate(reader, start=2):
        try:
            status_val = (row.get("Status") or "").strip()
            if status_val != "Filled":
                continue

            # Filter by account name if present in CSV
            csv_account = (row.get("Account") or "").strip()
            if csv_account and csv_account != account.account_alias:
                continue

            order_id = (row.get("orderId") or "").strip()
            if order_id in existing_ids:
                skipped += 1
                continue

            action_str = (row.get("B/S") or "").strip().lower()
            if action_str not in ("buy", "sell"):
                continue

            contract = (row.get("Contract") or "").strip()
            product = (row.get("Product") or "").strip()
            avg_price_str = (row.get("avgPrice") or row.get("Avg Fill Price") or "").strip()
            filled_qty_str = (row.get("filledQty") or row.get("Filled Qty") or "").strip()
            fill_time_str = (row.get("Fill Time") or "").strip()
            order_type_str = (row.get("Type") or "market").strip().lower()

            if not avg_price_str or not filled_qty_str or not fill_time_str:
                continue

            avg_price = float(avg_price_str)
            filled_qty = float(filled_qty_str)
            if avg_price == 0 or filled_qty == 0:
                continue

            # Parse fill time: "MM/DD/YYYY HH:MM:SS"
            from datetime import datetime as dt, timezone as tz
            try:
                fill_time = dt.strptime(fill_time_str, "%m/%d/%Y %H:%M:%S").replace(tzinfo=tz.utc)
            except ValueError:
                try:
                    fill_time = dt.fromisoformat(fill_time_str.replace("Z", "+00:00"))
                except ValueError:
                    continue

            # Root symbol for multiplier lookup
            root = ''.join(c for c in (product or contract) if c.isalpha())
            multiplier = DEFAULT_FUTURES_MULTIPLIERS.get(root, 1.0)

            ot = "market" if "market" in order_type_str else "limit" if "limit" in order_type_str else "stop"

            price = None
            if ot == "limit":
                try: price = float((row.get("decimalLimit") or row.get("Limit Price") or "").strip())
                except (ValueError, AttributeError): pass
            elif ot == "stop":
                try: price = float((row.get("decimalStop") or row.get("Stop Price") or "").strip())
                except (ValueError, AttributeError): pass

            order = Order(
                created_at=fill_time,
                updated_at=fill_time,
                tenant_id=tenant.id,
                broker=account.broker,
                account=account.account_alias,
                symbol=contract,
                instrument_type="FUTURE",
                action=action_str.upper(),
                order_type=ot.upper(),
                quantity=filled_qty,
                price=price,
                multiplier=multiplier,
                status="FILLED",
                filled_quantity=filled_qty,
                avg_fill_price=avg_price,
                broker_order_id=order_id,
                time_in_force="GTC",
            )
            db.add(order)
            imported += 1
            existing_ids.add(order_id)

        except Exception as e:
            errors.append(f"Row {row_num}: {str(e)}")
            if len(errors) > 10:
                break

    await db.commit()
    result = {"imported": imported, "skipped": skipped}
    if errors:
        result["errors"] = errors[:10]
    return result


@router.post("/{account_id}/sync-history")
async def sync_trade_history(
    account_id: int,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    Fetch historical trade data via Tradovate WebSocket sync.
    Falls back to REST API if WebSocket fails.
    """
    from app.services.credentials import decrypt_credentials
    from app.services.tradovate_sync import sync_fills
    from app.models.order import (
        Order, OrderStatus, OrderAction, OrderType,
        InstrumentType, DEFAULT_FUTURES_MULTIPLIERS,
    )

    account = await get_broker_account(db, account_id, tenant.id)
    if account is None:
        raise HTTPException(status_code=404, detail="Broker account not found")
    if account.broker != "tradovate":
        raise HTTPException(status_code=400, detail="Sync is only supported for Tradovate accounts")

    creds = decrypt_credentials(account.credentials_encrypted)
    token = creds.get("access_token")
    base_url = creds.get("base_url", "https://live.tradovateapi.com/v1")
    if not token:
        raise HTTPException(status_code=400, detail="No access token — reconnect via OAuth")

    # Get numeric account ID
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            acct_resp = await client.get(
                f"{base_url}/account/list",
                headers={"Authorization": f"Bearer {token}"},
            )
            if acct_resp.status_code == 401:
                raise HTTPException(status_code=401, detail="Token expired — reconnect via OAuth")
            acct_resp.raise_for_status()
            accounts = acct_resp.json()
            numeric_id = None
            for a in accounts:
                if a["name"] == account.account_alias:
                    numeric_id = a["id"]
                    break
            if numeric_id is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Account {account.account_alias} not found in Tradovate account list"
                )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch account list: {str(e)}")

    # Try WebSocket sync
    fills, orders_by_id = await sync_fills(base_url, token, numeric_id, account.account_alias)

    if not fills and not orders_by_id:
        return {
            "imported": 0,
            "method": "websocket",
            "message": "No historical data returned. Use CSV upload for historical trade import.",
        }

    # Get existing broker_order_ids to skip duplicates
    existing = await db.execute(
        select(Order.broker_order_id).where(
            Order.tenant_id == tenant.id,
            Order.broker == "tradovate",
            Order.account == account.account_alias,
            Order.broker_order_id.isnot(None),
        )
    )
    existing_ids = {r[0] for r in existing.fetchall()}

    imported = 0
    skipped = 0

    # Process fills
    for fill in fills:
        fill_id = str(fill.get("id", fill.get("orderId", "")))
        if fill_id in existing_ids:
            skipped += 1
            continue

        order_id = fill.get("orderId")
        order_data = orders_by_id.get(order_id, {})
        action_str = (order_data.get("action") or fill.get("action", "")).lower()
        if action_str not in ("buy", "sell"):
            continue

        contract = order_data.get("contractName") or fill.get("contractName", "")
        root = ''.join(c for c in contract if c.isalpha())
        multiplier = DEFAULT_FUTURES_MULTIPLIERS.get(root, 1.0)

        price = float(fill.get("price", 0))
        qty = float(fill.get("qty", fill.get("filledQty", 0)))
        if price == 0 or qty == 0:
            continue

        ts_str = fill.get("timestamp", "")
        try:
            from datetime import datetime as dt, timezone as tz
            ts = dt.fromisoformat(ts_str.replace("Z", "+00:00"))
        except:
            continue

        order_type_str = (order_data.get("orderType") or "Market").lower()
        ot = "market" if "market" in order_type_str else "limit" if "limit" in order_type_str else "stop"

        order = Order(
            created_at=ts,
            updated_at=ts,
            tenant_id=tenant.id,
            broker="tradovate",
            account=account.account_alias,
            symbol=contract,
            instrument_type="FUTURE",
            action=action_str.upper(),
            order_type=ot.upper(),
            quantity=qty,
            price=float(order_data.get("price")) if order_data.get("price") else None,
            multiplier=multiplier,
            status="FILLED",
            filled_quantity=qty,
            avg_fill_price=price,
            broker_order_id=fill_id,
            time_in_force="GTC",
        )
        db.add(order)
        imported += 1
        existing_ids.add(fill_id)

    await db.commit()
    return {
        "imported": imported,
        "skipped": skipped,
        "method": "websocket",
        "total_fills": len(fills),
        "total_orders": len(orders_by_id),
    }


class DisplayNameUpdate(BaseModel):
    display_name: str | None = None


@router.patch("/{account_id}/display-name", status_code=200)
async def update_display_name(
    account_id: int,
    body: DisplayNameUpdate,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(BrokerAccount).where(
            BrokerAccount.id == account_id,
            BrokerAccount.tenant_id == tenant.id,
        )
    )
    account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Broker account not found")
    account.display_name = body.display_name
    await db.commit()
    return {"id": account.id, "display_name": account.display_name}


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    account_id: int,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    deleted = await delete_broker_account(db, account_id, tenant.id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Broker account not found")
    await db.commit()


@router.get("/tradovate/oauth-url")
async def tradovate_oauth_url(
    env: str = "live",
    tenant: Tenant = Depends(get_current_tenant),
):
    """Return the Tradovate OAuth authorization URL."""
    import urllib.parse
    from app.services.credentials import encrypt_credentials

    settings = get_settings()
    if not settings.tradovate_oauth_client_id:
        raise HTTPException(status_code=400, detail="Tradovate OAuth not configured")

    # Encrypt env + tenant_id in state param for CSRF protection
    state = encrypt_credentials({"tenant_id": str(tenant.id), "env": env})

    url = (
        f"https://trader.tradovate.com/oauth"
        f"?response_type=code"
        f"&client_id={settings.tradovate_oauth_client_id}"
        f"&redirect_uri={urllib.parse.quote(settings.tradovate_oauth_redirect_uri)}"
        f"&state={urllib.parse.quote(state)}"
    )
    return {"url": url}


class TradovateFetchRequest(BaseModel):
    credentials: dict


class TradovateAccountInfo(BaseModel):
    name: str
    id: int
    nickname: str | None = None


@router.post("/tradovate/fetch-accounts", response_model=list[TradovateAccountInfo])
async def fetch_tradovate_accounts(
    body: TradovateFetchRequest,
    tenant: Tenant = Depends(get_current_tenant),
):
    """Authenticate with Tradovate and return all accounts under the login."""
    import httpx
    creds = body.credentials
    base_url = creds.get("base_url", "https://demo.tradovateapi.com/v1").rstrip("/")

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            auth_body = {
                "name":       creds.get("username", ""),
                "password":   creds.get("password", ""),
                "appId":      creds.get("app_id", ""),
                "appVersion": creds.get("app_version", "1.0"),
                "deviceId":   creds.get("device_id", ""),
                "cid":        str(creds.get("cid", "0")),
                "sec":        creds.get("sec", ""),
            }

            resp = await client.post(
                f"{base_url}/auth/accesstokenrequest",
                json=auth_body,
            )
            resp.raise_for_status()
            data = resp.json()
            if "errorText" in data:
                raise HTTPException(status_code=401, detail=data["errorText"])
            if "p-ticket" in data:
                # p-ticket is a rate-limit penalty from Tradovate (too many attempts,
                # bad device binding, etc). Wait for the penalty to expire and retry.
                p_time = data.get("p-time", 0)
                raise HTTPException(
                    status_code=429,
                    detail=(
                        f"Tradovate returned a rate-limit penalty (p-ticket). "
                        f"Wait {p_time} seconds before retrying. "
                        f"If this keeps happening, try without device_id/cid/sec in advanced settings."
                    ),
                )
            if "accessToken" not in data:
                raise HTTPException(
                    status_code=502,
                    detail=f"Unexpected Tradovate response: {list(data.keys())}",
                )

            token = data["accessToken"]
            acct_resp = await client.get(
                f"{base_url}/account/list",
                headers={"Authorization": f"Bearer {token}"},
            )
            acct_resp.raise_for_status()
            accounts = acct_resp.json()
            return [
                TradovateAccountInfo(
                    name=a["name"],
                    id=a["id"],
                    nickname=a.get("nickname"),
                )
                for a in accounts
            ]
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"Tradovate API error: {e.response.text}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to connect to Tradovate: {str(e)}")


class TradovateBulkCreateRequest(BaseModel):
    credentials: dict
    accounts: list[dict]  # [{"name": "APEX2912...", "alias": "AP-176", "display_name": "...", "prop_firm": bool}]


@router.post("/tradovate/bulk-create", response_model=list[BrokerAccountOut])
async def bulk_create_tradovate_accounts(
    body: TradovateBulkCreateRequest,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Create multiple Tradovate broker accounts sharing one set of credentials."""
    from app.services.plan_enforcer import PlanEnforcer, PlanLimitExceeded

    # If credentials contain _encrypted (OAuth flow), decrypt first
    creds = body.credentials
    if "_encrypted" in creds:
        from app.services.credentials import decrypt_credentials
        creds = decrypt_credentials(creds["_encrypted"])

    try:
        enforcer = await PlanEnforcer.load(tenant.id, db)
    except Exception:
        enforcer = None

    created = []
    for acct in body.accounts:
        account_name = acct["name"]
        alias = acct.get("alias") or account_name
        display_name = acct.get("display_name") or alias
        is_prop = acct.get("prop_firm", False)

        # Check plan limits per account
        if enforcer:
            try:
                await enforcer.check_broker_account_limit(db)
            except PlanLimitExceeded as e:
                raise HTTPException(status_code=429, detail=str(e))

        try:
            account = await create_broker_account(
                db,
                tenant_id=tenant.id,
                broker="tradovate",
                account_alias=account_name,
                credentials=creds,
                display_name=display_name,
                auto_close_enabled=is_prop,
                auto_close_time="16:50" if is_prop else None,
            )
            created.append(account)
        except ValueError:
            # Duplicate — skip silently
            pass

    await db.commit()
    for a in created:
        await db.refresh(a)
    return [_to_out(a) for a in created]


@router.get("/fields/{broker}")
async def get_required_fields(
    broker: BrokerLiteral,
    tenant: Tenant = Depends(get_current_tenant),
):
    """Return the required credential field names for a given broker."""
    return {
        "broker": broker,
        "required_fields": BROKER_CREDENTIAL_FIELDS.get(broker, []),
    }


# ── Instrument Map ─────────────────────────────────────────────────────────────

class InstrumentMapEntry(BaseModel):
    """
    Configuration for a single tradeable instrument on this broker account.

    For IBKR (required):
        conid:      IBKR contract ID (find via TWS or IBKR search API)
        sec_type:   "STK" (equity), "FUT" (futures), "CASH" (forex)
        exchange:   primary exchange, e.g. "NASDAQ", "CME", "IDEALPRO"
        multiplier: point value per contract (futures only), e.g. 50.0 for ES

    For Tradovate (optional — only needed to override multiplier):
        multiplier: override the default multiplier for P&L tracking

    For Oanda/E*Trade: instrument_map is not used.
    """
    conid: int | None = None
    sec_type: str | None = None
    exchange: str | None = None
    multiplier: float | None = None
    commission: float | None = None  # per-contract per-side, overrides account default

    class Config:
        json_schema_extra = {
            "example": {
                "conid": 495512551,
                "sec_type": "FUT",
                "exchange": "CME",
                "multiplier": 50.0,
            }
        }


@router.get("/{account_id}/instruments")
async def get_instrument_map(
    account_id: int,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Return the instrument map for a broker account."""
    account = await get_broker_account(db, account_id, tenant.id)
    if account is None:
        raise HTTPException(status_code=404, detail="Broker account not found")
    return account.instrument_map or {}


@router.put("/{account_id}/instruments/{symbol}")
async def upsert_instrument(
    account_id: int,
    symbol: str,
    entry: InstrumentMapEntry,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    Add or update a single instrument in the map.
    symbol should be the ticker as you'll use it in webhook payloads (e.g. "ES", "AAPL").
    """
    account = await get_broker_account(db, account_id, tenant.id)
    if account is None:
        raise HTTPException(status_code=404, detail="Broker account not found")

    symbol = symbol.upper().strip()
    instrument_map = dict(account.instrument_map or {})
    instrument_map[symbol] = {k: v for k, v in entry.model_dump().items() if v is not None}
    account.instrument_map = instrument_map
    await db.commit()
    return {symbol: instrument_map[symbol]}


@router.delete("/{account_id}/instruments/{symbol}", status_code=204)
async def remove_instrument(
    account_id: int,
    symbol: str,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Remove an instrument from the map."""
    account = await get_broker_account(db, account_id, tenant.id)
    if account is None:
        raise HTTPException(status_code=404, detail="Broker account not found")
    symbol = symbol.upper().strip()
    instrument_map = dict(account.instrument_map or {})
    if symbol not in instrument_map:
        raise HTTPException(status_code=404, detail=f"Instrument {symbol!r} not in map")
    del instrument_map[symbol]
    account.instrument_map = instrument_map
    await db.commit()


class FifoUpdate(BaseModel):
    fifo_randomize: bool


@router.patch("/{account_id}/fifo", status_code=200)
async def update_fifo(
    account_id: int,
    body: FifoUpdate,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Update FIFO avoidance setting for a broker account."""
    result = await db.execute(
        select(BrokerAccount).where(
            BrokerAccount.id        == account_id,
            BrokerAccount.tenant_id == tenant.id,
        )
    )
    account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Broker account not found")
    account.fifo_randomize = body.fifo_randomize
    await db.commit()
    return {
        "id":             account.id,
        "fifo_randomize": account.fifo_randomize,
    }


class AutoCloseUpdate(BaseModel):
    auto_close_enabled: bool
    auto_close_time: str | None = None  # "HH:MM" in ET


@router.patch("/{account_id}/auto-close", status_code=200)
async def update_auto_close(
    account_id: int,
    body: AutoCloseUpdate,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Update auto-close settings for a broker account."""
    result = await db.execute(
        select(BrokerAccount).where(
            BrokerAccount.id        == account_id,
            BrokerAccount.tenant_id == tenant.id,
        )
    )
    account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Broker account not found")

    # Validate time format if provided
    if body.auto_close_time:
        try:
            h, m = body.auto_close_time.split(":")
            assert 0 <= int(h) <= 23 and 0 <= int(m) <= 59
        except Exception:
            raise HTTPException(
                status_code=422,
                detail="auto_close_time must be in HH:MM format (e.g. '16:50')"
            )

    account.auto_close_enabled = body.auto_close_enabled
    account.auto_close_time    = body.auto_close_time if body.auto_close_enabled else None
    await db.commit()

    return {
        "id":                   account.id,
        "auto_close_enabled":   account.auto_close_enabled,
        "auto_close_time":      account.auto_close_time,
    }


class SuspendUpdate(BaseModel):
    is_active: bool


@router.patch("/{account_id}/suspend", status_code=200)
async def toggle_suspend(
    account_id: int,
    body: SuspendUpdate,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Suspend or resume webhook relay for a broker account."""
    result = await db.execute(
        select(BrokerAccount).where(
            BrokerAccount.id == account_id,
            BrokerAccount.tenant_id == tenant.id,
        )
    )
    account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Broker account not found")
    account.is_active = body.is_active
    await db.commit()
    return {"id": account.id, "is_active": account.is_active}


@router.post("/{account_id}/flatten", status_code=200)
async def flatten_positions(
    account_id: int,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Close all open positions on a broker account by submitting CLOSE orders."""
    from app.models.position import Position
    from app.models.order import Order, OrderStatus, OrderAction, OrderType

    account = await get_broker_account(db, account_id, tenant.id)
    if account is None:
        raise HTTPException(status_code=404, detail="Broker account not found")

    # Find open positions
    pos_result = await db.execute(
        select(Position).where(
            Position.tenant_id == tenant.id,
            Position.broker == account.broker,
            Position.account == account.account_alias,
            func.abs(Position.quantity) > 1e-9,
        )
    )
    open_positions = pos_result.scalars().all()

    if not open_positions:
        return {"closed": 0, "message": "No open positions"}

    # Get broker adapter
    broker = await get_broker_for_tenant(
        account.broker, account.account_alias, tenant.id, db
    )

    closed = 0
    errors = []
    for pos in open_positions:
        try:
            # Create a close order
            order = Order(
                tenant_id=tenant.id,
                broker=account.broker,
                account=account.account_alias,
                symbol=pos.symbol,
                instrument_type=pos.instrument_type,
                action=OrderAction.CLOSE,
                order_type=OrderType.MARKET,
                quantity=abs(pos.quantity),
                multiplier=pos.multiplier,
                status=OrderStatus.SUBMITTED,
                time_in_force="FOK",
            )
            db.add(order)
            await db.flush()

            result = await broker.submit_order(order)
            if result.success:
                order.status = OrderStatus.FILLED
                order.broker_order_id = result.broker_order_id
                closed += 1
            else:
                order.status = OrderStatus.REJECTED
                order.error_message = result.error_message
                errors.append(f"{pos.symbol}: {result.error_message}")
        except Exception as e:
            errors.append(f"{pos.symbol}: {str(e)}")

    await db.commit()
    resp = {"closed": closed, "total": len(open_positions)}
    if errors:
        resp["errors"] = errors
    return resp


class DrawdownUpdate(BaseModel):
    max_total_drawdown: float | None = None
    max_daily_drawdown: float | None = None
    commission_per_contract: float | None = None


@router.patch("/{account_id}/drawdown-limits", status_code=200)
async def update_drawdown_limits(
    account_id: int,
    body: DrawdownUpdate,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(BrokerAccount).where(
            BrokerAccount.id == account_id,
            BrokerAccount.tenant_id == tenant.id,
        )
    )
    account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Broker account not found")
    account.max_total_drawdown = body.max_total_drawdown
    account.max_daily_drawdown = body.max_daily_drawdown
    account.commission_per_contract = body.commission_per_contract
    await db.commit()
    return {
        "id": account.id,
        "max_total_drawdown": account.max_total_drawdown,
        "max_daily_drawdown": account.max_daily_drawdown,
    }
