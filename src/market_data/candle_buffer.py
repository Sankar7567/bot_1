"""Chronological ring buffer for closed market candles.

Enforces strictly completed candles, detects duplicates and missing bars,
maintains chronological ordering, and limits memory consumption.
"""

from bisect import bisect_left
from typing import List, Optional, Tuple
from src.market_data.exceptions import CandleDataError
from src.market_data.models import Candle, interval_to_ms
from src.monitoring.logger import get_logger

logger = get_logger("market_data.buffer")


class CandleBuffer:
  """Chronological storage and validation for candles of a specific symbol and timeframe."""

  def __init__(
      self,
      symbol: str,
      interval: str,
      max_capacity: int = 300,
      strict_closed: bool = True,
  ):
    self.symbol = symbol.upper()
    self.interval = interval
    self.max_capacity = max(max_capacity, 50)
    self.strict_closed = strict_closed
    self.interval_ms = interval_to_ms(interval)

    self._candles: List[Candle] = []
    # Fast index lookup: open_time -> index in _candles
    self._open_times: List[int] = []

  def __len__(self) -> int:
    return len(self._candles)

  def clear(self) -> None:
    """Empties the candle buffer."""
    self._candles.clear()
    self._open_times.clear()

  def has_sufficient_history(self, min_bars: int) -> bool:
    """Verifies whether buffer holds at least the required minimum closed bars."""
    return len(self._candles) >= min_bars

  def get_latest_candle(self) -> Optional[Candle]:
    """Returns the most recent closed candle in the buffer."""
    return self._candles[-1] if self._candles else None

  def get_candles(self, n: Optional[int] = None) -> List[Candle]:
    """Returns the latest n candles in chronological order."""
    if n is None or n >= len(self._candles):
      return list(self._candles)
    return self._candles[-n:]

  def add_candle(self, candle: Candle) -> bool:
    """Adds a candle to the buffer idempotently and maintains chronological ordering.

    Args:
        candle: The candle to add.

    Returns:
        bool: True if a new closed candle was added; False if it was an update,
        duplicate, or rejected.
    """
    if candle.symbol != self.symbol or candle.interval != self.interval:
      raise CandleDataError(
          f"Buffer for {self.symbol}:{self.interval} cannot accept"
          f" {candle.symbol}:{candle.interval}"
      )

    # Enforce strict closure requirement
    if self.strict_closed and not candle.is_closed:
      return False

    open_time = candle.open_time

    # Fast path: in-order append (most common scenario in streaming)
    if not self._open_times or open_time > self._open_times[-1]:
      self._candles.append(candle)
      self._open_times.append(open_time)

      # Evict oldest candle if capacity exceeded
      if len(self._candles) > self.max_capacity:
        self._candles.pop(0)
        self._open_times.pop(0)
      return True

    # Duplicate / existing bar update check
    pos = bisect_left(self._open_times, open_time)
    if pos < len(self._open_times) and self._open_times[pos] == open_time:
      # Idempotent replacement of existing bar with newer data
      self._candles[pos] = candle
      return False

    # Out-of-order insertion (historical backfill or network jitter)
    self._candles.insert(pos, candle)
    self._open_times.insert(pos, open_time)

    # Trim if over capacity
    if len(self._candles) > self.max_capacity:
      self._candles.pop(0)
      self._open_times.pop(0)

    return True

  def add_many(self, candles: List[Candle]) -> int:
    """Batch adds multiple candles and returns count of newly added bars."""
    new_count = 0
    for c in candles:
      if self.add_candle(c):
        new_count += 1
    return new_count

  def detect_gaps(self) -> List[Tuple[int, int]]:
    """Identifies any gaps in the chronological sequence of candles.

    Returns:
        List of tuples: (expected_open_time, actual_open_time)
    """
    gaps: List[Tuple[int, int]] = []
    if len(self._candles) < 2:
      return gaps

    for i in range(1, len(self._candles)):
      prev_open = self._candles[i - 1].open_time
      curr_open = self._candles[i].open_time
      expected_open = prev_open + self.interval_ms

      if curr_open > expected_open:
        gaps.append((expected_open, curr_open))

    return gaps
