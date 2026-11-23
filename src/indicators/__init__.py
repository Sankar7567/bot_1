"""Technical indicators package including Heikin-Ashi and Swing Points."""

from src.indicators.exceptions import (
    InconsistentDataError,
    IndicatorError,
    UnfinishedCandleError,
)
from src.indicators.heikin_ashi import (
    EPSILON,
    HeikinAshiCandle,
    HeikinAshiSeries,
    calculate_heikin_ashi_candle,
    calculate_heikin_ashi_series,
)
from src.indicators.swing_points import (
    PivotType,
    StopLossCalculation,
    SwingDetector,
    SwingPivot,
    calculate_swing_stop_loss,
    find_confirmed_pivots,
    get_latest_eligible_pivot,
)

__all__ = [
    "HeikinAshiCandle",
    "HeikinAshiSeries",
    "calculate_heikin_ashi_candle",
    "calculate_heikin_ashi_series",
    "PivotType",
    "SwingPivot",
    "StopLossCalculation",
    "SwingDetector",
    "find_confirmed_pivots",
    "get_latest_eligible_pivot",
    "calculate_swing_stop_loss",
    "IndicatorError",
    "InconsistentDataError",
    "UnfinishedCandleError",
    "EPSILON",
]
