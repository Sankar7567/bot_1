"""Domain exceptions for the Risk Management module."""


class RiskValidationError(Exception):
  """Base class for all risk validation failures."""

  def __init__(self, reason: str):
    self.reason = reason
    super().__init__(reason)

  def __str__(self) -> str:
    return f"RiskValidationError: {self.reason}"


class InsufficientEquityError(RiskValidationError):
  """Raised when account equity is too low to open any position."""


class InvalidStopDistanceError(RiskValidationError):
  """Raised when stop distance is zero, negative, or logically invalid."""


class LiquidationRiskError(RiskValidationError):
  """Raised when estimated liquidation price is tighter than the swing stop-loss."""
