"""Models and data structures for protective orders and position tracking."""

from dataclasses import dataclass
from enum import Enum
import time
from typing import Optional
from src.execution.models import OrderSide, OrderType


class ExitReason(str, Enum):
  STOP_LOSS = "STOP_LOSS"
  TAKE_PROFIT = "TAKE_PROFIT"
  EMERGENCY = "EMERGENCY"
  MANUAL = "MANUAL"


@dataclass
class ProtectiveOrderPair:
  """Represents an active pair of exchange-side SL and TP conditional orders."""

  symbol: str
  entry_side: OrderSide
  exit_side: OrderSide
  quantity: float
  avg_entry_price: float
  sl_order_id: int
  sl_client_order_id: str
  sl_price: float
  tp_order_id: int
  tp_client_order_id: str
  tp_price: float
  created_at: int = 0
  is_active: bool = True

  def __post_init__(self):
    if not self.created_at:
      self.created_at = int(time.time() * 1000)

  def is_sl_order(
      self,
      client_order_id: Optional[str] = None,
      order_id: Optional[int] = None,
  ) -> bool:
    """Returns True if the order ID matches the Stop-Loss order."""
    if client_order_id and client_order_id == self.sl_client_order_id:
      return True
    if order_id is not None and order_id == self.sl_order_id:
      return True
    return False

  def is_tp_order(
      self,
      client_order_id: Optional[str] = None,
      order_id: Optional[int] = None,
  ) -> bool:
    """Returns True if the order ID matches the Take-Profit order."""
    if client_order_id and client_order_id == self.tp_client_order_id:
      return True
    if order_id is not None and order_id == self.tp_order_id:
      return True
    return False

  def get_sibling_order_details(
      self,
      triggered_client_order_id: Optional[str] = None,
      triggered_order_id: Optional[int] = None,
  ) -> tuple[str, int, OrderType]:
    """Given one triggered order in the pair, returns the sibling order details.

    Returns:
        tuple of (sibling_client_order_id, sibling_order_id, sibling_type)
    """
    if self.is_sl_order(triggered_client_order_id, triggered_order_id):
      return self.tp_client_order_id, self.tp_order_id, OrderType.TAKE_PROFIT_MARKET
    elif self.is_tp_order(triggered_client_order_id, triggered_order_id):
      return self.sl_client_order_id, self.sl_order_id, OrderType.STOP_MARKET
    else:
      raise ValueError("Neither SL nor TP matches the triggered order ID.")


@dataclass(frozen=True)
class ExitTriggerResult:
  """Result of processing an exit order fill and cancelling its sibling."""

  symbol: str
  triggered_client_order_id: str
  triggered_type: OrderType
  exit_reason: ExitReason
  cancelled_sibling_client_order_id: str
  sibling_cancellation_success: bool
  position_closed_verified: bool
  timestamp_ms: int


@dataclass(frozen=True)
class EmergencyExitResult:
  """Result of executing an emergency market close when protection fails."""

  symbol: str
  emergency_client_order_id: str
  quantity: float
  is_closed: bool
  error_message: Optional[str] = None
  timestamp_ms: int = 0
