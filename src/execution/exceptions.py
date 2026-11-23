"""Domain exceptions for the Order Execution module."""


class ExecutionError(Exception):
  """Base exception for all order execution failures."""

  def __init__(self, reason: str, client_order_id: str = ""):
    self.reason = reason
    self.client_order_id = client_order_id
    super().__init__(reason)

  def __str__(self) -> str:
    cid = f" [cid={self.client_order_id}]" if self.client_order_id else ""
    return f"{self.__class__.__name__}{cid}: {self.reason}"


class OrderRejectedError(ExecutionError):
  """Raised when the exchange rejects an order placement."""


class FillTimeoutError(ExecutionError):
  """Raised when an order does not reach FILLED status within the allowed timeout."""


class PartialFillError(ExecutionError):
  """Raised when a market order is only partially filled and cannot be reconciled."""


class ProtectiveOrderError(ExecutionError):
  """Raised when SL or TP protective orders cannot be placed or verified."""


class CancellationError(ExecutionError):
  """Raised when an order cancellation fails or cannot be verified."""
