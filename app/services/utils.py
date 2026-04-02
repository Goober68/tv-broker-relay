"""
Shared utility functions used across multiple modules.
"""
import re
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Futures month codes: F=Jan, G=Feb, H=Mar, J=Apr, K=May, M=Jun,
# N=Jul, Q=Aug, U=Sep, V=Oct, X=Nov, Z=Dec
_FUTURES_CONTRACT_RE = re.compile(r'^(.+?)[FGHJKMNQUVXZ]\d{1,2}$')


def futures_root(contract: str) -> str:
    """Extract product root from futures contract symbol.
    MNQM6 -> MNQ, ESH5 -> ES, NQM6 -> NQ, CLZ25 -> CL.
    Falls back to stripping all digits."""
    m = _FUTURES_CONTRACT_RE.match(contract)
    if m:
        return m.group(1)
    return ''.join(c for c in contract if c.isalpha())


def trading_day(ts: datetime) -> date:
    """Convert a UTC timestamp to its futures trading day.
    Trading day rolls at 5pm ET — a fill at 4:59pm ET belongs to that
    calendar day; a fill at 5:01pm ET belongs to the next day."""
    ts_et = ts.astimezone(ET)
    if ts_et.hour >= 17:
        return (ts_et + timedelta(days=1)).date()
    return ts_et.date()


# ── Per-broker default commission rates (all-in, per contract per side) ────────
# Keyed by broker → account_type → symbol_root → rate.
# "_default" is the fallback at each level.
# Rates from NinjaTrader "Free" tier schedule (Nov 2025) as starting point.
# These are the lowest-priority fallback — per-fill and instrument_map override.

_NT_FREE = {
    # Equity Indexes (CME)
    "EMD": 2.83, "ES": 2.88, "MES": 0.95, "NQ": 2.88, "MNQ": 0.95,
    "RTY": 2.88, "M2K": 0.95,
    # Equity Indexes (CBOT)
    "YM": 2.88, "MYM": 0.95,
    # Crypto (CME)
    "BTC": 8.00, "MBT": 1.60, "MET": 0.80,
    # Agriculturals (CME)
    "GF": 3.60, "HE": 3.60, "LE": 3.60,
    # Agriculturals (CBOT)
    "ZC": 3.60, "XC": 2.53, "ZL": 3.60, "ZM": 3.60, "ZO": 3.60,
    "ZR": 3.60, "ZS": 3.60, "ZW": 3.60,
    # FX (CME)
    "6A": 3.10, "M6A": 0.84, "6B": 3.10, "M6B": 0.84, "6C": 3.10,
    "MICD": 0.84, "6E": 3.10, "E7": 2.35, "M6E": 0.84, "6J": 3.10,
    "J7": 2.35, "6M": 3.10, "6N": 3.10, "6S": 3.10,
    # Interest Rates (CBOT)
    "ZN": 2.30, "10YR": 0.90, "ZB": 2.37, "30YR": 0.90,
    "ZF": 2.15, "5YR": 0.90, "ZT": 2.15, "2YR": 0.90,
    # Metals (COMEX)
    "GC": 3.10, "QO": 2.50, "MGC": 1.20, "HG": 3.10, "MHG": 1.20,
    "QI": 2.50, "SI": 3.10,
    # Energies (NYMEX)
    "CL": 3.00, "QM": 2.70, "MCL": 1.10, "HO": 3.00, "NG": 3.10,
    "QG": 2.00, "RB": 3.00,
    # ICE
    "CC": 3.60, "CT": 3.60, "KC": 3.60, "SB": 3.60, "DX": 2.85,
    # Volatility
    "SPK": 0.41,
    "_default": 2.88,
}

# Zero-commission schedule for demo/eval accounts
_ZERO = {"_default": 0.0}

DEFAULT_BROKER_COMMISSIONS: dict[str, dict[str, dict[str, float]]] = {
    "tradovate": {
        "prop-eval":     _ZERO,
        "prop-demo":     _ZERO,
        "prop-live":     _ZERO,
        "personal-live": _NT_FREE,
        "personal-demo": _ZERO,
        "_default":      _ZERO,
    },
    "rithmic": {
        "prop-eval":     _ZERO,
        "prop-demo":     _ZERO,
        "prop-live":     _ZERO,
        "personal-live": _NT_FREE,
        "personal-demo": _ZERO,
        "_default":      _ZERO,
    },
}


def get_broker_default_commission(
    broker: str, account_type: str | None, symbol: str,
) -> float | None:
    """Look up default commission for a broker/account_type/symbol combo.
    Returns None if no broker default is configured."""
    broker_schedule = DEFAULT_BROKER_COMMISSIONS.get(broker)
    if not broker_schedule:
        return None
    type_schedule = broker_schedule.get(account_type or "", broker_schedule.get("_default"))
    if not type_schedule:
        return None
    root = futures_root(symbol)
    return type_schedule.get(symbol) or type_schedule.get(root) or type_schedule.get("_default")


def build_commission_lookup(
    instrument_map: dict | None,
    default_commission: float = 0.0,
    broker: str | None = None,
    account_type: str | None = None,
) -> tuple[dict, float]:
    """Build commission lookup from instrument_map + broker defaults.
    Returns (lookup_dict, default_commission).
    Priority: instrument_map > broker defaults > default_commission."""
    instrument_map = instrument_map or {}
    lookup = {}

    # 1. Per-account instrument_map overrides (highest priority)
    for key, val in instrument_map.items():
        if isinstance(val, dict) and "commission" in val:
            comm = float(val["commission"])
            lookup[key] = comm
            ts = val.get("target_symbol", "")
            if ts:
                lookup[ts] = comm
                lookup[futures_root(ts)] = comm
            lookup[futures_root(key)] = comm

    # 2. Backfill from broker defaults (only for symbols not already in lookup)
    if broker:
        broker_schedule = DEFAULT_BROKER_COMMISSIONS.get(broker)
        if broker_schedule:
            type_schedule = broker_schedule.get(
                account_type or "", broker_schedule.get("_default", {})
            )
            if type_schedule:
                for sym, rate in type_schedule.items():
                    if sym != "_default" and sym not in lookup:
                        lookup[sym] = rate
                # Use broker default as the fallback if no explicit default_commission
                if default_commission == 0.0 and "_default" in type_schedule:
                    default_commission = type_schedule["_default"]

    return lookup, default_commission


def get_commission(symbol: str, lookup: dict, default: float) -> float:
    """Look up per-product commission, fall back to default."""
    if symbol in lookup:
        return lookup[symbol]
    root = futures_root(symbol)
    if root in lookup:
        return lookup[root]
    return default
