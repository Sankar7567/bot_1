"""Models and data structures for protective orders and position tracking."""

from dataclasses import dataclass
from enum import Enum
import time
from typing import Optional
from src.execution.models import OrderSide, OrderType


class PositionState(str, Enum):
  """Lifecycle states of a bot-managed position."""

  IDLE = "IDLE"
  PENDING_ENTRY = "PENDING_ENTRY"
  OPEN = "OPEN"
  PENDING_EXIT = "PENDING_EXIT"
  CLOSED = "CLOSED"


class ExitReason(str, Enum):
  STOP_LOSS = "STOP_LOSS"
  TAKE_PROFIT = "TAKE_PROFIT"
  EMERGENCY = "EMERGENCY"
  MANUAL = "MANUAL"
  CLOSED_OFFLINE = "CLOSED_OFFLINE"
  ORPHANED = "ORPHANED"


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


@dataclass
class Position:
  """In-memory active position representation with associated protective orders."""

  position_id: str
  symbol: str
  side: OrderSide
  status: PositionState
  quantity: float
  entry_price: float
  leverage: int
  stop_loss_price: float
  take_profit_price: float
  created_at: int
  updated_at: int
  protective_pair: Optional[ProtectiveOrderPair] = None
  exit_price: Optional[float] = None
  realized_pnl: Optional[float] = None
  exit_reason: Optional[str] = None
  closed_at: Optional[int] = None

  @property
  def is_active(self) -> bool:
    """Returns True if position is currently active or pending."""
    return self.status in (
        PositionState.PENDING_ENTRY,
        PositionState.OPEN,
        PositionState.PENDING_EXIT,
    )

