"""State Repository for persisting positions, orders, strategy windows, and audit events."""

from dataclasses import dataclass
import json
import time
from typing import Any, Dict, List, Optional
from src.monitoring.logger import get_logger
from src.state.database import DatabaseManager

logger = get_logger("state.repository")


@dataclass
class PositionRecord:
  """Persistent record for a bot-managed position."""

  position_id: str
  symbol: str
  side: str
  status: str  # PENDING_ENTRY, OPEN, PENDING_EXIT, CLOSED
  quantity: float
  entry_price: float
  leverage: int
  stop_loss_price: float
  take_profit_price: float
  created_at: int
  updated_at: int
  exit_price: Optional[float] = None
  realized_pnl: Optional[float] = None
  exit_reason: Optional[str] = None
  sl_client_order_id: Optional[str] = None
  tp_client_order_id: Optional[str] = None
  closed_at: Optional[int] = None


@dataclass
class OrderRecord:
  """Persistent record for an exchange order."""

  client_order_id: str
  symbol: str
  order_type: str
  side: str
  status: str
  orig_qty: float
  created_at: int
  updated_at: int
  order_id: Optional[int] = None
  exchange_order_id: Optional[int] = None
  position_id: Optional[str] = None
  price: Optional[float] = None
  stop_price: Optional[float] = None
  executed_qty: float = 0.0
  avg_price: float = 0.0
  reduce_only: bool = False


class StateRepository:
  """Encapsulates SQL operations for state persistence and retrieval."""

  def __init__(self, db_manager: DatabaseManager):
    self.db = db_manager

  # --------------------------------------------------------------------------
  # Position Persistence
  # --------------------------------------------------------------------------

  def save_position(self, pos: PositionRecord) -> None:
    """Inserts or replaces a position record."""
    conn = self.db.get_connection()
    with conn:
      conn.execute(
          """
          INSERT INTO positions (
              position_id, symbol, side, status, quantity, entry_price,
              exit_price, leverage, stop_loss_price, take_profit_price,
              realized_pnl, exit_reason, sl_client_order_id, tp_client_order_id,
              created_at, updated_at, closed_at
          ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
          ON CONFLICT(position_id) DO UPDATE SET
              status = excluded.status,
              quantity = excluded.quantity,
              entry_price = excluded.entry_price,
              exit_price = excluded.exit_price,
              realized_pnl = excluded.realized_pnl,
              exit_reason = excluded.exit_reason,
              sl_client_order_id = excluded.sl_client_order_id,
              tp_client_order_id = excluded.tp_client_order_id,
              updated_at = excluded.updated_at,
              closed_at = excluded.closed_at;
          """,
          (
              pos.position_id,
              pos.symbol.upper(),
              pos.side.upper(),
              pos.status.upper(),
              pos.quantity,
              pos.entry_price,
              pos.exit_price,
              pos.leverage,
              pos.stop_loss_price,
              pos.take_profit_price,
              pos.realized_pnl,
              pos.exit_reason,
              pos.sl_client_order_id,
              pos.tp_client_order_id,
              pos.created_at,
              pos.updated_at,
              pos.closed_at,
          ),
      )
    logger.debug("Saved position %s [%s]", pos.position_id, pos.status)

  def get_position(self, position_id: str) -> Optional[PositionRecord]:
    """Retrieves a position record by ID."""
    conn = self.db.get_connection()
    cursor = conn.execute(
        "SELECT * FROM positions WHERE position_id = ?;", (position_id,)
    )
    row = cursor.fetchone()
    return self._row_to_position(row) if row else None

  def get_open_position(self) -> Optional[PositionRecord]:
    """Retrieves the currently open or pending position (at most 1)."""
    conn = self.db.get_connection()
    cursor = conn.execute(
        """
        SELECT * FROM positions
        WHERE status IN ('PENDING_ENTRY', 'OPEN', 'PENDING_EXIT')
        ORDER BY created_at DESC LIMIT 1;
        """
    )
    row = cursor.fetchone()
    return self._row_to_position(row) if row else None

  def close_position(
      self,
      position_id: str,
      exit_price: float,
      realized_pnl: float,
      exit_reason: str,
      closed_at: Optional[int] = None,
  ) -> None:
    """Transitions a position to CLOSED status with final metrics."""
    now_ms = closed_at or int(time.time() * 1000)
    conn = self.db.get_connection()
    with conn:
      conn.execute(
          """
          UPDATE positions
          SET status = 'CLOSED',
              exit_price = ?,
              realized_pnl = ?,
              exit_reason = ?,
              updated_at = ?,
              closed_at = ?
          WHERE position_id = ?;
          """,
          (exit_price, realized_pnl, exit_reason, now_ms, now_ms, position_id),
      )
    logger.info(
        "Closed position %s in database: exit_price=%.4f, pnl=%.4f (%s)",
        position_id,
        exit_price,
        realized_pnl,
        exit_reason,
    )

  def _row_to_position(self, row: Any) -> PositionRecord:
    return PositionRecord(
        position_id=row["position_id"],
        symbol=row["symbol"],
        side=row["side"],
        status=row["status"],
        quantity=float(row["quantity"]),
        entry_price=float(row["entry_price"]),
        exit_price=float(row["exit_price"]) if row["exit_price"] is not None else None,
        leverage=int(row["leverage"]),
        stop_loss_price=float(row["stop_loss_price"]),
        take_profit_price=float(row["take_profit_price"]),
        realized_pnl=float(row["realized_pnl"]) if row["realized_pnl"] is not None else None,
        exit_reason=row["exit_reason"],
        sl_client_order_id=row["sl_client_order_id"],
        tp_client_order_id=row["tp_client_order_id"],
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
        closed_at=int(row["closed_at"]) if row["closed_at"] is not None else None,
    )

  # --------------------------------------------------------------------------
  # Order Persistence
  # --------------------------------------------------------------------------

  def save_order(self, order: OrderRecord) -> None:
    """Inserts or updates an order record."""
    conn = self.db.get_connection()
    with conn:
      conn.execute(
          """
          INSERT INTO orders (
              client_order_id, exchange_order_id, symbol, position_id,
              order_type, side, status, price, stop_price, orig_qty,
              executed_qty, avg_price, reduce_only, created_at, updated_at
          ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
          ON CONFLICT(client_order_id) DO UPDATE SET
              exchange_order_id = COALESCE(excluded.exchange_order_id, orders.exchange_order_id),
              status = excluded.status,
              executed_qty = excluded.executed_qty,
              avg_price = excluded.avg_price,
              updated_at = excluded.updated_at;
          """,
          (
              order.client_order_id,
              order.exchange_order_id,
              order.symbol.upper(),
              order.position_id,
              order.order_type.upper(),
              order.side.upper(),
              order.status.upper(),
              order.price,
              order.stop_price,
              order.orig_qty,
              order.executed_qty,
              order.avg_price,
              1 if order.reduce_only else 0,
              order.created_at,
              order.updated_at,
          ),
      )

  def update_order_status(
      self,
      client_order_id: str,
      status: str,
      executed_qty: Optional[float] = None,
      avg_price: Optional[float] = None,
      exchange_order_id: Optional[int] = None,
  ) -> None:
    """Updates order status, fills, and update timestamp."""
    now_ms = int(time.time() * 1000)
    conn = self.db.get_connection()
    with conn:
      conn.execute(
          """
          UPDATE orders
          SET status = ?,
              executed_qty = COALESCE(?, executed_qty),
              avg_price = COALESCE(?, avg_price),
              exchange_order_id = COALESCE(?, exchange_order_id),
              updated_at = ?
          WHERE client_order_id = ?;
          """,
          (status.upper(), executed_qty, avg_price, exchange_order_id, now_ms, client_order_id),
      )

  def get_order_by_client_id(self, client_order_id: str) -> Optional[OrderRecord]:
    """Retrieves an order record by clientOrderId."""
    conn = self.db.get_connection()
    cursor = conn.execute(
        "SELECT * FROM orders WHERE client_order_id = ?;", (client_order_id,)
    )
    row = cursor.fetchone()
    return self._row_to_order(row) if row else None

  def get_orders_for_position(self, position_id: str) -> List[OrderRecord]:
    """Retrieves all orders linked to a position."""
    conn = self.db.get_connection()
    cursor = conn.execute(
        "SELECT * FROM orders WHERE position_id = ? ORDER BY created_at ASC;",
        (position_id,),
    )
    return [self._row_to_order(row) for row in cursor.fetchall()]

  def _row_to_order(self, row: Any) -> OrderRecord:
    return OrderRecord(
        order_id=int(row["order_id"]),
        client_order_id=row["client_order_id"],
        exchange_order_id=int(row["exchange_order_id"]) if row["exchange_order_id"] is not None else None,
        symbol=row["symbol"],
        position_id=row["position_id"],
        order_type=row["order_type"],
        side=row["side"],
        status=row["status"],
        price=float(row["price"]) if row["price"] is not None else None,
        stop_price=float(row["stop_price"]) if row["stop_price"] is not None else None,
        orig_qty=float(row["orig_qty"]),
        executed_qty=float(row["executed_qty"]),
        avg_price=float(row["avg_price"]),
        reduce_only=bool(row["reduce_only"]),
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
    )

  # --------------------------------------------------------------------------
  # Strategy Window Persistence
  # --------------------------------------------------------------------------

  def save_strategy_window(
      self,
      window_id: str,
      start_ts: int,
      end_ts: int,
      status: str,
      winning_symbol: Optional[str] = None,
      window_consumed: bool = False,
      payload_json: str = "{}",
  ) -> None:
    """Persists confirmation window state for crash recovery."""
    now_ms = int(time.time() * 1000)
    conn = self.db.get_connection()
    with conn:
      conn.execute(
          """
          INSERT INTO strategy_windows (
              window_id, start_timestamp, end_timestamp, status,
              winning_symbol, window_consumed, payload, created_at, updated_at
          ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
          ON CONFLICT(window_id) DO UPDATE SET
              status = excluded.status,
              winning_symbol = excluded.winning_symbol,
              window_consumed = excluded.window_consumed,
              payload = excluded.payload,
              updated_at = excluded.updated_at;
          """,
          (
              window_id,
              start_ts,
              end_ts,
              status,
              winning_symbol,
              1 if window_consumed else 0,
              payload_json,
              now_ms,
              now_ms,
          ),
      )

  def get_active_strategy_window(self) -> Optional[dict]:
    """Retrieves the most recent ACTIVE strategy window."""
    conn = self.db.get_connection()
    cursor = conn.execute(
        """
        SELECT * FROM strategy_windows
        WHERE status = 'ACTIVE' AND window_consumed = 0
        ORDER BY start_timestamp DESC LIMIT 1;
        """
    )
    row = cursor.fetchone()
    if not row:
      return None
    return {
        "window_id": row["window_id"],
        "start_timestamp": row["start_timestamp"],
        "end_timestamp": row["end_timestamp"],
        "status": row["status"],
        "winning_symbol": row["winning_symbol"],
        "window_consumed": bool(row["window_consumed"]),
        "payload": json.loads(row["payload"]) if row["payload"] else {},
    }

  # --------------------------------------------------------------------------
  # Audit Logging
  # --------------------------------------------------------------------------

  def log_audit(
      self, event_type: str, entity_type: str, entity_id: str, details: str = ""
  ) -> None:
    """Appends an event to the immutable audit log."""
    now_ms = int(time.time() * 1000)
    conn = self.db.get_connection()
    with conn:
      conn.execute(
          """
          INSERT INTO audit_log (event_type, entity_type, entity_id, details, created_at)
          VALUES (?, ?, ?, ?, ?);
          """,
          (event_type, entity_type, entity_id, details, now_ms),
      )
    logger.debug("Audit [%s] on %s:%s - %s", event_type, entity_type, entity_id, details)
