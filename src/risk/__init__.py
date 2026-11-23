"""Risk Management package — position sizing, bracket matching, and order validation."""

from src.risk.models import SizingInput, SizingResult, BracketInfo
from src.risk.calculator import (
    calculate_risk_budget,
    calculate_stop_distance,
    calculate_tp_price,
    calculate_friction_adjusted_quantity,
    calculate_required_margin,
    estimate_liquidation_price,
)
from src.risk.brackets import get_bracket_for_notional, get_max_eligible_leverage
from src.risk.validator import (
    truncate_to_step_size,
    round_price_to_tick,
    validate_and_size_position,
)
from src.risk.exceptions import (
    RiskValidationError,
    InsufficientEquityError,
    InvalidStopDistanceError,
    LiquidationRiskError,
)

__all__ = [
    "SizingInput",
    "SizingResult",
    "BracketInfo",
    "calculate_risk_budget",
    "calculate_stop_distance",
    "calculate_tp_price",
    "calculate_friction_adjusted_quantity",
    "calculate_required_margin",
    "estimate_liquidation_price",
    "get_bracket_for_notional",
    "get_max_eligible_leverage",
    "truncate_to_step_size",
    "round_price_to_tick",
    "validate_and_size_position",
    "RiskValidationError",
    "InsufficientEquityError",
    "InvalidStopDistanceError",
    "LiquidationRiskError",
]
