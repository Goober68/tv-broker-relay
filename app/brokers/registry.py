"""
Broker registry — per-request adapter instantiation from DB credentials.
Passes instrument_map from BrokerAccount alongside decrypted credentials.
"""
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.brokers.base import BrokerBase
from app.brokers.oanda import OandaBroker
from app.brokers.ibkr import IBKRBroker
from app.brokers.tradovate import TradovateBroker
from app.brokers.etrade import EtradeBroker
from app.brokers.rithmic import RithmicBroker
from app.models.broker_account import BrokerAccount
from app.services.credentials import decrypt_credentials

import logging
logger = logging.getLogger(__name__)


async def get_broker_for_tenant(
    broker_name: str,
    account_alias: str,
    tenant_id: int,
    db: AsyncSession,
) -> BrokerBase:
    result = await db.execute(
        select(BrokerAccount).where(
            BrokerAccount.tenant_id == tenant_id,
            BrokerAccount.broker == broker_name,
            BrokerAccount.account_alias == account_alias,
            BrokerAccount.is_active == True,  # noqa: E712
        )
    )
    broker_account = result.scalar_one_or_none()
    if broker_account is None:
        raise ValueError(
            f"No active broker account found for broker={broker_name!r}, "
            f"account={account_alias!r}. Add it via POST /broker-accounts."
        )

    try:
        creds = decrypt_credentials(broker_account.credentials_encrypted)
    except Exception as e:
        logger.error(f"Failed to decrypt credentials for BrokerAccount {broker_account.id}: {e}")
        raise ValueError("Broker credentials could not be decrypted") from e

    # Merge instrument_map into creds so from_credentials picks it up
    if broker_account.instrument_map:
        creds["instrument_map"] = broker_account.instrument_map

    match broker_name:
        case "oanda":
            return OandaBroker.from_credentials(creds)
        case "ibkr":
            return IBKRBroker.from_credentials(creds)
        case "tradovate":
            return TradovateBroker.from_credentials(creds)
        case "etrade":
            return EtradeBroker.from_credentials(creds)
        case "rithmic":
            return RithmicBroker.from_credentials(creds)
        case _:
            raise ValueError(f"Unknown broker: {broker_name!r}")


def get_broker(name: str) -> BrokerBase:
    """Legacy single-tenant helper — loads from environment settings."""
    match name:
        case "oanda":     return OandaBroker.from_settings()
        case "ibkr":      return IBKRBroker.from_settings()
        case "tradovate": return TradovateBroker.from_settings()
        case "etrade":    return EtradeBroker.from_settings()
        case "rithmic":   return RithmicBroker.from_settings()
        case _:           raise ValueError(f"Unknown broker: {name!r}")
