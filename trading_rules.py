"""Shared direction and bracket-price rules for live and simulated trading."""

from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from math import isfinite
from typing import Optional, Sequence, Tuple


def select_directional_signal(
    probabilities: Sequence[float],
    allow_short_score: bool = True,
) -> Tuple[str, float]:
    """Select the stronger side from a three-class model output.

    When allowed, class 0 is used as a short-side/downside score and class 2
    as the long-side/upside score. Set allow_short_score=False to treat class 0
    strictly as a legacy long-stop class and only permit class 2 LONG signals.
    """
    if len(probabilities) < 3:
        raise ValueError("Expected three class probabilities: short, neutral, long")

    short_probability = float(probabilities[0]) if allow_short_score else 0.0
    long_probability = float(probabilities[2])
    if not isfinite(short_probability) or not isfinite(long_probability):
        raise ValueError("Directional probabilities must be finite")

    if short_probability > long_probability:
        return "SHORT", short_probability
    return "LONG", long_probability


def validate_rr_multipliers(stop_multiplier: float, target_multiplier: float) -> None:
    """Reject a configured bracket that does not preserve the strict 1:3 ratio."""
    stop = float(stop_multiplier)
    target = float(target_multiplier)
    if not isfinite(stop) or not isfinite(target) or stop <= 0 or target <= 0:
        raise ValueError("Stop and target ATR multipliers must be finite positive numbers")
    if abs(target - (3.0 * stop)) > max(1e-10, abs(3.0 * stop) * 1e-8):
        raise ValueError(
            f"Strict 1:3 risk-reward requires tp_atr_mult = 3 * sl_atr_mult "
            f"(got stop={stop_multiplier}, target={target_multiplier})"
        )


def market_tick_size(market: dict, precision_mode: Optional[int] = None) -> Optional[float]:
    """Get the exchange price tick from CCXT market metadata when available."""
    info = market.get("info", {}) if isinstance(market, dict) else {}
    for rule in info.get("filters", []) if isinstance(info, dict) else []:
        if rule.get("filterType") == "PRICE_FILTER":
            try:
                tick = float(rule.get("tickSize", 0))
                if tick > 0 and isfinite(tick):
                    return tick
            except (TypeError, ValueError):
                pass

    precision = market.get("precision", {}) if isinstance(market, dict) else {}
    price_precision = precision.get("price") if isinstance(precision, dict) else None
    try:
        value = float(price_precision)
    except (TypeError, ValueError):
        return None
    if not isfinite(value) or value <= 0:
        return None

    # CCXT DECIMAL_PLACES precision is an integer count; TICK_SIZE precision is
    # the actual increment. Fractional values are ticks in either mode.
    if value.is_integer() and (precision_mode == 2 or (precision_mode is None and value > 1)):
        return 10.0 ** (-int(value))
    return value


def calculate_bracket_prices(
    entry_price: float,
    risk_distance: float,
    direction: str,
    tick_size: Optional[float] = None,
) -> Tuple[float, float, float, float]:
    """Return (tick-aligned entry anchor, stop, target, effective stop distance).

    Stop distance rounds outward to a whole number of ticks and the target is
    exactly three times that distance. This keeps the bracket ratio at 1:3 after
    exchange precision is applied. Invalid or non-positive prices are rejected.
    """
    if direction not in ("LONG", "SHORT"):
        raise ValueError(f"Unsupported trade direction: {direction}")
    if not isfinite(float(entry_price)) or float(entry_price) <= 0:
        raise ValueError("Entry price must be a finite positive number")
    if not isfinite(float(risk_distance)) or float(risk_distance) <= 0:
        raise ValueError("Stop distance must be a finite positive number")

    entry = Decimal(str(entry_price))
    risk = Decimal(str(risk_distance))
    tick = Decimal(str(tick_size)) if tick_size is not None and float(tick_size) > 0 else None

    if tick is not None:
        entry = (entry / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick
        risk_ticks = max(1, int((risk / tick).to_integral_value(rounding=ROUND_CEILING)))
        risk = Decimal(risk_ticks) * tick

    reward = risk * Decimal(3)
    if direction == "LONG":
        stop = entry - risk
        target = entry + reward
    else:
        stop = entry + risk
        target = entry - reward

    if stop <= 0 or target <= 0:
        raise ValueError(
            f"A strict 1:3 {direction} bracket is not positive for entry {entry_price} "
            f"and stop distance {risk_distance}"
        )

    return float(entry), float(stop), float(target), float(risk)
