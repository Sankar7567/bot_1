"""Unit tests for pure mathematical Heikin-Ashi calculation engine.

Validates formulas, hand-calculated test vectors, bullish/bearish/doji classification,
series calculation, capacity eviction, and unfinished candle rejection.
"""

import pytest
from src.indicators.exceptions import (
    InconsistentDataError,
    UnfinishedCandleError,
)
from src.indicators.heikin_ashi import (
    EPSILON,
    HeikinAshiCandle,
    HeikinAshiSeries,
    calculate_heikin_ashi_candle,
    calculate_heikin_ashi_series,
)
from src.market_data.models import Candle


def make_test_candle(
    open_price: float,
    high_price: float,
    low_price: float,
    close_price: float,
    open_time: int = 1700000000000,
    interval: str = "1m",
    symbol: str = "BTCUSDT",
    is_closed: bool = True,
) -> Candle:
  return Candle(
      symbol=symbol,
      interval=interval,
      open_time=open_time,
      close_time=open_time + 59999,
      open=open_price,
      high=high_price,
      low=low_price,
      close=close_price,
      volume=10.0,
      quote_volume=1000.0,
      trades_count=10,
      is_closed=is_closed,
  )


class TestHeikinAshiEngine:
  """Test suite for Heikin-Ashi indicator formulas and series manager."""

  def test_first_candle_initialization_hand_calculated(self):
    """Test Vector 1: First candle initialization with hand-calculated expected values.

    Input: Open=100.0, High=110.0, Low=90.0, Close=104.0
    Expected:
      HA Close = (100 + 110 + 90 + 104) / 4 = 101.0
      HA Open  = (100 + 104) / 2 = 102.0
      HA High  = max(110.0, 102.0, 101.0) = 110.0
      HA Low   = min(90.0, 102.0, 101.0) = 90.0
      Direction = Bearish (Close 101.0 < Open 102.0)
    """
    c0 = make_test_candle(100.0, 110.0, 90.0, 104.0, open_time=1700000000000)
    ha0 = calculate_heikin_ashi_candle(c0, prev_ha=None)

    assert ha0.ha_close == 101.0
    assert ha0.ha_open == 102.0
    assert ha0.ha_high == 110.0
    assert ha0.ha_low == 90.0

    assert ha0.is_bearish is True
    assert ha0.is_bullish is False
    assert ha0.is_doji is False
    assert ha0.body_size == 1.0
    assert ha0.upper_wick == 8.0  # 110.0 - 102.0
    assert ha0.lower_wick == 11.0  # 101.0 - 90.0

  def test_subsequent_candle_hand_calculated(self):
    """Test Vector 2: Subsequent candle using prev_ha.

    Input 1: c0 from Test Vector 1 -> ha0 (ha_open=102.0, ha_close=101.0)
    Input 2: Open=105.0, High=115.0, Low=103.0, Close=112.0
    Expected:
      HA Close = (105 + 115 + 103 + 112) / 4 = 435 / 4 = 108.75
      HA Open  = (102.0 + 101.0) / 2 = 101.5
      HA High  = max(115.0, 101.5, 108.75) = 115.0
      HA Low   = min(103.0, 101.5, 108.75) = 101.5 (HA Open lower than actual low!)
      Direction = Bullish (Close 108.75 > Open 101.5)
    """
    c0 = make_test_candle(100.0, 110.0, 90.0, 104.0, open_time=1700000000000)
    ha0 = calculate_heikin_ashi_candle(c0, prev_ha=None)

    c1 = make_test_candle(
        105.0, 115.0, 103.0, 112.0, open_time=1700000060000
    )
    ha1 = calculate_heikin_ashi_candle(c1, prev_ha=ha0)

    assert ha1.ha_close == 108.75
    assert ha1.ha_open == 101.5
    assert ha1.ha_high == 115.0
    assert ha1.ha_low == 101.5

    assert ha1.is_bullish is True
    assert ha1.is_bearish is False
    assert ha1.is_doji is False
    assert ha1.body_size == 7.25
    assert ha1.upper_wick == 6.25  # 115.0 - 108.75
    assert ha1.lower_wick == 0.0  # 101.5 - 101.5 (Flat bottom)

  def test_doji_candle_hand_calculated(self):
    """Test Vector 3: Doji candle where HA Close == HA Open."""
    c0 = make_test_candle(100.0, 110.0, 90.0, 104.0)
    ha0 = calculate_heikin_ashi_candle(c0, prev_ha=None)

    c1 = make_test_candle(105.0, 115.0, 103.0, 112.0, open_time=1700000060000)
    ha1 = calculate_heikin_ashi_candle(c1, prev_ha=ha0)
    # ha1: ha_open=101.5, ha_close=108.75 -> Next ha_open = 105.125

    # Construct actual candle 2 such that HA Close also equals 105.125
    c2 = make_test_candle(
        105.125, 108.0, 102.25, 105.125, open_time=1700000120000
    )
    ha2 = calculate_heikin_ashi_candle(c2, prev_ha=ha1)

    assert ha2.ha_open == 105.125
    assert ha2.ha_close == 105.125
    assert ha2.is_doji is True
    assert ha2.is_bullish is False
    assert ha2.is_bearish is False
    assert ha2.body_size == 0.0

  def test_extreme_high_low_derived_from_ha_open_close(self):
    """Verify that HA High and Low correctly incorporate HA Open and Close when they exceed actual wicks."""
    # Create candle where actual High is 102, but previous HA was much higher
    ha_prev = HeikinAshiCandle(
        symbol="BTCUSDT",
        interval="1m",
        open_time=1700000000000,
        close_time=1700000059999,
        ha_open=120.0,
        ha_close=110.0,
        ha_high=120.0,
        ha_low=100.0,
        is_closed=True,
    )
    # Next HA Open will be (120 + 110) / 2 = 115.0
    c_current = make_test_candle(
        100.0, 102.0, 98.0, 100.0, open_time=1700000060000
    )
    ha_curr = calculate_heikin_ashi_candle(c_current, prev_ha=ha_prev)

    assert ha_curr.ha_open == 115.0
    assert ha_curr.ha_close == 100.0
    # HA High is max(102.0, 115.0, 100.0) = 115.0
    assert ha_curr.ha_high == 115.0
    # HA Low is min(98.0, 115.0, 100.0) = 98.0
    assert ha_curr.ha_low == 98.0

  def test_unfinished_candle_raises_error(self):
    """Verify unclosed candle is rejected by pure function."""
    c_unclosed = make_test_candle(100.0, 105.0, 95.0, 102.0, is_closed=False)
    with pytest.raises(UnfinishedCandleError):
      calculate_heikin_ashi_candle(c_unclosed)

  def test_calculate_heikin_ashi_series(self):
    """Verify series calculation over multiple consecutive 15m candles."""
    candles = [
        make_test_candle(
            100.0,
            110.0,
            90.0,
            104.0,
            open_time=1700000000000,
            interval="15m",
        ),
        make_test_candle(
            105.0,
            115.0,
            103.0,
            112.0,
            open_time=1700000900000,
            interval="15m",
        ),
        make_test_candle(
            110.0,
            120.0,
            108.0,
            118.0,
            open_time=1700001800000,
            interval="15m",
        ),
    ]

    ha_series = calculate_heikin_ashi_series(candles)
    assert len(ha_series) == 3
    assert ha_series[0].ha_close == 101.0
    assert ha_series[1].ha_close == 108.75
    assert ha_series[2].interval == "15m"

    # Empty series returns empty list
    assert calculate_heikin_ashi_series([]) == []

  def test_series_with_out_of_order_candles_raises_error(self):
    """Verify series raises InconsistentDataError if candle timestamps are out of order."""
    candles = [
        make_test_candle(100.0, 110.0, 90.0, 104.0, open_time=1700000060000),
        make_test_candle(
            105.0, 115.0, 103.0, 112.0, open_time=1700000000000
        ),  # Earlier!
    ]
    with pytest.raises(InconsistentDataError):
      calculate_heikin_ashi_series(candles)

  def test_heikin_ashi_series_stateful_manager(self):
    """Verify HeikinAshiSeries incremental updates, idempotency, and capacity."""
    series = HeikinAshiSeries("BTCUSDT", "1m", max_capacity=50)
    assert len(series) == 0
    assert series.get_latest() is None

    t0 = 1700000000000
    c0 = make_test_candle(100.0, 110.0, 90.0, 104.0, open_time=t0)
    c1 = make_test_candle(105.0, 115.0, 103.0, 112.0, open_time=t0 + 60000)

    # 1. First bar update
    ha0 = series.update(c0)
    assert ha0 is not None
    assert ha0.ha_close == 101.0
    assert len(series) == 1
    assert series.get_latest() == ha0

    # 2. Subsequent bar update
    ha1 = series.update(c1)
    assert ha1 is not None
    assert ha1.ha_close == 108.75
    assert len(series) == 2

    # 3. Duplicate bar update (idempotent replacement)
    c1_new = make_test_candle(
        105.0, 116.0, 103.0, 114.0, open_time=t0 + 60000
    )
    ha1_updated = series.update(c1_new)
    assert ha1_updated is not None
    assert len(series) == 2
    assert series.get_latest().ha_high == 116.0

    # 4. Incomplete candle rejected
    c_unclosed = make_test_candle(
        115.0, 120.0, 110.0, 118.0, open_time=t0 + 120000, is_closed=False
    )
    assert series.update(c_unclosed) is None
    assert len(series) == 2

    # 5. Symbol mismatch rejected
    c_wrong_sym = make_test_candle(
        115.0, 120.0, 110.0, 118.0, symbol="ETHUSDT"
    )
    with pytest.raises(InconsistentDataError):
      series.update(c_wrong_sym)

    # 6. Load from batch
    all_candles = [
        make_test_candle(100.0 + i, 105.0 + i, 95.0 + i, 102.0 + i, open_time=t0 + (i * 60000))
        for i in range(10)
    ]
    loaded_count = series.load_from_candles(all_candles)
    assert loaded_count == 10
    assert len(series) == 10
    assert len(series.get_candles(5)) == 5

    # 7. Reset
    series.reset()
    assert len(series) == 0
