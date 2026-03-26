"""
SL/TP offset conversion.

Converts stop_loss, take_profit, and trailing_distance from
offset units (ticks, pips, points) to absolute price levels.

Instrument type determines the unit:
  future  → ticks  (e.g. NQ: 1 tick = 0.25 pts)
  forex   → pips   (e.g. EUR_USD: 1 pip = 0.0001)
  equity  → points (raw price offset, e.g. 1.50)
  cfd     → points
  option  → points

The converter distinguishes offsets from absolute prices using a
magnitude heuristic: if the value is much smaller than the entry
price, it's an offset. If it's in the same ballpark as the entry
price, it's already an absolute level.

For futures: offsets are typically < 200 ticks.
  NQ at 21,000 → SL of 20 is clearly ticks; SL of 20,900 is absolute.
For forex: offsets are typically < 500 pips.
  EUR_USD at 1.08 → SL of 50 is clearly pips; SL of 1.075 is absolute.
"""
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Tick sizes for common futures contracts (in points)
FUTURES_TICK_SIZES: dict[str, float] = {
    # Equity index
    "ES":   0.25, "MES":  0.25,
    "NQ":   0.25, "MNQ":  0.25,
    "RTY":  0.10, "M2K":  0.10,
    "YM":   1.00, "MYM":  1.00,
    # Energy
    "CL":   0.01, "MCL":  0.01,
    "NG":   0.001,
    "RB":   0.0001,
    # Metals
    "GC":   0.10, "MGC":  0.10,
    "SI":   0.005,"SIL":  0.005,
    "HG":   0.0005,
    "PL":   0.10,
    # Rates
    "ZB":   1/32, "ZN":  1/64,
    "ZF":   1/128,"ZT":  1/256,
    # FX futures
    "6E":   0.00005, "6B": 0.0001,
    "6J":   0.0000005,"6C": 0.00005,
    # Ag
    "ZC":   0.25, "ZW":  0.25, "ZS": 0.25,
}

# Pip sizes for forex pairs (4 or 5 decimal places)
# JPY pairs use 2 decimal places (pip = 0.01)
_JPY_PAIRS = {"JPY", "HUF"}


def _pip_size(symbol: str) -> float:
    """Return pip size for a forex symbol."""
    sym = symbol.upper().replace("_", "").replace("/", "")
    # Check if it's a JPY pair (quote currency = JPY)
    if len(sym) >= 6 and sym[3:6] in _JPY_PAIRS:
        return 0.01
    if len(sym) >= 6 and sym[0:3] in _JPY_PAIRS:
        return 0.01
    return 0.0001


def _tick_size(symbol: str) -> float:
    """Return tick size for a futures symbol, stripping contract month."""
    sym = symbol.upper().strip()
    # Try full symbol first (e.g. "ESM5")
    if sym in FUTURES_TICK_SIZES:
        return FUTURES_TICK_SIZES[sym]
    # Strip contract month/year suffix (letters only = root)
    root = ''.join(c for c in sym if c.isalpha())
    return FUTURES_TICK_SIZES.get(root, 0.25)  # default 0.25 if unknown


def _is_offset(value: float, entry_price: float | None, instrument_type: str) -> bool:
    """
    Heuristic to detect whether a value is an offset or an absolute price.

    Rules:
      futures: offset if value < 500 (ticks) AND entry_price > 10× value
      forex:   offset if value < 1000 (pips) AND value < 10.0 (pip values are tiny)
      equity:  offset if value < 100 (points) AND entry_price > 10× value
    """
    if entry_price is None:
        # No entry price — assume offset if small enough
        if instrument_type == "futures":
            return value < 500
        elif instrument_type == "forex":
            return value < 1000
        else:
            return value < 100

    ratio = entry_price / value if value > 0 else float("inf")

    if instrument_type == "future":
        # Offsets are typically 1-200 ticks; absolute prices are 1000s+ for NQ/ES
        return value < 500 and ratio > 5
    elif instrument_type == "forex":
        # Forex pip offsets are whole numbers (e.g. 50 pips).
        # Absolute forex prices are small decimals (e.g. 1.08000).
        # Rule: if value >= 1.0, treat as pip offset; if value < 1.0, treat as absolute price.
        return value >= 1.0
    else:
        # Equity/CFD: offsets usually < 50 points, absolute prices are similar magnitude
        return value < 100 and ratio > 5


@dataclass
class ConvertedLevels:
    stop_loss: float | None
    take_profit: float | None
    trailing_distance: float | None
    stop_loss_was_offset: bool = False
    take_profit_was_offset: bool = False
    trailing_was_offset: bool = False


def convert_sl_tp(
    *,
    action: str,             # "buy" or "sell"
    instrument_type: str,    # "future", "forex", "equity", "cfd"
    symbol: str,
    entry_price: float | None,
    stop_loss: float | None,
    take_profit: float | None,
    trailing_distance: float | None,
) -> ConvertedLevels:
    """
    Convert SL/TP/trailing values from offsets to absolute price levels.

    Returns ConvertedLevels with absolute prices ready to send to the broker.
    """
    is_buy = action.lower() == "buy"

    def to_absolute(value: float, field: str) -> tuple[float, bool]:
        """Convert a single value. Returns (absolute_price, was_offset)."""
        if not _is_offset(value, entry_price, instrument_type):
            return value, False

        if instrument_type == "future":
            tick = _tick_size(symbol)
            offset_pts = value * tick
            base = entry_price if entry_price else 0.0
            if field == "stop_loss":
                result = base - offset_pts if is_buy else base + offset_pts
            elif field == "take_profit":
                result = base + offset_pts if is_buy else base - offset_pts
            else:  # trailing_distance — always positive points
                result = offset_pts
            logger.debug(
                f"{symbol} {field}: {value} ticks × {tick} = {offset_pts:.4f} pts → {result:.4f}"
            )
            return round(result, 4), True

        elif instrument_type == "forex":
            pip = _pip_size(symbol)
            offset = value * pip
            base = entry_price if entry_price else 0.0
            if field == "stop_loss":
                result = base - offset if is_buy else base + offset
            elif field == "take_profit":
                result = base + offset if is_buy else base - offset
            else:
                result = offset
            logger.debug(
                f"{symbol} {field}: {value} pips × {pip} = {offset:.6f} → {result:.6f}"
            )
            return round(result, 6), True

        else:
            # equity / cfd — points = raw price offset
            base = entry_price if entry_price else 0.0
            if field == "stop_loss":
                result = base - value if is_buy else base + value
            elif field == "take_profit":
                result = base + value if is_buy else base - value
            else:
                result = value
            return round(result, 4), True

    sl, sl_was = (to_absolute(stop_loss, "stop_loss")
                  if stop_loss is not None else (None, False))
    tp, tp_was = (to_absolute(take_profit, "take_profit")
                  if take_profit is not None else (None, False))
    tsl, tsl_was = (to_absolute(trailing_distance, "trailing_distance")
                    if trailing_distance is not None else (None, False))

    return ConvertedLevels(
        stop_loss=sl,
        take_profit=tp,
        trailing_distance=tsl,
        stop_loss_was_offset=sl_was,
        take_profit_was_offset=tp_was,
        trailing_was_offset=tsl_was,
    )
