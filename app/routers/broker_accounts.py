from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel, field_validator
from datetime import datetime
from typing import Literal

from app.models.db import get_db
from app.models.tenant import Tenant
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

router = APIRouter(prefix="/broker-accounts", tags=["broker-accounts"])

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
    fifo_max_offset: int = 3


@router.patch("/{account_id}/fifo", status_code=200)
async def update_fifo(
    account_id: int,
    body: FifoUpdate,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Update FIFO randomization settings for a broker account."""
    result = await db.execute(
        select(BrokerAccount).where(
            BrokerAccount.id        == account_id,
            BrokerAccount.tenant_id == tenant.id,
        )
    )
    account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Broker account not found")
    account.fifo_randomize  = body.fifo_randomize
    account.fifo_max_offset = max(1, min(body.fifo_max_offset, 10))  # clamp 1-10
    await db.commit()
    return {
        "id":              account.id,
        "fifo_randomize":  account.fifo_randomize,
        "fifo_max_offset": account.fifo_max_offset,
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
