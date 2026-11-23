"""Data models and schemas for 1-minute and 15-minute market candles."""

from dataclasses import dataclass
from typing import Any, List
from src.market_data.exceptions import CandleDataError


def interval_to_ms(interval: str) -> int:
  """Converts Binance interval string to duration in milliseconds."""
  unit = interval[-1]
  val = int(interval[:-1])
  if unit == "m":
    return val * 60 * 1000
  elif unit == "h":
    return val * 60 * 60 * 1000
  elif unit == "d":
    return val * 24 * 60 * 60 * 1000
  raise ValueError(f"Unsupported interval: {interval}")


@dataclass(frozen=True)
class Candle:
  """Immutable representation of a market candle."""

  symbol: str
  interval: str
  open_time: int  # UTC ms timestamp (bar open)
  close_time: int  # UTC ms timestamp (bar close)
  open: float
  high: float
  low: float
  close: float
  volume: float
  quote_volume: float
  trades_count: int
  is_closed: bool

  @property
  def duration_ms(self) -> int:
    return self.close_time - self.open_time + 1

  @property
  def is_bullish(self) -> bool:
    return self.close > self.open

  @property
  def is_bearish(self) -> bool:
    return self.close < self.open

  @classmethod
  def from_rest_kline(
      cls,
      symbol: str,
      interval: str,
      raw: List[Any],
      is_closed: bool = True,
  ) -> "Candle":
    """Parses a Binance REST kline array into a typed Candle instance."""
    try:
      return cls(
          symbol=symbol.upper(),
          interval=interval,
          open_time=int(raw[0]),
          open=float(raw[1]),
          high=float(raw[2]),
          low=float(raw[3]),
          close=float(raw[4]),
          volume=float(raw[5]),
          close_time=int(raw[6]),
          quote_volume=float(raw[7]),
          trades_count=int(raw[8]),
          is_closed=is_closed,
      )
    except (IndexError, ValueError, TypeError) as err:
      raise CandleDataError(
          f"Failed to parse REST kline for {symbol} ({interval}): {raw}"
      ) from err

  @classmethod
  def from_ws_payload(cls, payload: dict) -> "Candle":
    """Parses a Binance WebSocket kline event dictionary into a typed Candle instance."""
    try:
      k = payload.get("k", payload)
      return cls(
          symbol=k["s"].upper(),
          interval=k["i"],
          open_time=int(k["t"]),
          close_time=int(k["T"]),
          open=float(k["o"]),
          high=float(k["h"]),
          low=float(k["l"]),
          close=float(k["c"]),
          volume=float(k["v"]),
          quote_volume=float(k.get("q", 0.0)),
          trades_count=int(k.get("n", 0)),
          is_closed=bool(k["x"]),
      )
    except (KeyError, ValueError, TypeError) as err:
      raise CandleDataError(
          f"Failed to parse WebSocket kline payload: {payload}"
      ) from err


@dataclass(frozen=True)
class CandleCloseEvent:
  """Event emitted strictly when a candle completes and closes."""

  symbol: str
  interval: str
  candle: Candle
  emitted_at: int  # UTC ms timestamp when emitted
