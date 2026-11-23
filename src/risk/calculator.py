"""Pure mathematical position sizing formulas for the Risk Management module.

All functions are stateless and side-effect-free.  Every formula is documented
with its derivation so that hand-calculated test vectors can independently
verify correctness.

Sizing Formula (from docs/RISK_MODEL.md §2.2)
----------------------------------------------
The gross loss when a LONG position hits its stop-loss is:

    Loss(Q) = Q × ΔP_sl
            + Q × P_entry × r_fee          (entry taker fee)
            + Q × P_sl    × r_fee          (stop-loss exit taker fee)
            + Q × P_entry × r_slip         (entry slippage)

Setting Loss(Q) = R_budget and solving for Q:

    Q_adj = R_budget / (ΔP_sl + P_entry × (r_fee + r_slip) + P_sl × r_fee)

The same formula applies symmetrically to SHORT (ΔP_sl = P_sl − P_entry).

Liquidation Guard (from docs/RISK_MODEL.md §4.1)
-------------------------------------------------
Estimated liquidation price for isolated margin:

    LONG:  P_liq = P_entry × (1 − 1/L + MMR)
    SHORT: P_liq = P_entry × (1 + 1/L − MMR)

Where L = max eligible leverage, MMR = maintenance margin ratio.
Reject if:
    LONG:  P_sl ≤ P_liq  (stop is beyond liquidation, position already gone)
    SHORT: P_sl ≥ P_liq
"""

import math


def calculate_risk_budget(equity: float, risk_ratio: float = 0.10) -> float:
  """Returns the nominal risk budget in USDT.

  Args:
      equity:     Current futures account equity (totalMarginBalance).
      risk_ratio: Fraction to risk per trade (default 0.10 = 10%).

  Returns:
      Risk budget in USDT.

  Raises:
      ValueError: If equity or risk_ratio are not positive.
  """
  if equity <= 0:
    raise ValueError(f"Equity must be positive, got {equity}")
  if not (0 < risk_ratio <= 1):
    raise ValueError(f"risk_ratio must be in (0, 1], got {risk_ratio}")
  return equity * risk_ratio


def calculate_stop_distance(
    entry_price: float, sl_price: float, is_long: bool
) -> float:
  """Returns the absolute stop distance in price units.

  Args:
      entry_price: Estimated market entry price.
      sl_price:    Stop-loss price.
      is_long:     True for LONG (sl < entry), False for SHORT (sl > entry).

  Returns:
      Positive stop distance.

  Raises:
      ValueError: If stop distance ≤ 0 (stop on wrong side of entry).
  """
  if is_long:
    dist = entry_price - sl_price
  else:
    dist = sl_price - entry_price

  if dist <= 0:
    raise ValueError(
        f"Stop distance must be positive. "
        f"entry={entry_price}, sl={sl_price}, is_long={is_long} → dist={dist}"
    )
  return dist


def calculate_tp_price(
    entry_price: float,
    sl_price: float,
    is_long: bool,
    rr_ratio: float = 3.0,
) -> float:
  """Calculates take-profit price at the specified reward-to-risk ratio.

  Formula:
      LONG:  TP = entry + rr_ratio × (entry − sl)
      SHORT: TP = entry − rr_ratio × (sl − entry)

  Args:
      entry_price: Estimated market entry price.
      sl_price:    Stop-loss price.
      is_long:     Trade direction.
      rr_ratio:    Reward-to-risk multiplier (default 3.0).

  Returns:
      Take-profit price.
  """
  stop_dist = calculate_stop_distance(entry_price, sl_price, is_long)
  if is_long:
    return entry_price + rr_ratio * stop_dist
  else:
    return entry_price - rr_ratio * stop_dist


def calculate_friction_adjusted_quantity(
    risk_budget: float,
    stop_distance: float,
    entry_price: float,
    sl_price: float,
    taker_fee_rate: float = 0.0005,
    slippage_rate: float = 0.0005,
) -> float:
  """Returns the cost-aware position quantity before step-size truncation.

  Derivation (RISK_MODEL.md §2.2):
      Denominator = ΔP_sl + P_entry × (r_fee + r_slip) + P_sl × r_fee

  The slippage term covers the entry leg only (worst-case market order
  impact).  The exit at SL is a STOP_MARKET order; a separate taker fee
  is charged but no additional slippage term is added to avoid
  double-counting.

  Args:
      risk_budget:    Nominal risk budget in USDT.
      stop_distance:  |entry − sl| in price units (must be > 0).
      entry_price:    Estimated entry price.
      sl_price:       Stop-loss price.
      taker_fee_rate: Exchange taker fee per trade leg (default 0.0005).
      slippage_rate:  One-way entry slippage estimate (default 0.0005).

  Returns:
      Friction-adjusted quantity (positive float, NOT yet truncated).

  Raises:
      ValueError: If inputs are invalid.
  """
  if risk_budget <= 0:
    raise ValueError(f"risk_budget must be positive, got {risk_budget}")
  if stop_distance <= 0:
    raise ValueError(f"stop_distance must be positive, got {stop_distance}")
  if entry_price <= 0 or sl_price <= 0:
    raise ValueError("entry_price and sl_price must be positive")

  denominator = (
      stop_distance
      + entry_price * (taker_fee_rate + slippage_rate)
      + sl_price * taker_fee_rate
  )

  if denominator <= 0:
    raise ValueError(f"Sizing denominator is non-positive: {denominator}")

  return risk_budget / denominator


def calculate_required_margin(
    quantity: float, entry_price: float, leverage: int
) -> float:
  """Returns the isolated initial margin required for the position.

  Formula:
      Margin = (quantity × entry_price) / leverage

  Args:
      quantity:     Final position size in base asset.
      entry_price:  Entry price.
      leverage:     Max eligible leverage for this bracket.

  Returns:
      Required isolated margin in USDT.
  """
  if leverage <= 0:
    raise ValueError(f"leverage must be positive, got {leverage}")
  notional = quantity * entry_price
  return notional / leverage


def estimate_liquidation_price(
    entry_price: float,
    leverage: int,
    is_long: bool,
    maint_margin_ratio: float,
) -> float:
  """Estimates the isolated margin liquidation price.

  Derivation (RISK_MODEL.md §4.1):
      At liquidation, unrealized PnL exhausts the initial margin down to the
      maintenance margin level.

      LONG:  P_liq = P_entry × (1 − 1/L + MMR)
      SHORT: P_liq = P_entry × (1 + 1/L − MMR)

  Note: This is an approximation that ignores position opening fees and
  funding costs.  The guard check provides a conservative buffer.

  Args:
      entry_price:       Entry price.
      leverage:          Applied leverage.
      is_long:           Trade direction.
      maint_margin_ratio: Maintenance margin ratio from bracket.

  Returns:
      Estimated liquidation price.
  """
  if is_long:
    return entry_price * (1.0 - (1.0 / leverage) + maint_margin_ratio)
  else:
    return entry_price * (1.0 + (1.0 / leverage) - maint_margin_ratio)


def calculate_notional(quantity: float, entry_price: float) -> float:
  """Returns position notional value in USDT."""
  return quantity * entry_price


def is_liq_price_safer_than_sl(
    liq_price: float,
    sl_price: float,
    is_long: bool,
) -> bool:
  """Returns True when liquidation is farther from entry than the stop-loss.

  For the trade to be valid:
      LONG:  liq_price < sl_price  (liquidation below stop → stop hit first)
      SHORT: liq_price > sl_price  (liquidation above stop → stop hit first)

  Returns:
      True if the stop-loss is hit before liquidation (trade is safe).
      False if liquidation is tighter than the stop (trade must be rejected).
  """
  if is_long:
    return liq_price < sl_price
  else:
    return liq_price > sl_price


def compute_pnl_at_target(
    quantity: float,
    entry_price: float,
    target_price: float,
    is_long: bool,
    taker_fee_rate: float = 0.0005,
) -> float:
  """Computes estimated net PnL at a specific exit target price.

  Used for final verification of expected reward at TP.

  Args:
      quantity:       Position size.
      entry_price:    Entry price.
      target_price:   Exit price (SL or TP).
      is_long:        Trade direction.
      taker_fee_rate: Taker fee for exit leg.

  Returns:
      Net PnL in USDT (negative = loss).
  """
  if is_long:
    gross_pnl = quantity * (target_price - entry_price)
  else:
    gross_pnl = quantity * (entry_price - target_price)

  entry_fee = quantity * entry_price * taker_fee_rate
  exit_fee = quantity * target_price * taker_fee_rate
  return gross_pnl - entry_fee - exit_fee
