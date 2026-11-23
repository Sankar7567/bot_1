"""Exceptions for Market Scanner."""

from typing import Optional


class ScannerError(Exception):
  """Base exception for all market scanner errors."""

  def __init__(self, message: str, code: Optional[str] = None):
    super().__init__(message)
    self.message = message
    self.code = code


class EmptyUniverseError(ScannerError):
  """Raised when zero symbols qualify for the market universe."""

  pass


class StaleDataError(ScannerError):
  """Raised when market data is stale or ticker timestamps lag exchange time."""

  pass


class ScannerAPIError(ScannerError):
  """Raised when an underlying exchange API error interrupts market scanning."""

  pass
