"""Domain exceptions for Position Management."""

from src.execution.exceptions import ExecutionError


class PositionError(ExecutionError):
  """Base exception for all position management failures."""


class PositionLockError(PositionError):
  """Raised when an operation violates the global 1-position lock invariant."""


class PositionNotFoundError(PositionError):
  """Raised when a referenced position is not found in memory or database."""
