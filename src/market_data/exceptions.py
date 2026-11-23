"""Exceptions for Market Data Engine."""

from typing import Optional


class MarketDataError(Exception):
  """Base exception for all market data errors."""

  def __init__(self, message: str, code: Optional[str] = None):
    super().__init__(message)
    self.message = message
    self.code = code


class CandleDataError(MarketDataError):
  """Raised when candle data is malformed or invalid."""

  pass


class MissingCandleError(MarketDataError):
  """Raised when a gap in the chronological candle sequence is detected."""

  pass


class StaleCandleError(MarketDataError):
  """Raised when candle data lags significantly behind exchange time."""

  pass


class WebSocketFeedError(MarketDataError):
  """Raised when WebSocket stream connection fails or drops."""

  pass
