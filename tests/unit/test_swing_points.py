"""Unit tests for 5-left / 5-right Swing Detection Engine.

Validates confirmed pivot lows/highs on actual OHLC data, strict inequality rules,
zero look-ahead bias, equal high/low rejection, lookback slicing, and stop-loss calculations.
"""

import pytest
from src.indicators.exceptions import InconsistentDataError
from src.indicators.swing_points import (
    PivotType,
    StopLossCalculation,
    SwingDetector,
    SwingPivot,
    calculate_swing_stop_loss,
    find_confirmed_pivots,
    get_latest_eligible_pivot,
)
from src.market_data.models import Candle


def make_candle(
    index: int,
    low: float,
    high: float,
    open_price: float = 100.0,
    close_price: float = 100.0,
    base_time: int = 1700000000000,
    symbol: str = "BTCUSDT",
    interval: str = "1m",
    is_closed: bool = True,
) -> Candle:
  open_time = base_time + (index * 60000)
  return Candle(
      symbol=symbol,
      interval=interval,
      open_time=open_time,
      close_time=open_time + 59999,
      open=open_price,
      high=high,
      low=low,
      close=close_price,
      volume=10.0,
      quote_volume=1000.0,
      trades_count=50,
      is_closed=is_closed,
  )


class TestSwingDetectionEngine:
  """Test suite for Swing Detection formulas and stop-loss calculations."""

  def test_valid_pivot_low_hand_calculated(self):
    """Test Vector 1: 11 candles with index 5 strictly lower than 5 left and 5 right bars.

    Lows: [105, 104, 103, 102, 101,  95 , 101, 102, 103, 104, 105]
    Index 5 has low = 95.0.
    Confirmed at candle index 10 (5 bars after index 5).
    """
    lows = [105.0, 104.0, 103.0, 102.0, 101.0, 95.0, 101.0, 102.0, 103.0, 104.0, 105.0]
    candles = [make_candle(i, low=l, high=l + 5.0) for i, l in enumerate(lows)]

    pivots = find_confirmed_pivots(candles, left_bars=5, right_bars=5)
    assert len(pivots) == 1
    p = pivots[0]
    assert p.pivot_type == PivotType.SWING_LOW
    assert p.bar_index == 5
    assert p.price == 95.0
    assert p.pivot_timestamp == candles[5].open_time
    assert p.confirmation_timestamp == candles[10].close_time

  def test_valid_pivot_high_hand_calculated(self):
    """Test Vector 2: 11 candles with index 5 strictly higher than 5 left and 5 right bars.

    Highs: [95, 96, 97, 98, 99,  110 , 99, 98, 97, 96, 95]
    Index 5 has high = 110.0.
    """
    highs = [95.0, 96.0, 97.0, 98.0, 99.0, 110.0, 99.0, 98.0, 97.0, 96.0, 95.0]
    candles = [make_candle(i, low=h - 5.0, high=h) for i, h in enumerate(highs)]

    pivots = find_confirmed_pivots(candles, left_bars=5, right_bars=5)
    assert len(pivots) == 1
    p = pivots[0]
    assert p.pivot_type == PivotType.SWING_HIGH
    assert p.bar_index == 5
    assert p.price == 110.0
    assert p.confirmation_timestamp == candles[10].close_time

  def test_insufficient_candles_returns_empty(self):
    """Verify that fewer than 11 candles cannot produce a 5L/5R confirmed pivot."""
    lows = [105.0, 104.0, 103.0, 102.0, 101.0, 95.0, 101.0, 102.0, 103.0, 104.0]  # Only 10 bars
    candles = [make_candle(i, low=l, high=l + 5.0) for i, l in enumerate(lows)]

    assert len(find_confirmed_pivots(candles)) == 0
    assert get_latest_eligible_pivot(candles, PivotType.SWING_LOW) is None

  def test_equal_lows_rejected_strict_inequality(self):
    """Verify strict inequality: equal low on left or right eliminates pivot."""
    # Low at index 5 is 95.0, but index 2 is also 95.0
    lows = [105.0, 104.0, 95.0, 102.0, 101.0, 95.0, 101.0, 102.0, 103.0, 104.0, 105.0]
    candles = [make_candle(i, low=l, high=l + 5.0) for i, l in enumerate(lows)]

    pivots = find_confirmed_pivots(candles)
    assert len(pivots) == 0

    # Low at index 5 is 95.0, but index 8 is also 95.0
    lows_right = [105.0, 104.0, 103.0, 102.0, 101.0, 95.0, 101.0, 102.0, 95.0, 104.0, 105.0]
    candles_right = [make_candle(i, low=l, high=l + 5.0) for i, l in enumerate(lows_right)]
    assert len(find_confirmed_pivots(candles_right)) == 0

  def test_equal_highs_rejected_strict_inequality(self):
    """Verify strict inequality: equal high on left or right eliminates pivot."""
    highs = [95.0, 96.0, 110.0, 98.0, 99.0, 110.0, 99.0, 98.0, 97.0, 96.0, 95.0]
    candles = [make_candle(i, low=h - 5.0, high=h) for i, h in enumerate(highs)]
    assert len(find_confirmed_pivots(candles)) == 0

  def test_no_lookahead_bias(self):
    """Verify pivot at index 5 is NOT confirmed or eligible until index 10 closes.

    At index 9 (4 bars after index 5), pivot must NOT be returned.
    Only when index 10 completes is the pivot confirmed.
    """
    lows = [105.0, 104.0, 103.0, 102.0, 101.0, 95.0, 101.0, 102.0, 103.0, 104.0, 105.0]
    candles_full = [make_candle(i, low=l, high=l + 5.0) for i, l in enumerate(lows)]

    # Slice containing bars 0..9 (only 4 bars to the right of index 5)
    candles_at_t9 = candles_full[:10]
    assert len(candles_at_t9) == 10
    pivot_premature = get_latest_eligible_pivot(candles_at_t9, PivotType.SWING_LOW)
    assert pivot_premature is None

    # Slice containing bars 0..10 (5 bars to the right of index 5)
    pivot_confirmed = get_latest_eligible_pivot(candles_full, PivotType.SWING_LOW)
    assert pivot_confirmed is not None
    assert pivot_confirmed.bar_index == 5
    assert pivot_confirmed.price == 95.0

  def test_get_latest_eligible_pivot_multiple(self):
    """Verify that when multiple pivots exist, get_latest_eligible_pivot returns the latest one."""
    # Create 25 bars with two pivot lows at index 5 (price 90) and index 15 (price 85)
    lows = [100.0] * 25
    # First pivot low at index 5
    for i in range(11):
      lows[i] = 100.0 + abs(i - 5)
    lows[5] = 90.0

    # Second pivot low at index 15
    for i in range(10, 21):
      lows[i] = 100.0 + abs(i - 15)
    lows[15] = 85.0

    candles = [make_candle(i, low=l, high=l + 5.0) for i, l in enumerate(lows)]
    latest_pivot = get_latest_eligible_pivot(candles, PivotType.SWING_LOW)

    assert latest_pivot is not None
    assert latest_pivot.bar_index == 15
    assert latest_pivot.price == 85.0

  def test_long_stop_loss_calculation(self):
    """Verify stop-loss calculation for LONG trade using confirmed swing low."""
    pivot = SwingPivot(
        symbol="BTCUSDT",
        interval="1m",
        pivot_type=PivotType.SWING_LOW,
        bar_index=5,
        pivot_timestamp=1700000000000,
        confirmation_timestamp=1700000300000,
        price=100.0,
    )

    # 1. Valid LONG calculation: Entry = 105.0, Pivot Low = 100.0, Buffer = 0.001 (0.1%)
    # Expected SL = 100.0 * (1 - 0.001) = 99.90
    calc = calculate_swing_stop_loss(
        entry_price=105.0, pivot=pivot, is_long=True, buffer_ratio=0.001
    )
    assert calc.is_valid is True
    assert calc.stop_price == pytest.approx(99.90, rel=1e-5)
    assert calc.stop_distance == pytest.approx(105.0 - 99.90, rel=1e-5)

    # 2. Invalid: LONG with SWING_HIGH pivot
    high_pivot = SwingPivot(
        symbol="BTCUSDT",
        interval="1m",
        pivot_type=PivotType.SWING_HIGH,
        bar_index=5,
        pivot_timestamp=1700000000000,
        confirmation_timestamp=1700000300000,
        price=110.0,
    )
    calc_wrong = calculate_swing_stop_loss(entry_price=105.0, pivot=high_pivot, is_long=True)
    assert calc_wrong.is_valid is False
    assert "SWING_LOW" in calc_wrong.rejection_reason

    # 3. Invalid: Stop price >= entry price (e.g. entry below swing low)
    calc_above = calculate_swing_stop_loss(entry_price=98.0, pivot=pivot, is_long=True)
    assert calc_above.is_valid is False
    assert "strictly below" in calc_above.rejection_reason

  def test_short_stop_loss_calculation(self):
    """Verify stop-loss calculation for SHORT trade using confirmed swing high."""
    pivot = SwingPivot(
        symbol="BTCUSDT",
        interval="1m",
        pivot_type=PivotType.SWING_HIGH,
        bar_index=5,
        pivot_timestamp=1700000000000,
        confirmation_timestamp=1700000300000,
        price=110.0,
    )

    # 1. Valid SHORT calculation: Entry = 105.0, Pivot High = 110.0, Buffer = 0.001 (0.1%)
    # Expected SL = 110.0 * (1 + 0.001) = 110.11
    calc = calculate_swing_stop_loss(
        entry_price=105.0, pivot=pivot, is_long=False, buffer_ratio=0.001
    )
    assert calc.is_valid is True
    assert calc.stop_price == pytest.approx(110.11, rel=1e-5)
    assert calc.stop_distance == pytest.approx(110.11 - 105.0, rel=1e-5)

    # 2. Invalid: SHORT with SWING_LOW pivot
    low_pivot = SwingPivot(
        symbol="BTCUSDT",
        interval="1m",
        pivot_type=PivotType.SWING_LOW,
        bar_index=5,
        pivot_timestamp=1700000000000,
        confirmation_timestamp=1700000300000,
        price=100.0,
    )
    calc_wrong = calculate_swing_stop_loss(entry_price=105.0, pivot=low_pivot, is_long=False)
    assert calc_wrong.is_valid is False
    assert "SWING_HIGH" in calc_wrong.rejection_reason

    # 3. Invalid: Stop price <= entry price (entry above swing high)
    calc_below = calculate_swing_stop_loss(entry_price=115.0, pivot=pivot, is_long=False)
    assert calc_below.is_valid is False
    assert "strictly above" in calc_below.rejection_reason

  def test_swing_detector_stateful_wrapper(self):
    """Verify SwingDetector wrapper methods."""
    detector = SwingDetector("BTCUSDT", interval="1m", left_bars=5, right_bars=5)
    lows = [105.0, 104.0, 103.0, 102.0, 101.0, 95.0, 101.0, 102.0, 103.0, 104.0, 105.0]
    candles = [make_candle(i, low=l, high=l + 5.0) for i, l in enumerate(lows)]

    p_low = detector.get_latest_swing_low(candles)
    assert p_low is not None
    assert p_low.price == 95.0

    p_high = detector.get_latest_swing_high(candles)
    assert p_high is None

  def test_non_chronological_sequence_raises_error(self):
    """Verify find_confirmed_pivots raises InconsistentDataError if candle timestamps are out of order."""
    candles = [
        make_candle(0, low=100.0, high=105.0, base_time=1700000060000),
        make_candle(1, low=100.0, high=105.0, base_time=1700000000000),  # Earlier!
    ]
    with pytest.raises(InconsistentDataError):
      find_confirmed_pivots(candles)
