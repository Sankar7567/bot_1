"""Unit tests for P08 — Risk Management and Position Sizing.

All numerical assertions are independently hand-calculated from first principles
using the formulas documented in docs/RISK_MODEL.md.

Hand-Calculated Reference (§5 of RISK_MODEL.md)
-------------------------------------------------
  equity       = 1000.00 USDT
  risk_ratio   = 0.10   → risk_budget = 100.00 USDT
  entry        = 100.00 USDT
  sl           = 98.00  USDT (LONG)
  stop_dist    = 2.00   USDT
  raw_qty      = 100.00 / 2.00 = 50.0

  Friction denominator:
    = stop_dist + entry × (r_fee + r_slip) + sl × r_fee
    = 2.00 + 100 × (0.0005 + 0.0005) + 98 × 0.0005
    = 2.00 + 0.10 + 0.049
    = 2.149

  friction_qty = 100.00 / 2.149 ≈ 46.5333...
  step_size = 0.001 → final_qty = 46.533

  tp_price = 100 + 3 × 2 = 106.00
  notional = 46.533 × 100 = 4653.3
  leverage = 20 (bracket for notional ≤ 10000)
  mmr = 0.004
  required_margin = 4653.3 / 20 = 232.665
  liq_price (LONG) = 100 × (1 − 1/20 + 0.004) = 100 × 0.954 = 95.4
  95.4 < 98 → stop hit before liquidation → VALID
"""

import math
import pytest
from src.exchange.models import LeverageBracketTier, SymbolLeverageBrackets
from src.risk.brackets import get_bracket_for_notional, get_max_eligible_leverage
from src.risk.calculator import (
    calculate_friction_adjusted_quantity,
    calculate_required_margin,
    calculate_risk_budget,
    calculate_stop_distance,
    calculate_tp_price,
    compute_pnl_at_target,
    estimate_liquidation_price,
    is_liq_price_safer_than_sl,
)
from src.risk.models import BracketInfo, SizingInput
from src.risk.validator import (
    round_price_to_tick,
    truncate_to_step_size,
    validate_and_size_position,
)


# ==============================================================================
# Test Fixtures & Helpers
# ==============================================================================

TAKER_FEE = 0.0005
SLIPPAGE = 0.0005
ENTRY = 100.0
SL_LONG = 98.0
EQUITY = 1000.0
RISK_RATIO = 0.10


def make_bracket(
    max_leverage: int = 20,
    notional_cap: float = 10_000.0,
    notional_floor: float = 0.0,
    mmr: float = 0.004,
    bracket_num: int = 1,
) -> BracketInfo:
  return BracketInfo(
      bracket_number=bracket_num,
      max_leverage=max_leverage,
      notional_cap=notional_cap,
      notional_floor=notional_floor,
      maint_margin_ratio=mmr,
  )


def make_symbol_brackets(tiers: list[tuple]) -> SymbolLeverageBrackets:
  """Build a SymbolLeverageBrackets from a list of (bracket, leverage, cap, floor, mmr) tuples."""
  bracket_list = [
      LeverageBracketTier(
          bracket=t[0],
          initial_leverage=t[1],
          notional_cap=t[2],
          notional_floor=t[3],
          maint_margin_ratio=t[4],
      )
      for t in tiers
  ]
  return SymbolLeverageBrackets(symbol="BTCUSDT", brackets=bracket_list)


def make_sizing_input(
    equity: float = EQUITY,
    entry: float = ENTRY,
    sl: float = SL_LONG,
    is_long: bool = True,
    step_size: float = 0.001,
    min_qty: float = 0.001,
    max_qty: float = 1_000_000.0,
    min_notional: float = 5.0,
    tick_size: float = 0.01,
    available_margin: float = 500.0,
    leverage: int = 20,
    mmr: float = 0.004,
    notional_cap: float = 10_000.0,
) -> SizingInput:
  return SizingInput(
      symbol="BTCUSDT",
      equity=equity,
      is_long=is_long,
      entry_price=entry,
      sl_price=sl,
      step_size=step_size,
      min_qty=min_qty,
      max_qty=max_qty,
      min_notional=min_notional,
      tick_size=tick_size,
      available_margin=available_margin,
      bracket=make_bracket(leverage, notional_cap, mmr=mmr),
  )


# ==============================================================================
# 1. Risk Budget
# ==============================================================================


class TestCalculateRiskBudget:
  def test_standard_10pct_equity(self):
    """R = 1000 × 0.10 = 100.00"""
    assert calculate_risk_budget(1000.0, 0.10) == pytest.approx(100.0)

  def test_5pct_risk(self):
    assert calculate_risk_budget(2000.0, 0.05) == pytest.approx(100.0)

  def test_full_equity(self):
    assert calculate_risk_budget(500.0, 1.0) == pytest.approx(500.0)

  def test_zero_equity_raises(self):
    with pytest.raises(ValueError, match="Equity"):
      calculate_risk_budget(0.0, 0.10)

  def test_negative_equity_raises(self):
    with pytest.raises(ValueError, match="Equity"):
      calculate_risk_budget(-100.0, 0.10)

  def test_zero_risk_ratio_raises(self):
    with pytest.raises(ValueError, match="risk_ratio"):
      calculate_risk_budget(1000.0, 0.0)

  def test_ratio_exceeds_one_raises(self):
    with pytest.raises(ValueError, match="risk_ratio"):
      calculate_risk_budget(1000.0, 1.5)


# ==============================================================================
# 2. Stop Distance
# ==============================================================================


class TestCalculateStopDistance:
  def test_long_stop_distance(self):
    """entry=100, sl=98 → dist=2"""
    assert calculate_stop_distance(100.0, 98.0, is_long=True) == pytest.approx(2.0)

  def test_short_stop_distance(self):
    """entry=100, sl=103 → dist=3"""
    assert calculate_stop_distance(100.0, 103.0, is_long=False) == pytest.approx(3.0)

  def test_long_sl_above_entry_raises(self):
    with pytest.raises(ValueError, match="positive"):
      calculate_stop_distance(100.0, 102.0, is_long=True)

  def test_short_sl_below_entry_raises(self):
    with pytest.raises(ValueError, match="positive"):
      calculate_stop_distance(100.0, 97.0, is_long=False)

  def test_equal_sl_and_entry_raises(self):
    with pytest.raises(ValueError, match="positive"):
      calculate_stop_distance(100.0, 100.0, is_long=True)


# ==============================================================================
# 3. Take-Profit Price
# ==============================================================================


class TestCalculateTpPrice:
  def test_long_3r_tp(self):
    """entry=100, sl=98, dist=2 → tp = 100 + 3×2 = 106"""
    assert calculate_tp_price(100.0, 98.0, is_long=True, rr_ratio=3.0) == pytest.approx(106.0)

  def test_short_3r_tp(self):
    """entry=100, sl=103, dist=3 → tp = 100 − 3×3 = 91"""
    assert calculate_tp_price(100.0, 103.0, is_long=False, rr_ratio=3.0) == pytest.approx(91.0)

  def test_long_2r_tp(self):
    """entry=200, sl=190, dist=10 → tp = 200 + 2×10 = 220"""
    assert calculate_tp_price(200.0, 190.0, is_long=True, rr_ratio=2.0) == pytest.approx(220.0)

  def test_short_1r_tp(self):
    """entry=50, sl=55, dist=5 → tp = 50 − 1×5 = 45"""
    assert calculate_tp_price(50.0, 55.0, is_long=False, rr_ratio=1.0) == pytest.approx(45.0)


# ==============================================================================
# 4. Friction-Adjusted Quantity (Primary Hand-Calculated Vector)
# ==============================================================================


class TestCalculateFrictionAdjustedQuantity:
  def test_primary_reference_vector(self):
    """
    From RISK_MODEL.md §5:
      denominator = 2.00 + 100×0.001 + 98×0.0005
                  = 2.00 + 0.10 + 0.049
                  = 2.149
      qty = 100 / 2.149 ≈ 46.5333...
    """
    result = calculate_friction_adjusted_quantity(
        risk_budget=100.0,
        stop_distance=2.0,
        entry_price=100.0,
        sl_price=98.0,
        taker_fee_rate=0.0005,
        slippage_rate=0.0005,
    )
    assert result == pytest.approx(100.0 / 2.149, rel=1e-6)

  def test_zero_fees_equals_raw_quantity(self):
    """With no fees and no slippage: qty = risk_budget / stop_dist = 50"""
    result = calculate_friction_adjusted_quantity(
        risk_budget=100.0,
        stop_distance=2.0,
        entry_price=100.0,
        sl_price=98.0,
        taker_fee_rate=0.0,
        slippage_rate=0.0,
    )
    assert result == pytest.approx(50.0)

  def test_friction_qty_less_than_raw_qty(self):
    """With any positive fees, friction-adjusted qty must be < risk/stop_dist"""
    result = calculate_friction_adjusted_quantity(
        risk_budget=100.0,
        stop_distance=2.0,
        entry_price=100.0,
        sl_price=98.0,
        taker_fee_rate=0.0005,
        slippage_rate=0.0005,
    )
    assert result < 50.0

  def test_short_trade_vector(self):
    """
    SHORT: entry=100, sl=103 → stop_dist=3
    denominator = 3 + 100×0.001 + 103×0.0005
                = 3 + 0.10 + 0.0515
                = 3.1515
    qty = 100 / 3.1515 ≈ 31.7308...
    """
    result = calculate_friction_adjusted_quantity(
        risk_budget=100.0,
        stop_distance=3.0,
        entry_price=100.0,
        sl_price=103.0,
        taker_fee_rate=0.0005,
        slippage_rate=0.0005,
    )
    expected_denom = 3.0 + 100.0 * 0.001 + 103.0 * 0.0005
    assert result == pytest.approx(100.0 / expected_denom, rel=1e-6)

  def test_zero_risk_budget_raises(self):
    with pytest.raises(ValueError, match="risk_budget"):
      calculate_friction_adjusted_quantity(0.0, 2.0, 100.0, 98.0)

  def test_zero_stop_distance_raises(self):
    with pytest.raises(ValueError, match="stop_distance"):
      calculate_friction_adjusted_quantity(100.0, 0.0, 100.0, 98.0)

  def test_zero_entry_price_raises(self):
    with pytest.raises(ValueError, match="positive"):
      calculate_friction_adjusted_quantity(100.0, 2.0, 0.0, 98.0)


# ==============================================================================
# 5. Required Margin
# ==============================================================================


class TestCalculateRequiredMargin:
  def test_primary_reference_vector(self):
    """
    final_qty=46.533, entry=100, leverage=20
    notional = 4653.3
    margin = 4653.3 / 20 = 232.665
    """
    final_qty = 100.0 / 2.149  # friction_qty (before truncation)
    result = calculate_required_margin(final_qty, entry_price=100.0, leverage=20)
    assert result == pytest.approx(final_qty * 100.0 / 20, rel=1e-6)

  def test_zero_leverage_raises(self):
    with pytest.raises(ValueError, match="leverage"):
      calculate_required_margin(10.0, 100.0, 0)

  def test_high_leverage_reduces_margin(self):
    m20 = calculate_required_margin(10.0, 100.0, 20)
    m100 = calculate_required_margin(10.0, 100.0, 100)
    assert m100 < m20


# ==============================================================================
# 6. Liquidation Price
# ==============================================================================


class TestEstimateLiquidationPrice:
  def test_long_liq_price_formula(self):
    """
    LONG: liq = 100 × (1 − 1/20 + 0.004) = 100 × 0.954 = 95.4
    """
    liq = estimate_liquidation_price(100.0, leverage=20, is_long=True, maint_margin_ratio=0.004)
    assert liq == pytest.approx(95.4, rel=1e-6)

  def test_short_liq_price_formula(self):
    """
    SHORT: liq = 100 × (1 + 1/20 − 0.004) = 100 × 1.046 = 104.6
    """
    liq = estimate_liquidation_price(100.0, leverage=20, is_long=False, maint_margin_ratio=0.004)
    assert liq == pytest.approx(104.6, rel=1e-6)

  def test_liq_moves_closer_to_entry_with_higher_leverage(self):
    liq_20x = estimate_liquidation_price(100.0, 20, True, 0.004)
    liq_50x = estimate_liquidation_price(100.0, 50, True, 0.004)
    # Higher leverage → liquidation closer to entry → liq_50x > liq_20x for LONG
    assert liq_50x > liq_20x

  def test_liq_safer_than_sl_long(self):
    """LONG: liq=95.4 < sl=98 → safe"""
    liq = estimate_liquidation_price(100.0, 20, True, 0.004)
    assert is_liq_price_safer_than_sl(liq, sl_price=98.0, is_long=True)

  def test_liq_not_safer_than_sl_long(self):
    """
    LONG extreme leverage: liq=99.6 > sl=97 → liq fires first → NOT safe.
    is_liq_price_safer_than_sl: liq(99.6) < sl(97)? → False → unsafe
    """
    liq = estimate_liquidation_price(100.0, leverage=125, is_long=True, maint_margin_ratio=0.004)
    # liq = 100 × (1 − 1/125 + 0.004) = 99.6 > sl=97 → position liquidated first
    assert not is_liq_price_safer_than_sl(liq, sl_price=97.0, is_long=True)

  def test_liq_safer_than_sl_short(self):
    """SHORT: liq=104.6 > sl=103 → safe"""
    liq = estimate_liquidation_price(100.0, 20, False, 0.004)
    assert is_liq_price_safer_than_sl(liq, sl_price=103.0, is_long=False)

  def test_liq_not_safer_than_sl_short(self):
    """
    SHORT extreme leverage: liq < sl → liq fires first → NOT safe.
    SHORT entry=100, sl=103, leverage=125, mmr=0.004
    liq = 100 × (1 + 1/125 − 0.004) = 100 × 1.004 = 100.4
    100.4 < 103 → liq (100.4) fires before sl (103) → position liquidated first → NOT safe
    is_liq_price_safer_than_sl: liq(100.4) > sl(103)? → False → unsafe
    """
    liq = estimate_liquidation_price(100.0, leverage=125, is_long=False, maint_margin_ratio=0.004)
    # liq ≈ 100.4; sl=103; for SHORT liq fires when price RISES above liq_price
    # 100.4 < 103 means liq fires before price reaches 103 → NOT safe
    assert not is_liq_price_safer_than_sl(liq, sl_price=103.0, is_long=False)


# ==============================================================================
# 7. Step-Size Truncation
# ==============================================================================


class TestTruncateToStepSize:
  def test_primary_reference_vector(self):
    """friction_qty ≈ 46.5333 with step=0.001 → 46.533"""
    qty = 100.0 / 2.149  # ≈ 46.5333...
    result = truncate_to_step_size(qty, 0.001)
    assert result == pytest.approx(46.533, abs=1e-8)

  def test_step_size_1_0(self):
    assert truncate_to_step_size(46.9, 1.0) == pytest.approx(46.0)

  def test_step_size_0_01(self):
    assert truncate_to_step_size(12.345678, 0.01) == pytest.approx(12.34, abs=1e-8)

  def test_step_size_0_1(self):
    assert truncate_to_step_size(7.99, 0.1) == pytest.approx(7.9, abs=1e-8)

  def test_exact_multiple_unchanged(self):
    assert truncate_to_step_size(5.0, 0.001) == pytest.approx(5.0, abs=1e-9)

  def test_always_rounds_down_not_up(self):
    """3.999 with step=1.0 must give 3.0 not 4.0"""
    assert truncate_to_step_size(3.999, 1.0) == pytest.approx(3.0)

  def test_zero_step_size_raises(self):
    with pytest.raises(ValueError, match="step_size"):
      truncate_to_step_size(10.0, 0.0)


# ==============================================================================
# 8. Price Rounding to Tick
# ==============================================================================


class TestRoundPriceToTick:
  def test_round_to_tick_0_01(self):
    assert round_price_to_tick(100.1234, 0.01) == pytest.approx(100.12)

  def test_round_to_tick_0_1(self):
    assert round_price_to_tick(99.95, 0.1) == pytest.approx(100.0)

  def test_round_to_tick_1_0(self):
    assert round_price_to_tick(105.6, 1.0) == pytest.approx(106.0)

  def test_zero_tick_raises(self):
    with pytest.raises(ValueError, match="tick_size"):
      round_price_to_tick(100.0, 0.0)


# ==============================================================================
# 9. Bracket Matching
# ==============================================================================


class TestBracketMatching:
  STANDARD_TIERS = [
      (1, 125, 10_000, 0, 0.004),
      (2, 100, 50_000, 10_000, 0.005),
      (3, 50, 250_000, 50_000, 0.010),
      (4, 20, 1_000_000, 250_000, 0.025),
      (5, 10, 5_000_000, 1_000_000, 0.050),
  ]

  def test_low_notional_gets_max_leverage(self):
    """Notional 5000 ≤ 10,000 → bracket 1, leverage=125"""
    sym_br = make_symbol_brackets(self.STANDARD_TIERS)
    info = get_bracket_for_notional(sym_br, 5_000.0)
    assert info is not None
    assert info.max_leverage == 125
    assert info.bracket_number == 1
    assert info.maint_margin_ratio == pytest.approx(0.004)

  def test_mid_notional_matches_bracket_2(self):
    """Notional 20,000 → bracket 2, leverage=100"""
    sym_br = make_symbol_brackets(self.STANDARD_TIERS)
    info = get_bracket_for_notional(sym_br, 20_000.0)
    assert info.max_leverage == 100
    assert info.maint_margin_ratio == pytest.approx(0.005)

  def test_exact_cap_boundary(self):
    """Notional exactly at notional_cap=10,000 → bracket 1"""
    sym_br = make_symbol_brackets(self.STANDARD_TIERS)
    info = get_bracket_for_notional(sym_br, 10_000.0)
    assert info.max_leverage == 125

  def test_notional_just_above_cap(self):
    """10,001 → bracket 2"""
    sym_br = make_symbol_brackets(self.STANDARD_TIERS)
    info = get_bracket_for_notional(sym_br, 10_001.0)
    assert info.max_leverage == 100

  def test_notional_exceeds_all_caps_returns_last(self):
    """Notional beyond all tiers returns last (lowest-leverage) tier"""
    sym_br = make_symbol_brackets(self.STANDARD_TIERS)
    info = get_bracket_for_notional(sym_br, 999_999_999.0)
    assert info.max_leverage == 10

  def test_empty_brackets_returns_none(self):
    sym_br = SymbolLeverageBrackets(symbol="BTCUSDT", brackets=[])
    assert get_bracket_for_notional(sym_br, 1000.0) is None

  def test_get_max_eligible_leverage_convenience(self):
    sym_br = make_symbol_brackets(self.STANDARD_TIERS)
    assert get_max_eligible_leverage(sym_br, 5_000.0) == 125
    assert get_max_eligible_leverage(sym_br, 100_000.0) == 50

  def test_empty_brackets_max_leverage_defaults_to_1(self):
    sym_br = SymbolLeverageBrackets(symbol="BTCUSDT", brackets=[])
    assert get_max_eligible_leverage(sym_br, 1000.0) == 1


# ==============================================================================
# 10. PnL Verification
# ==============================================================================


class TestComputePnlAtTarget:
  def test_long_loss_at_sl(self):
    """
    qty=46.533, entry=100, sl=98, fee=0.0005
    gross_pnl = 46.533 × (98−100) = −93.066
    entry_fee = 46.533 × 100 × 0.0005 = 2.32665
    exit_fee  = 46.533 × 98 × 0.0005  = 2.280117
    net = −93.066 − 2.32665 − 2.280117 = −97.672767
    Must be ≥ −100 (risk budget not exceeded)
    """
    qty = 46.533
    pnl = compute_pnl_at_target(qty, 100.0, 98.0, is_long=True, taker_fee_rate=0.0005)
    assert pnl < 0
    assert pnl >= -100.0  # within risk budget

  def test_long_gain_at_tp(self):
    """
    qty=46.533, entry=100, tp=106
    gross = 46.533 × 6 = 279.198
    Net gain must be positive and roughly 3× the SL loss
    """
    qty = 46.533
    pnl = compute_pnl_at_target(qty, 100.0, 106.0, is_long=True, taker_fee_rate=0.0005)
    assert pnl > 0

  def test_short_loss_at_sl(self):
    """SHORT: entry=100, sl=103 → loss"""
    pnl = compute_pnl_at_target(31.0, 100.0, 103.0, is_long=False, taker_fee_rate=0.0005)
    assert pnl < 0

  def test_short_gain_at_tp(self):
    """SHORT: entry=100, tp=91 → gain"""
    pnl = compute_pnl_at_target(31.0, 100.0, 91.0, is_long=False, taker_fee_rate=0.0005)
    assert pnl > 0


# ==============================================================================
# 11. Full End-to-End Validation (validate_and_size_position)
# ==============================================================================


class TestValidateAndSizePosition:
  def test_primary_reference_vector_approved(self):
    """
    Full validation of the hand-calculated reference trade:
      equity=1000, entry=100, sl=98, step=0.001, leverage=20
    Expected: is_valid=True, final_qty=46.533, tp=106, liq=95.4
    """
    inp = make_sizing_input()
    result = validate_and_size_position(inp)

    assert result.is_valid is True
    assert result.rejection_reason is None
    assert result.risk_budget == pytest.approx(100.0)
    assert result.stop_distance == pytest.approx(2.0)
    assert result.raw_quantity == pytest.approx(50.0)
    assert result.friction_adjusted_qty == pytest.approx(100.0 / 2.149, rel=1e-6)
    assert result.final_quantity == pytest.approx(46.533, abs=1e-8)
    assert result.tp_price == pytest.approx(106.0)
    assert result.leverage == 20
    assert result.estimated_liq_price == pytest.approx(95.4, rel=1e-5)
    assert result.maint_margin_ratio == pytest.approx(0.004)

  def test_short_trade_approved(self):
    """SHORT: entry=100, sl=103 → valid with symmetric calculations."""
    inp = make_sizing_input(sl=103.0, is_long=False)
    result = validate_and_size_position(inp)
    assert result.is_valid is True
    assert result.stop_distance == pytest.approx(3.0)
    assert result.tp_price == pytest.approx(91.0)

  def test_zero_equity_rejected(self):
    inp = make_sizing_input(equity=0.0)
    result = validate_and_size_position(inp)
    assert result.is_valid is False
    assert "Equity" in result.rejection_reason

  def test_sl_on_wrong_side_rejected(self):
    """LONG with sl=102 (above entry=100) must be rejected."""
    inp = make_sizing_input(sl=102.0, is_long=True)
    result = validate_and_size_position(inp)
    assert result.is_valid is False
    assert "LONG" in result.rejection_reason or "stop" in result.rejection_reason.lower()

  def test_below_min_qty_rejected(self):
    """Force qty below min_qty by using tiny equity."""
    inp = make_sizing_input(equity=0.01, min_qty=1.0)
    result = validate_and_size_position(inp)
    assert result.is_valid is False
    assert "minQty" in result.rejection_reason

  def test_below_min_notional_rejected(self):
    """Huge min_notional that the tiny position cannot satisfy."""
    inp = make_sizing_input(min_notional=100_000.0)
    result = validate_and_size_position(inp)
    assert result.is_valid is False
    assert "minNotional" in result.rejection_reason

  def test_insufficient_margin_rejected(self):
    """Available margin = 1 USDT — far below required."""
    inp = make_sizing_input(available_margin=1.0)
    result = validate_and_size_position(inp)
    assert result.is_valid is False
    assert "margin" in result.rejection_reason.lower()

  def test_liquidation_tighter_than_sl_rejected(self):
    """
    LONG entry=100, sl=97 (stop below entry), leverage=125, mmr=0.004
    liq = 100 × (1 − 1/125 + 0.004) = 100 × 0.996 = 99.6
    For LONG: price falls → liquidation fires at 99.6 BEFORE stop at 97
    (99.6 > 97 → liq is closer to entry than sl → position liquidated first)
    is_liq_price_safer_than_sl: liq(99.6) < sl(97)? → False → unsafe → reject
    """
    # With entry=100, sl=97 (stop_dist=3), leverage=125, mmr=0.004
    # liq = 100 × (1 − 1/125 + 0.004) = 99.6
    # 99.6 > 97 (sl) → liq fires first (position lost before stop) → REJECT
    inp = make_sizing_input(
        sl=97.0,
        is_long=True,
        leverage=125,
        mmr=0.004,
        notional_cap=10_000.0,
        available_margin=5000.0,
    )
    result = validate_and_size_position(inp)
    assert result.is_valid is False
    assert "liquidat" in result.rejection_reason.lower()

  def test_step_size_truncation_always_rounds_down(self):
    """final_qty must always be <= friction_adjusted_qty."""
    inp = make_sizing_input(step_size=0.01)
    result = validate_and_size_position(inp)
    assert result.is_valid is True
    assert result.final_quantity <= result.friction_adjusted_qty

  def test_final_qty_never_exceeds_risk_budget_at_sl(self):
    """
    Net loss at SL (excluding slippage) must not exceed risk_budget.
    gross_loss = qty × stop_dist ≤ risk_budget
    (fees may push realized loss slightly above nominal target — that is documented)
    """
    inp = make_sizing_input()
    result = validate_and_size_position(inp)
    assert result.is_valid is True
    gross_loss = result.final_quantity * result.stop_distance
    # Gross loss (before fees) must be ≤ risk budget
    assert gross_loss <= result.risk_budget + 1e-6

  def test_above_max_qty_rejected(self):
    """max_qty=1 forces rejection when sizing gives > 1."""
    inp = make_sizing_input(equity=100_000.0, max_qty=1.0)
    result = validate_and_size_position(inp)
    assert result.is_valid is False
    assert "maxQty" in result.rejection_reason

  def test_required_margin_field_correct(self):
    """required_margin = final_qty × entry / leverage"""
    inp = make_sizing_input()
    result = validate_and_size_position(inp)
    assert result.is_valid is True
    expected_margin = result.final_quantity * result.entry_price / result.leverage
    assert result.required_margin == pytest.approx(expected_margin, rel=1e-6)
