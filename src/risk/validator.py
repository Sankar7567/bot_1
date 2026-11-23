"""Position sizing validation and exchange-filter compliance.

Applies all Binance exchange constraints and risk guard-rails to a candidate
trade, producing an authoritative SizingResult that is either fully approved
or rejected with a human-readable reason.

Validation sequence (must pass in order):
  1.  Equity sanity check (equity > 0).
  2.  Stop distance positive (entry and sl on correct side).
  3.  Friction-adjusted raw quantity > 0.
  4.  Step-size truncation (always round DOWN).
  5.  minQty filter — reject if below; never increase quantity.
  6.  maxQty filter — reject if above.
  7.  minNotional filter — reject if below; never increase quantity.
  8.  Margin availability — isolated margin ≥ required margin.
  9.  Liquidation guard — stop-loss must be hit before liquidation.
"""

import math
from typing import Optional
from src.monitoring.logger import get_logger
from src.risk.calculator import (
    calculate_risk_budget,
    calculate_stop_distance,
    calculate_tp_price,
    calculate_friction_adjusted_quantity,
    calculate_required_margin,
    estimate_liquidation_price,
    calculate_notional,
    is_liq_price_safer_than_sl,
)
from src.risk.models import BracketInfo, SizingInput, SizingResult

logger = get_logger("risk.validator")


def truncate_to_step_size(quantity: float, step_size: float) -> float:
  """Truncates (rounds DOWN) quantity to the nearest step_size multiple.

  Formula:  floor(quantity / step_size) × step_size

  Uses integer arithmetic to avoid floating-point rounding artefacts.
  Quantities are expressed as multiples of step_size in integer space.

  Args:
      quantity:  Raw position quantity.
      step_size: LOT_SIZE stepSize filter value (e.g. 0.001, 0.01, 1.0).

  Returns:
      Truncated quantity that is a valid multiple of step_size.

  Raises:
      ValueError: If step_size ≤ 0.
  """
  if step_size <= 0:
    raise ValueError(f"step_size must be positive, got {step_size}")

  # Determine decimal precision of step_size
  if step_size >= 1.0:
    # Whole-number steps — simple floor
    return math.floor(quantity / step_size) * step_size

  # Sub-unit steps: use scaled integer arithmetic to avoid float drift
  # e.g. step_size=0.001 → precision=3
  precision = max(0, round(-math.log10(step_size)))
  scale = 10 ** precision
  truncated_int = math.floor(quantity * scale / round(step_size * scale))
  return truncated_int * step_size


def round_price_to_tick(price: float, tick_size: float) -> float:
  """Rounds a price to the nearest valid tick_size multiple (round-half-up).

  Args:
      price:     Raw price.
      tick_size: PRICE_FILTER tickSize value.

  Returns:
      Price rounded to nearest tick.
  """
  if tick_size <= 0:
    raise ValueError(f"tick_size must be positive, got {tick_size}")

  precision = max(0, round(-math.log10(tick_size)))
  scale = 10 ** precision
  return round(price * scale) / scale


def _reject(
    sizing_input: SizingInput,
    reason: str,
    *,
    risk_budget: float = 0.0,
    stop_distance: float = 0.0,
    raw_quantity: float = 0.0,
    friction_qty: float = 0.0,
    final_qty: float = 0.0,
    tp_price: float = 0.0,
    leverage: int = 0,
    required_margin: float = 0.0,
    liq_price: float = 0.0,
    mmr: float = 0.0,
) -> SizingResult:
  """Convenience builder for a failed SizingResult."""
  logger.warning(
      "Trade REJECTED for %s: %s", sizing_input.symbol, reason
  )
  return SizingResult(
      is_valid=False,
      rejection_reason=reason,
      symbol=sizing_input.symbol,
      is_long=sizing_input.is_long,
      equity=sizing_input.equity,
      risk_budget=risk_budget,
      stop_distance=stop_distance,
      raw_quantity=raw_quantity,
      friction_adjusted_qty=friction_qty,
      final_quantity=final_qty,
      entry_price=sizing_input.entry_price,
      sl_price=sizing_input.sl_price,
      tp_price=tp_price,
      leverage=leverage,
      required_margin=required_margin,
      estimated_liq_price=liq_price,
      maint_margin_ratio=mmr,
  )


def validate_and_size_position(sizing_input: SizingInput) -> SizingResult:
  """Full end-to-end position sizing validation.

  Applies all guard checks in sequence and returns a SizingResult that is
  either fully approved (is_valid=True) or rejected with a human-readable
  rejection_reason.

  Args:
      sizing_input: Complete trade parameters and exchange filter values.

  Returns:
      SizingResult.
  """
  inp = sizing_input
  br: BracketInfo = inp.bracket

  # ---- 1. Equity sanity -------------------------------------------------------
  if inp.equity <= 0:
    return _reject(inp, f"Equity must be positive (got {inp.equity:.4f}).")

  # ---- 2. Risk budget ---------------------------------------------------------
  try:
    risk_budget = calculate_risk_budget(inp.equity, inp.risk_ratio)
  except ValueError as e:
    return _reject(inp, str(e))

  # ---- 3. Stop distance -------------------------------------------------------
  try:
    stop_distance = calculate_stop_distance(inp.entry_price, inp.sl_price, inp.is_long)
  except ValueError:
    direction = "LONG" if inp.is_long else "SHORT"
    return _reject(
        inp,
        f"Invalid stop placement for {direction}: "
        f"entry={inp.entry_price}, sl={inp.sl_price}.",
        risk_budget=risk_budget,
    )

  # ---- 4. Friction-adjusted raw quantity ---------------------------------------
  try:
    friction_qty = calculate_friction_adjusted_quantity(
        risk_budget=risk_budget,
        stop_distance=stop_distance,
        entry_price=inp.entry_price,
        sl_price=inp.sl_price,
        taker_fee_rate=inp.taker_fee_rate,
        slippage_rate=inp.slippage_rate,
    )
  except ValueError as e:
    return _reject(
        inp, str(e), risk_budget=risk_budget, stop_distance=stop_distance
    )

  raw_quantity = risk_budget / stop_distance  # unadjusted for reference

  # ---- 5. Step-size truncation ------------------------------------------------
  try:
    final_qty = truncate_to_step_size(friction_qty, inp.step_size)
  except ValueError as e:
    return _reject(
        inp,
        str(e),
        risk_budget=risk_budget,
        stop_distance=stop_distance,
        raw_quantity=raw_quantity,
        friction_qty=friction_qty,
    )

  # ---- 6. minQty filter -------------------------------------------------------
  if final_qty < inp.min_qty:
    return _reject(
        inp,
        f"Final quantity {final_qty:.8f} is below minQty {inp.min_qty:.8f}. "
        "Quantity not increased to avoid breaching risk budget.",
        risk_budget=risk_budget,
        stop_distance=stop_distance,
        raw_quantity=raw_quantity,
        friction_qty=friction_qty,
        final_qty=final_qty,
    )

  # ---- 7. maxQty filter -------------------------------------------------------
  if final_qty > inp.max_qty:
    return _reject(
        inp,
        f"Final quantity {final_qty:.8f} exceeds maxQty {inp.max_qty:.8f}.",
        risk_budget=risk_budget,
        stop_distance=stop_distance,
        raw_quantity=raw_quantity,
        friction_qty=friction_qty,
        final_qty=final_qty,
    )

  # ---- 8. TP price ------------------------------------------------------------
  tp_price = calculate_tp_price(
      inp.entry_price, inp.sl_price, inp.is_long, inp.rr_ratio
  )

  # ---- 9. Notional & leverage from bracket ------------------------------------
  notional = calculate_notional(final_qty, inp.entry_price)
  leverage = br.max_leverage
  mmr = br.maint_margin_ratio

  # ---- 10. minNotional filter -------------------------------------------------
  if notional < inp.min_notional:
    return _reject(
        inp,
        f"Position notional {notional:.4f} USDT is below minNotional "
        f"{inp.min_notional:.4f} USDT. "
        "Quantity not increased to avoid breaching risk budget.",
        risk_budget=risk_budget,
        stop_distance=stop_distance,
        raw_quantity=raw_quantity,
        friction_qty=friction_qty,
        final_qty=final_qty,
        tp_price=tp_price,
        leverage=leverage,
        mmr=mmr,
    )

  # ---- 11. Required margin calculation ----------------------------------------
  required_margin = calculate_required_margin(final_qty, inp.entry_price, leverage)

  # ---- 12. Margin availability ------------------------------------------------
  if required_margin > inp.available_margin:
    return _reject(
        inp,
        f"Required isolated margin {required_margin:.4f} USDT exceeds "
        f"available margin {inp.available_margin:.4f} USDT.",
        risk_budget=risk_budget,
        stop_distance=stop_distance,
        raw_quantity=raw_quantity,
        friction_qty=friction_qty,
        final_qty=final_qty,
        tp_price=tp_price,
        leverage=leverage,
        required_margin=required_margin,
        mmr=mmr,
    )

  # ---- 13. Liquidation guard --------------------------------------------------
  liq_price = estimate_liquidation_price(inp.entry_price, leverage, inp.is_long, mmr)

  if not is_liq_price_safer_than_sl(liq_price, inp.sl_price, inp.is_long):
    direction = "LONG" if inp.is_long else "SHORT"
    return _reject(
        inp,
        f"Liquidation price {liq_price:.6f} is tighter than stop-loss "
        f"{inp.sl_price:.6f} for {direction} at leverage {leverage}x. "
        "Position would liquidate before stop-loss fires.",
        risk_budget=risk_budget,
        stop_distance=stop_distance,
        raw_quantity=raw_quantity,
        friction_qty=friction_qty,
        final_qty=final_qty,
        tp_price=tp_price,
        leverage=leverage,
        required_margin=required_margin,
        liq_price=liq_price,
        mmr=mmr,
    )

  # ---- All checks passed — trade approved -------------------------------------
  logger.info(
      "Trade APPROVED for %s: qty=%.8f, entry=%.4f, sl=%.4f, tp=%.4f, "
      "leverage=%dx, margin=%.4f USDT, liq=%.4f.",
      inp.symbol,
      final_qty,
      inp.entry_price,
      inp.sl_price,
      tp_price,
      leverage,
      required_margin,
      liq_price,
  )

  return SizingResult(
      is_valid=True,
      rejection_reason=None,
      symbol=inp.symbol,
      is_long=inp.is_long,
      equity=inp.equity,
      risk_budget=risk_budget,
      stop_distance=stop_distance,
      raw_quantity=raw_quantity,
      friction_adjusted_qty=friction_qty,
      final_quantity=final_qty,
      entry_price=inp.entry_price,
      sl_price=inp.sl_price,
      tp_price=tp_price,
      leverage=leverage,
      required_margin=required_margin,
      estimated_liq_price=liq_price,
      maint_margin_ratio=mmr,
  )
