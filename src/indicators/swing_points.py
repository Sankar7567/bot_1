"""Pure 5-left / 5-right Swing High and Swing Low Pivot Detector.

Uses actual market OHLC prices (never synthetic Heikin-Ashi values) with strict
inequalities, zero look-ahead bias, and configurable buffer stop-loss calculations.
"""

from dataclasses import dataclass
from enum import Enum
import math
from typing import List, Optional
from src.indicators.exceptions import InconsistentDataError
from src.market_data.models import Candle
from src.monitoring.logger import get_logger

logger = get_logger("indicators.swing_points")


class PivotType(str, Enum):
  SWING_LOW = "SWING_LOW"
  SWING_HIGH = "SWING_HIGH"


@dataclass(frozen=True)
class SwingPivot:
  """Confirmed swing pivot point on actual market OHLC data."""

  symbol: str
  interval: str
  pivot_type: PivotType
  bar_index: int  # Index in evaluated sequence
  pivot_timestamp: int  # UTC ms open_time of pivot candle
  confirmation_timestamp: int  # UTC ms close_time of bar_index + right_bars
  price: float  # Actual Low for SWING_LOW, Actual High for SWING_HIGH
  left_bars: int = 5
  right_bars: int = 5


@dataclass(frozen=True)
class StopLossCalculation:
  """Calculated stop loss price based on confirmed swing pivot and buffer."""

  pivot: SwingPivot
  entry_price: float
  buffer_ratio: float
  stop_price: float
  stop_distance: float
  is_valid: bool
  rejection_reason: Optional[str] = None


def find_confirmed_pivots(
    candles: List[Candle],
    left_bars: int = 5,
    right_bars: int = 5,
) -> List[SwingPivot]:
  """Identifies all confirmed swing highs and lows in a chronological candle sequence.

  Strict Rules:
  - Swing Low: candle.low < low of left_bars immediately before AND right_bars immediately after.
  - Swing High: candle.high > high of left_bars immediately before AND right_bars immediately after.
  - Equal highs/lows do not qualify (strict inequality required).
  - Uses actual OHLC data only.
  """
  total_bars = len(candles)

  # Check chronological ordering first
  for k in range(1, total_bars):
    if candles[k].open_time <= candles[k - 1].open_time:
      raise InconsistentDataError(
          f"Candle sequence not strictly chronological at index {k}:"
          f" {candles[k-1].open_time} >= {candles[k].open_time}"
      )

  required_bars = left_bars + right_bars + 1
  if total_bars < required_bars:
    return []

  pivots: List[SwingPivot] = []

  # Pivot bar index i ranges from left_bars to total_bars - right_bars - 1
  for i in range(left_bars, total_bars - right_bars):
    cand = candles[i]
    if not cand.is_closed:
      continue

    curr_low = cand.low
    curr_high = cand.high

    if math.isnan(curr_low) or math.isnan(curr_high):
      continue

    confirmation_candle = candles[i + right_bars]
    if not confirmation_candle.is_closed:
      continue
    confirmation_time = confirmation_candle.close_time

    # Evaluate Swing Low (strict inequality)
    is_pivot_low = True
    # Left bars check (i - left_bars to i - 1)
    for j in range(i - left_bars, i):
      if curr_low >= candles[j].low:
        is_pivot_low = False
        break

    # Right bars check (i + 1 to i + right_bars)
    if is_pivot_low:
      for k in range(i + 1, i + right_bars + 1):
        if curr_low >= candles[k].low:
          is_pivot_low = False
          break

    if is_pivot_low:
      pivots.append(
          SwingPivot(
              symbol=cand.symbol,
              interval=cand.interval,
              pivot_type=PivotType.SWING_LOW,
              bar_index=i,
              pivot_timestamp=cand.open_time,
              confirmation_timestamp=confirmation_time,
              price=curr_low,
              left_bars=left_bars,
              right_bars=right_bars,
          )
      )

    # Evaluate Swing High (strict inequality)
    is_pivot_high = True
    for j in range(i - left_bars, i):
      if curr_high <= candles[j].high:
        is_pivot_high = False
        break

    if is_pivot_high:
      for k in range(i + 1, i + right_bars + 1):
        if curr_high <= candles[k].high:
          is_pivot_high = False
          break

    if is_pivot_high:
      pivots.append(
          SwingPivot(
              symbol=cand.symbol,
              interval=cand.interval,
              pivot_type=PivotType.SWING_HIGH,
              bar_index=i,
              pivot_timestamp=cand.open_time,
              confirmation_timestamp=confirmation_time,
              price=curr_high,
              left_bars=left_bars,
              right_bars=right_bars,
          )
      )

  return pivots


def get_latest_eligible_pivot(
    candles: List[Candle],
    pivot_type: PivotType,
    left_bars: int = 5,
    right_bars: int = 5,
    lookback_bars: int = 100,
) -> Optional[SwingPivot]:
  """Searches backwards to find the most recent confirmed pivot of the requested type.

  Only pivots whose right_bars have officially completed at or before the latest
  candle are eligible.
  """
  required_bars = left_bars + right_bars + 1
  total_bars = len(candles)
  if total_bars < required_bars:
    return None

  # Restrict evaluation to lookback window
  start_idx = max(left_bars, total_bars - lookback_bars)
  # Latest bar that has right_bars completed is total_bars - 1 - right_bars
  latest_possible_idx = total_bars - 1 - right_bars

  if latest_possible_idx < start_idx:
    return None

  # Search backwards from latest_possible_idx down to start_idx
  for i in range(latest_possible_idx, start_idx - 1, -1):
    cand = candles[i]
    if not cand.is_closed:
      continue

    confirmation_candle = candles[i + right_bars]
    if not confirmation_candle.is_closed:
      continue

    if pivot_type == PivotType.SWING_LOW:
      curr_low = cand.low
      is_low = True
      for j in range(i - left_bars, i):
        if curr_low >= candles[j].low:
          is_low = False
          break
      if is_low:
        for k in range(i + 1, i + right_bars + 1):
          if curr_low >= candles[k].low:
            is_low = False
            break
      if is_low:
        return SwingPivot(
            symbol=cand.symbol,
            interval=cand.interval,
            pivot_type=PivotType.SWING_LOW,
            bar_index=i,
            pivot_timestamp=cand.open_time,
            confirmation_timestamp=confirmation_candle.close_time,
            price=curr_low,
            left_bars=left_bars,
            right_bars=right_bars,
        )

    elif pivot_type == PivotType.SWING_HIGH:
      curr_high = cand.high
      is_high = True
      for j in range(i - left_bars, i):
        if curr_high <= candles[j].high:
          is_high = False
          break
      if is_high:
        for k in range(i + 1, i + right_bars + 1):
          if curr_high <= candles[k].high:
            is_high = False
            break
      if is_high:
        return SwingPivot(
            symbol=cand.symbol,
            interval=cand.interval,
            pivot_type=PivotType.SWING_HIGH,
            bar_index=i,
            pivot_timestamp=cand.open_time,
            confirmation_timestamp=confirmation_candle.close_time,
            price=curr_high,
            left_bars=left_bars,
            right_bars=right_bars,
        )

  return None


def calculate_swing_stop_loss(
    entry_price: float,
    pivot: SwingPivot,
    is_long: bool,
    buffer_ratio: float = 0.001,
) -> StopLossCalculation:
  """Calculates the stop-loss price level using the confirmed swing pivot and buffer.

  Rules:
  - For LONG:
      Requires SWING_LOW.
      SL = swing_low * (1 - buffer_ratio).
      Must satisfy SL < entry_price and stop_distance > 0.
  - For SHORT:
      Requires SWING_HIGH.
      SL = swing_high * (1 + buffer_ratio).
      Must satisfy SL > entry_price and stop_distance > 0.
  """
  if entry_price <= 0:
    return StopLossCalculation(
        pivot=pivot,
        entry_price=entry_price,
        buffer_ratio=buffer_ratio,
        stop_price=0.0,
        stop_distance=0.0,
        is_valid=False,
        rejection_reason="Entry price must be positive.",
    )

  if is_long:
    if pivot.pivot_type != PivotType.SWING_LOW:
      return StopLossCalculation(
          pivot=pivot,
          entry_price=entry_price,
          buffer_ratio=buffer_ratio,
          stop_price=0.0,
          stop_distance=0.0,
          is_valid=False,
          rejection_reason=(
              f"LONG trade requires SWING_LOW pivot, got {pivot.pivot_type}."
          ),
      )

    stop_price = pivot.price * (1.0 - buffer_ratio)
    if stop_price <= 0:
      return StopLossCalculation(
          pivot=pivot,
          entry_price=entry_price,
          buffer_ratio=buffer_ratio,
          stop_price=stop_price,
          stop_distance=0.0,
          is_valid=False,
          rejection_reason="Calculated stop price is non-positive.",
      )

    if stop_price >= entry_price:
      return StopLossCalculation(
          pivot=pivot,
          entry_price=entry_price,
          buffer_ratio=buffer_ratio,
          stop_price=stop_price,
          stop_distance=0.0,
          is_valid=False,
          rejection_reason=(
              f"LONG stop price ({stop_price:.4f}) must be strictly below"
              f" entry ({entry_price:.4f})."
          ),
      )

    stop_distance = entry_price - stop_price
    return StopLossCalculation(
        pivot=pivot,
        entry_price=entry_price,
        buffer_ratio=buffer_ratio,
        stop_price=stop_price,
        stop_distance=stop_distance,
        is_valid=True,
    )

  else:  # SHORT
    if pivot.pivot_type != PivotType.SWING_HIGH:
      return StopLossCalculation(
          pivot=pivot,
          entry_price=entry_price,
          buffer_ratio=buffer_ratio,
          stop_price=0.0,
          stop_distance=0.0,
          is_valid=False,
          rejection_reason=(
              f"SHORT trade requires SWING_HIGH pivot, got {pivot.pivot_type}."
          ),
      )

    stop_price = pivot.price * (1.0 + buffer_ratio)
    if stop_price <= entry_price:
      return StopLossCalculation(
          pivot=pivot,
          entry_price=entry_price,
          buffer_ratio=buffer_ratio,
          stop_price=stop_price,
          stop_distance=0.0,
          is_valid=False,
          rejection_reason=(
              f"SHORT stop price ({stop_price:.4f}) must be strictly above"
              f" entry ({entry_price:.4f})."
          ),
      )

    stop_distance = stop_price - entry_price
    return StopLossCalculation(
        pivot=pivot,
        entry_price=entry_price,
        buffer_ratio=buffer_ratio,
        stop_price=stop_price,
        stop_distance=stop_distance,
        is_valid=True,
    )


class SwingDetector:
  """Stateful swing detector managing historical candles and pivot detection."""

  def __init__(
      self,
      symbol: str,
      interval: str = "1m",
      left_bars: int = 5,
      right_bars: int = 5,
      lookback_bars: int = 100,
  ):
    self.symbol = symbol.upper()
    self.interval = interval
    self.left_bars = left_bars
    self.right_bars = right_bars
    self.lookback_bars = lookback_bars

  def get_latest_swing_low(self, candles: List[Candle]) -> Optional[SwingPivot]:
    """Finds the most recent confirmed swing low."""
    return get_latest_eligible_pivot(
        candles=candles,
        pivot_type=PivotType.SWING_LOW,
        left_bars=self.left_bars,
        right_bars=self.right_bars,
        lookback_bars=self.lookback_bars,
    )

  def get_latest_swing_high(self, candles: List[Candle]) -> Optional[SwingPivot]:
    """Finds the most recent confirmed swing high."""
    return get_latest_eligible_pivot(
        candles=candles,
        pivot_type=PivotType.SWING_HIGH,
        left_bars=self.left_bars,
        right_bars=self.right_bars,
        lookback_bars=self.lookback_bars,
    )
