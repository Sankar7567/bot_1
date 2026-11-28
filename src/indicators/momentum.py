"""Pure early-momentum breakout indicators for USD(S)-M Futures (OHLCV-based).

Combines four complementary dimensions to identify coins *entering* momentum
rather than already-exhausted 24h top gainers:
1. Volume Surge (current bar volume / SMA-20 volume)
2. Rate of Change (ROC-9 velocity)
3. Trend Inception (Wilder ADX-14, +DI, -DI)
4. Non-Exhaustion Filter (Wilder RSI-14 avoiding >80 / <20 extremes)
5. Volatility Compression (Bollinger Band Width-20)
"""

from dataclasses import dataclass
import math
from typing import List, Optional, Sequence
from src.market_data.models import Candle


@dataclass(frozen=True)
class MomentumScore:
  """Composite early-momentum evaluation for a single symbol."""

  symbol: str
  score: float
  direction_bias: str  # "LONG" | "SHORT" | "NEUTRAL"
  volume_surge: float
  roc_9: float
  rsi_14: float
  adx_14: float
  plus_di: float
  minus_di: float
  bb_width_pct: float
  last_15m_close_time: int


def compute_roc(closes: Sequence[float], period: int = 9) -> float:
  """Rate of Change (%) over `period` bars."""
  if len(closes) <= period or closes[-1 - period] <= 0:
    return 0.0
  prev = closes[-1 - period]
  return ((closes[-1] - prev) / prev) * 100.0


def compute_rsi(closes: Sequence[float], period: int = 14) -> float:
  """Wilder's Relative Strength Index (0-100)."""
  if len(closes) <= period:
    return 50.0
  deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
  gains = [max(0.0, d) for d in deltas]
  losses = [max(0.0, -d) for d in deltas]

  avg_gain = sum(gains[:period]) / period
  avg_loss = sum(losses[:period]) / period
  for i in range(period, len(deltas)):
    avg_gain = (avg_gain * (period - 1) + gains[i]) / period
    avg_loss = (avg_loss * (period - 1) + losses[i]) / period

  if avg_loss == 0.0:
    return 100.0 if avg_gain > 0 else 50.0
  rs = avg_gain / avg_loss
  return 100.0 - (100.0 / (1.0 + rs))


def compute_adx(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> tuple[float, float, float]:
  """Returns (adx, plus_di, minus_di) using Wilder's smoothing."""
  n = len(closes)
  if n < period * 2 + 1:
    return 0.0, 0.0, 0.0

  tr_list: List[float] = []
  plus_dm: List[float] = []
  minus_dm: List[float] = []

  for i in range(1, n):
    up_move = highs[i] - highs[i - 1]
    down_move = lows[i - 1] - lows[i]
    pdm = up_move if (up_move > down_move and up_move > 0) else 0.0
    mdm = down_move if (down_move > up_move and down_move > 0) else 0.0
    tr = max(
        highs[i] - lows[i],
        abs(highs[i] - closes[i - 1]),
        abs(lows[i] - closes[i - 1]),
    )
    tr_list.append(tr)
    plus_dm.append(pdm)
    minus_dm.append(mdm)

  tr_sm = sum(tr_list[:period])
  pdm_sm = sum(plus_dm[:period])
  mdm_sm = sum(minus_dm[:period])

  dx_list: List[float] = []
  pdi = 0.0
  mdi = 0.0

  for i in range(period, len(tr_list)):
    tr_sm = tr_sm - (tr_sm / period) + tr_list[i]
    pdm_sm = pdm_sm - (pdm_sm / period) + plus_dm[i]
    mdm_sm = mdm_sm - (mdm_sm / period) + minus_dm[i]

    if tr_sm <= 0:
      continue
    pdi = 100.0 * (pdm_sm / tr_sm)
    mdi = 100.0 * (mdm_sm / tr_sm)
    di_sum = pdi + mdi
    dx = (100.0 * abs(pdi - mdi) / di_sum) if di_sum > 0 else 0.0
    dx_list.append(dx)

  if not dx_list:
    return 0.0, pdi, mdi

  adx = sum(dx_list[:period]) / min(period, len(dx_list))
  for dx in dx_list[period:]:
    adx = (adx * (period - 1) + dx) / period

  return adx, pdi, mdi


def compute_volume_surge(volumes: Sequence[float], period: int = 20) -> float:
  """Ratio of latest closed bar volume to prior `period`-bar average volume."""
  if len(volumes) < 2:
    return 1.0
  lookback = volumes[-(period + 1) : -1] if len(volumes) > period else volumes[:-1]
  avg_vol = sum(lookback) / len(lookback) if lookback else 0.0
  if avg_vol <= 0:
    return 1.0
  return volumes[-1] / avg_vol


def compute_bb_width_pct(closes: Sequence[float], period: int = 20) -> float:
  """Bollinger Band width ((upper - lower) / middle * 100) for squeeze detection."""
  if len(closes) < period:
    return 0.0
  window = closes[-period:]
  mean = sum(window) / period
  if mean <= 0:
    return 0.0
  var = sum((x - mean) ** 2 for x in window) / period
  std = math.sqrt(var)
  return (4.0 * std / mean) * 100.0


def evaluate_early_momentum(
    symbol: str,
    candles_15m: Sequence[Candle],
) -> Optional[MomentumScore]:
  """Scores a symbol's 15m completed OHLCV candles for early-breakout momentum.

  Returns None if insufficient history (< 30 bars).
  """
  closed = [c for c in candles_15m if c.is_closed]
  if len(closed) < 30:
    return None

  closes = [c.close for c in closed]
  highs = [c.high for c in closed]
  lows = [c.low for c in closed]
  volumes = [c.volume for c in closed]

  vol_surge = compute_volume_surge(volumes, period=20)
  roc_9 = compute_roc(closes, period=9)
  rsi_14 = compute_rsi(closes, period=14)
  adx_14, plus_di, minus_di = compute_adx(highs, lows, closes, period=14)
  bb_width = compute_bb_width_pct(closes, period=20)

  # Determine directional momentum alignment & filter out exhausted extremes
  score = 0.0
  bias = "NEUTRAL"

  bullish_aligned = roc_9 > 0.25 and plus_di > minus_di and 52.0 <= rsi_14 <= 78.0
  bearish_aligned = roc_9 < -0.25 and minus_di > plus_di and 22.0 <= rsi_14 <= 48.0

  if bullish_aligned or bearish_aligned:
    bias = "LONG" if bullish_aligned else "SHORT"
    # 1. Volume surge component (strongest weight for fresh participation)
    vol_pts = min(vol_surge, 5.0) * 20.0 if vol_surge >= 1.2 else vol_surge * 8.0
    # 2. Trend inception ADX sweet-spot (18..45)
    adx_pts = min(adx_14, 45.0) * 1.2 if adx_14 >= 16.0 else adx_14 * 0.4
    # 3. Velocity ROC magnitude (capped to avoid chasing already-parabolic coins)
    roc_pts = min(abs(roc_9), 8.0) * 5.0
    # 4. RSI healthy momentum bonus (peaks around 62-68 for LONG, 32-38 for SHORT)
    rsi_dist = abs(rsi_14 - (65.0 if bullish_aligned else 35.0))
    rsi_pts = max(0.0, 20.0 - rsi_dist)
    # 5. Squeeze bonus (tighter BB width before/at breakout scores higher)
    squeeze_pts = max(0.0, 15.0 - bb_width * 2.0)

    score = vol_pts + adx_pts + roc_pts + rsi_pts + squeeze_pts

  return MomentumScore(
      symbol=symbol.upper(),
      score=round(score, 4),
      direction_bias=bias,
      volume_surge=round(vol_surge, 3),
      roc_9=round(roc_9, 3),
      rsi_14=round(rsi_14, 2),
      adx_14=round(adx_14, 2),
      plus_di=round(plus_di, 2),
      minus_di=round(minus_di, 2),
      bb_width_pct=round(bb_width, 3),
      last_15m_close_time=closed[-1].close_time,
  )
