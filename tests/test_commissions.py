"""
Tests for the broker default commission schedule and lookup priority chain.
"""
import pytest
from app.services.utils import (
    build_commission_lookup,
    get_commission,
    get_broker_default_commission,
    DEFAULT_BROKER_COMMISSIONS,
)


def test_broker_default_commission_tradovate_live():
    """Personal-live Tradovate accounts get NinjaTrader Free rates."""
    comm = get_broker_default_commission("tradovate", "personal-live", "ESM6")
    assert comm == 2.88

    comm_nq = get_broker_default_commission("tradovate", "personal-live", "NQZ5")
    assert comm_nq == 2.88

    comm_mes = get_broker_default_commission("tradovate", "personal-live", "MESH6")
    assert comm_mes == 0.95


def test_broker_default_commission_prop_eval_zero():
    """Prop eval accounts have zero commission."""
    comm = get_broker_default_commission("tradovate", "prop-eval", "ESM6")
    assert comm == 0.0


def test_broker_default_commission_prop_demo_zero():
    comm = get_broker_default_commission("tradovate", "prop-demo", "NQM6")
    assert comm == 0.0


def test_broker_default_commission_prop_live_zero():
    comm = get_broker_default_commission("tradovate", "prop-live", "ESM6")
    assert comm == 0.0


def test_broker_default_commission_unknown_broker():
    """Unknown brokers return None."""
    comm = get_broker_default_commission("unknown_broker", "personal-live", "ESM6")
    assert comm is None


def test_broker_default_commission_none_account_type():
    """None account_type falls back to _default."""
    comm = get_broker_default_commission("tradovate", None, "ESM6")
    assert comm == 0.0  # _default is _ZERO


def test_build_commission_lookup_with_broker_defaults():
    """build_commission_lookup backfills from broker defaults."""
    lookup, default = build_commission_lookup(
        instrument_map=None,
        default_commission=0.0,
        broker="tradovate",
        account_type="personal-live",
    )
    # Should have ES, NQ, etc. from broker defaults
    assert lookup.get("ES") == 2.88
    assert lookup.get("NQ") == 2.88
    assert lookup.get("MES") == 0.95
    # Default should be the broker's _default
    assert default == 2.88


def test_build_commission_instrument_map_overrides_broker_default():
    """Per-account instrument_map takes priority over broker defaults."""
    instrument_map = {"ES": {"commission": 1.50}}
    lookup, default = build_commission_lookup(
        instrument_map=instrument_map,
        default_commission=0.0,
        broker="tradovate",
        account_type="personal-live",
    )
    # ES should be the instrument_map value, not broker default
    assert lookup["ES"] == 1.50
    # NQ should still come from broker defaults
    assert lookup.get("NQ") == 2.88


def test_build_commission_no_broker_backward_compatible():
    """Without broker param, behaves like before (no broker defaults)."""
    lookup, default = build_commission_lookup(
        instrument_map=None,
        default_commission=2.50,
    )
    assert lookup == {}
    assert default == 2.50


def test_get_commission_priority_chain():
    """Full priority: instrument_map > broker default > fallback default."""
    instrument_map = {"CL": {"commission": 1.00}}
    lookup, default = build_commission_lookup(
        instrument_map=instrument_map,
        default_commission=0.0,
        broker="tradovate",
        account_type="personal-live",
    )
    # CL: from instrument_map (1.00), not broker default (3.00)
    assert get_commission("CLZ5", lookup, default) == 1.00
    # ES: from broker default (2.88)
    assert get_commission("ESM6", lookup, default) == 2.88
    # Unknown symbol: from broker _default (2.88)
    assert get_commission("XYZABC", lookup, default) == 2.88


def test_prop_eval_all_zero():
    """Prop eval gets zero for all symbols."""
    lookup, default = build_commission_lookup(
        instrument_map=None,
        default_commission=0.0,
        broker="tradovate",
        account_type="prop-eval",
    )
    assert get_commission("ESM6", lookup, default) == 0.0
    assert get_commission("NQM6", lookup, default) == 0.0
    assert get_commission("CLZ5", lookup, default) == 0.0


def test_broker_defaults_dict_structure():
    """Verify the dict has expected account types for tradovate."""
    tv = DEFAULT_BROKER_COMMISSIONS.get("tradovate", {})
    for acct_type in ("prop-eval", "prop-demo", "prop-live", "personal-live", "personal-demo"):
        assert acct_type in tv, f"Missing account_type {acct_type}"
        assert "_default" in tv[acct_type], f"Missing _default in {acct_type}"
