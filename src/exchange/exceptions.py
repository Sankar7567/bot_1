"""Exception hierarchy for Binance USDⓈ-M Futures connector.

Structured domain errors with error codes, messages, and retryability classifications.
"""

from typing import Optional


class BinanceConnectorError(Exception):
  """Base exception for all Binance connector errors."""

  def __init__(self, message: str, code: Optional[int] = None):
    super().__init__(message)
    self.message = message
    self.code = code

  def __str__(self) -> str:
    if self.code is not None:
      return f"[{self.code}] {self.message}"
    return self.message


class BinanceNetworkError(BinanceConnectorError):
  """Raised when a network-level error occurs (DNS, connection reset, timeout)."""

  pass


class BinanceAuthError(BinanceConnectorError):
  """Raised when authentication fails (invalid API key, signature, or permissions)."""

  pass


class BinanceRateLimitError(BinanceConnectorError):
  """Raised when exchange rate limits are exceeded (HTTP 429 or 418)."""

  def __init__(
      self,
      message: str,
      code: Optional[int] = None,
      retry_after_seconds: float = 1.0,
  ):
    super().__init__(message, code)
    self.retry_after_seconds = retry_after_seconds


class BinanceAPIError(BinanceConnectorError):
  """Raised when Binance returns a structured API error response."""

  def __init__(
      self, message: str, code: int, status_code: Optional[int] = None
  ):
    super().__init__(message, code)
    self.status_code = status_code


class BinanceFilterError(BinanceConnectorError):
  """Raised when symbol filter data is invalid or cannot be parsed."""

  pass
