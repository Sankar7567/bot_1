"""Data models for the Risk Management module."""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class BracketInfo:
  """Resolved leverage bracket and maintenance margin for a target notional.

  Fields are populated from the SymbolLeverageBrackets returned by the exchange.
  """

  bracket_number: int
  max_leverage: int
  notional_cap: float
  notional_floor: float
  maint_margin_ratio: float  # e.g. 0.004 = 0.4%


@dataclass(frozen=True)
class SizingInput:
  """All inputs required by the risk engine for a single trade evaluation.

  Attributes:
      symbol:             Exchange symbol (e.g. 'BTCUSDT').
      equity:             Current futures account equity in USDT
                          (totalMarginBalance).
      risk_ratio:         Nominal risk fraction (default 0.10).
      is_long:            True for LONG, False for SHORT.
      entry_price:        Estimated market entry price.
      sl_price:           Confirmed swing stop-loss price.
      rr_ratio:           Reward-to-risk ratio for TP placement (default 3.0).
      taker_fee_rate:     Exchange taker fee rate (default 0.0005 = 0.05%).
      slippage_rate:      Estimated slippage rate on entry (default 0.0005).
      step_size:          LOT_SIZE stepSize filter value.
      min_qty:            LOT_SIZE minQty filter value.
      max_qty:            LOT_SIZE maxQty filter value.
      min_notional:       MIN_NOTIONAL filter value in USDT.
      tick_size:          PRICE_FILTER tickSize for price rounding.
      available_margin:   USDT available for isolated margin allocation.
      bracket:            Resolved leverage bracket for the target notional.
                          If None the validator derives it internally.
  """

  symbol: str
  equity: float
  is_long: bool
  entry_price: float
  sl_price: float
  step_size: float
  min_qty: float
  max_qty: float
  min_notional: float
  tick_size: float
  available_margin: float
  bracket: BracketInfo
  risk_ratio: float = 0.10
  rr_ratio: float = 3.0
  taker_fee_rate: float = 0.0005
  slippage_rate: float = 0.0005


@dataclass(frozen=True)
class SizingResult:
  """Complete output of a risk validation run.

  Attributes:
      is_valid:               True if the trade may proceed.
      rejection_reason:       Human-readable explanation when is_valid=False.
      symbol:                 Symbol evaluated.
      is_long:                Trade direction.
      equity:                 Account equity at time of evaluation.
      risk_budget:            Nominal risk budget (equity × risk_ratio).
      stop_distance:          |entry − sl| in price units.
      raw_quantity:           Unadjusted quantity (risk_budget / stop_distance).
      friction_adjusted_qty:  Cost-aware quantity before step truncation.
      final_quantity:         Step-size truncated quantity ready for order.
      entry_price:            Estimated entry price.
      sl_price:               Stop-loss price.
      tp_price:               Take-profit price (3R target).
      leverage:               Max eligible leverage for this bracket.
      required_margin:        Isolated margin required (notional / leverage).
      estimated_liq_price:    Approximate liquidation price guard.
      maint_margin_ratio:     Maintenance margin ratio from bracket.
  """

  is_valid: bool
  rejection_reason: Optional[str]
  symbol: str
  is_long: bool
  equity: float
  risk_budget: float
  stop_distance: float
  raw_quantity: float
  friction_adjusted_qty: float
  final_quantity: float
  entry_price: float
  sl_price: float
  tp_price: float
  leverage: int
  required_margin: float
  estimated_liq_price: float
  maint_margin_ratio: float
