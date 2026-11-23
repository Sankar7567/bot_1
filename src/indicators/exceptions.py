"""Exceptions for Indicator modules."""

from typing import Optional


class IndicatorError(Exception):
  """Base exception for all indicator calculation errors."""

  def __init__(self, message: str, code: Optional[str] = None):
    super().__init__(message)
    self.message = message
    self.code = code


class InconsistentDataError(IndicatorError):
  """Raised when candle sequence is not sorted or contains gaps."""

  pass


class UnfinishedCandleError(IndicatorError):
  """Raised when attempting to calculate indicators on unfinished candles."""

  pass
