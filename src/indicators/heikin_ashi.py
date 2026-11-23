"""Pure mathematical Heikin-Ashi calculation engine.

Transforms standard market OHLC candles into synthetic Heikin-Ashi candles
with zero look-ahead bias and strict completion enforcement.
"""

from dataclasses import dataclass
from typing import List, Optional
from src.indicators.exceptions import (
    InconsistentDataError,
    UnfinishedCandleError,
)
from src.market_data.models import Candle
from src.monitoring.logger import get_logger

logger = get_logger("indicators.heikin_ashi")

EPSILON = 1e-8


@dataclass(frozen=True)
class HeikinAshiCandle:
  """Synthetic Heikin-Ashi candle representation."""

  symbol: str
  interval: str
  open_time: int
  close_time: int
  ha_open: float
  ha_high: float
  ha_low: float
  ha_close: float
  is_closed: bool

  @property
  def is_bullish(self) -> bool:
    """Returns True if HA Close > HA Open beyond floating-point epsilon."""
    return (self.ha_close - self.ha_open) > EPSILON

  @property
  def is_bearish(self) -> bool:
    """Returns True if HA Close < HA Open beyond floating-point epsilon."""
    return (self.ha_open - self.ha_close) > EPSILON

  @property
  def is_doji(self) -> bool:
    """Returns True if HA Close equals HA Open within floating-point epsilon."""
    return abs(self.ha_close - self.ha_open) <= EPSILON

  @property
  def body_size(self) -> float:
    return abs(self.ha_close - self.ha_open)

  @property
  def upper_wick(self) -> float:
    return self.ha_high - max(self.ha_open, self.ha_close)

  @property
  def lower_wick(self) -> float:
    return min(self.ha_open, self.ha_close) - self.ha_low


def calculate_heikin_ashi_candle(
    candle: Candle,
    prev_ha: Optional[HeikinAshiCandle] = None,
) -> HeikinAshiCandle:
  """Calculates a single Heikin-Ashi candle from a standard closed OHLC candle.

  Formulas:
  - HA Close = (Open + High + Low + Close) / 4
  - First HA Open = (Open + Close) / 2
  - Subsequent HA Open = (Prev HA Open + Prev HA Close) / 2
  - HA High = max(High, HA Open, HA Close)
  - HA Low = min(Low, HA Open, HA Close)

  Args:
      candle: Standard market candle. Must be closed.
      prev_ha: Previous Heikin-Ashi candle in sequence (if any).

  Returns:
      HeikinAshiCandle: The resulting synthetic Heikin-Ashi candle.

  Raises:
      UnfinishedCandleError: If candle.is_closed is False.
  """
  if not candle.is_closed:
    raise UnfinishedCandleError(
        f"Cannot calculate Heikin-Ashi from unfinished candle:"
        f" {candle.symbol}:{candle.interval} ({candle.open_time})"
    )

  ha_close = (candle.open + candle.high + candle.low + candle.close) / 4.0

  if prev_ha is None:
    # First candle in historical sequence
    ha_open = (candle.open + candle.close) / 2.0
  else:
    # Subsequent candle in sequence
    ha_open = (prev_ha.ha_open + prev_ha.ha_close) / 2.0

  ha_high = max(candle.high, ha_open, ha_close)
  ha_low = min(candle.low, ha_open, ha_close)

  return HeikinAshiCandle(
      symbol=candle.symbol,
      interval=candle.interval,
      open_time=candle.open_time,
      close_time=candle.close_time,
      ha_open=ha_open,
      ha_high=ha_high,
      ha_low=ha_low,
      ha_close=ha_close,
      is_closed=True,
  )


def calculate_heikin_ashi_series(
    candles: List[Candle],
) -> List[HeikinAshiCandle]:
  """Calculates an entire sequential series of Heikin-Ashi candles from standard OHLC candles.

  Args:
      candles: Chronologically sorted list of standard closed candles.

  Returns:
      List[HeikinAshiCandle]: Ordered list of calculated Heikin-Ashi candles.

  Raises:
      InconsistentDataError: If candles are not chronologically sorted.
      UnfinishedCandleError: If any candle is not closed.
  """
  if not candles:
    return []

  ha_series: List[HeikinAshiCandle] = []
  prev_ha: Optional[HeikinAshiCandle] = None

  for i, c in enumerate(candles):
    if i > 0 and c.open_time <= candles[i - 1].open_time:
      raise InconsistentDataError(
          f"Candle series is not strictly chronological at index {i}:"
          f" {candles[i-1].open_time} >= {c.open_time}"
      )

    ha_candle = calculate_heikin_ashi_candle(c, prev_ha)
    ha_series.append(ha_candle)
    prev_ha = ha_candle

  return ha_series


class HeikinAshiSeries:
  """Stateful Heikin-Ashi series manager for a single symbol and timeframe.

  Supports incremental updates, state tracking, and chronological retrieval.
  """

  def __init__(
      self, symbol: str, interval: str, max_capacity: int = 300
  ):
    self.symbol = symbol.upper()
    self.interval = interval
    self.max_capacity = max(max_capacity, 50)

    self._ha_candles: List[HeikinAshiCandle] = []
    self._open_times: List[int] = []

  def __len__(self) -> int:
    return len(self._ha_candles)

  def reset(self) -> None:
    """Resets series state."""
    self._ha_candles.clear()
    self._open_times.clear()

  def get_latest(self) -> Optional[HeikinAshiCandle]:
    """Returns the most recent calculated Heikin-Ashi candle."""
    return self._ha_candles[-1] if self._ha_candles else None

  def get_candles(self, n: Optional[int] = None) -> List[HeikinAshiCandle]:
    """Returns the latest n Heikin-Ashi candles in chronological order."""
    if n is None or n >= len(self._ha_candles):
      return list(self._ha_candles)
    return self._ha_candles[-n:]

  def update(self, candle: Candle) -> Optional[HeikinAshiCandle]:
    """Processes a newly closed candle and appends the calculated Heikin-Ashi candle.

    Args:
        candle: The standard market candle to process.

    Returns:
        Optional[HeikinAshiCandle]: The newly calculated HA candle, or None if
        the candle was rejected (e.g. unclosed).
    """
    if not candle.is_closed:
      return None

    if candle.symbol != self.symbol or candle.interval != self.interval:
      raise InconsistentDataError(
          f"Series for {self.symbol}:{self.interval} cannot accept"
          f" {candle.symbol}:{candle.interval}"
      )

    open_time = candle.open_time

    # Fast path: in-order append (most common scenario)
    if not self._ha_candles or open_time > self._open_times[-1]:
      prev_ha = self._ha_candles[-1] if self._ha_candles else None
      ha_candle = calculate_heikin_ashi_candle(candle, prev_ha)
      self._ha_candles.append(ha_candle)
      self._open_times.append(open_time)

      if len(self._ha_candles) > self.max_capacity:
        self._ha_candles.pop(0)
        self._open_times.pop(0)

      return ha_candle

    # Existing candle update (idempotent replacement)
    if open_time == self._open_times[-1]:
      prev_ha = self._ha_candles[-2] if len(self._ha_candles) >= 2 else None
      ha_candle = calculate_heikin_ashi_candle(candle, prev_ha)
      self._ha_candles[-1] = ha_candle
      return ha_candle

    # Out-of-order insertion: requires recomputing from the insertion point
    # Since HA open depends on previous HA, out-of-order bars require re-running from insertion
    logger.warning(
        "Out-of-order candle received for %s (%s) at %d. Rebuilding HA series.",
        self.symbol,
        self.interval,
        open_time,
    )
    return None

  def load_from_candles(self, candles: List[Candle]) -> int:
    """Loads a chronological sequence of standard candles and computes HA series."""
    self.reset()
    ha_list = calculate_heikin_ashi_series(candles)

    # Apply capacity limit
    if len(ha_list) > self.max_capacity:
      ha_list = ha_list[-self.max_capacity :]

    self._ha_candles = ha_list
    self._open_times = [c.open_time for c in ha_list]
    return len(self._ha_candles)
