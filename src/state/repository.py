"""State Repository for persisting positions, orders, strategy windows, and audit events."""

from dataclasses import asdict, dataclass
import csv
import io
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


@dataclass
class LiveTradeRecord:
  """Permanent SQLite-backed ledger record for a live/testnet trade (Section 8)."""

  trade_id: str
  symbol: str
  direction: str
  status: str  # OPEN | CLOSED | ERROR
  entry_timestamp: int
  entry_price: float
  quantity: float
  leverage: int
  stop_loss: float
  take_profit: float
  created_at: int
  updated_at: int
  position_id: Optional[str] = None
  environment: str = "testnet"
  signal_timestamp: int = 0
  entry_order_id: Optional[str] = None
  initial_margin: float = 0.0
  protective_order_ids: str = ""
  exit_order_id: Optional[str] = None
  exit_timestamp: Optional[int] = None
  exit_price: Optional[float] = None
  exit_reason: Optional[str] = None
  gross_pnl: float = 0.0
  entry_fee: float = 0.0
  exit_fee: float = 0.0
  funding: float = 0.0
  net_pnl: float = 0.0
  exchange_order_status: str = ""
  execution_errors: str = ""

  def to_dict(self) -> Dict[str, Any]:
    return asdict(self)


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

  # --------------------------------------------------------------------------
  # Permanent Live Trade Ledger (Section 8)
  # --------------------------------------------------------------------------

  def save_live_trade(self, trade: LiveTradeRecord) -> None:
    """Inserts or updates a live trade record without deleting history."""
    conn = self.db.get_connection()
    with conn:
      conn.execute(
          """
          INSERT INTO live_trades (
              trade_id, position_id, symbol, direction, environment, status,
              signal_timestamp, entry_order_id, entry_timestamp, entry_price,
              quantity, leverage, initial_margin, stop_loss, take_profit,
              protective_order_ids, exit_order_id, exit_timestamp, exit_price,
              exit_reason, gross_pnl, entry_fee, exit_fee, funding, net_pnl,
              exchange_order_status, execution_errors, created_at, updated_at
          ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
          ON CONFLICT(trade_id) DO UPDATE SET
              status = excluded.status,
              protective_order_ids = CASE
                  WHEN excluded.protective_order_ids != '' THEN excluded.protective_order_ids
                  ELSE live_trades.protective_order_ids
              END,
              exit_order_id = COALESCE(excluded.exit_order_id, live_trades.exit_order_id),
              exit_timestamp = COALESCE(excluded.exit_timestamp, live_trades.exit_timestamp),
              exit_price = COALESCE(excluded.exit_price, live_trades.exit_price),
              exit_reason = COALESCE(excluded.exit_reason, live_trades.exit_reason),
              gross_pnl = excluded.gross_pnl,
              entry_fee = excluded.entry_fee,
              exit_fee = excluded.exit_fee,
              funding = excluded.funding,
              net_pnl = excluded.net_pnl,
              exchange_order_status = CASE
                  WHEN excluded.exchange_order_status != '' THEN excluded.exchange_order_status
                  ELSE live_trades.exchange_order_status
              END,
              execution_errors = CASE
                  WHEN excluded.execution_errors != '' THEN excluded.execution_errors
                  ELSE live_trades.execution_errors
              END,
              updated_at = excluded.updated_at;
          """,
          (
              trade.trade_id,
              trade.position_id,
              trade.symbol.upper(),
              trade.direction.upper(),
              trade.environment,
              trade.status.upper(),
              trade.signal_timestamp,
              trade.entry_order_id,
              trade.entry_timestamp,
              trade.entry_price,
              trade.quantity,
              trade.leverage,
              trade.initial_margin,
              trade.stop_loss,
              trade.take_profit,
              trade.protective_order_ids,
              trade.exit_order_id,
              trade.exit_timestamp,
              trade.exit_price,
              trade.exit_reason,
              trade.gross_pnl,
              trade.entry_fee,
              trade.exit_fee,
              trade.funding,
              trade.net_pnl,
              trade.exchange_order_status,
              trade.execution_errors,
              trade.created_at,
              trade.updated_at,
          ),
      )

  def get_live_trade(self, trade_id: str) -> Optional[LiveTradeRecord]:
    conn = self.db.get_connection()
    row = conn.execute(
        "SELECT * FROM live_trades WHERE trade_id = ?;", (trade_id,)
    ).fetchone()
    return self._row_to_live_trade(row) if row else None

  def get_live_trade_by_position(self, position_id: str) -> Optional[LiveTradeRecord]:
    conn = self.db.get_connection()
    row = conn.execute(
        "SELECT * FROM live_trades WHERE position_id = ?;", (position_id,)
    ).fetchone()
    return self._row_to_live_trade(row) if row else None

  def list_live_trades(
      self,
      symbol: Optional[str] = None,
      direction: Optional[str] = None,
      status: Optional[str] = None,
      limit: int = 500,
  ) -> List[LiveTradeRecord]:
    conn = self.db.get_connection()
    clauses: List[str] = []
    params: List[Any] = []
    if symbol:
      clauses.append("symbol = ?")
      params.append(symbol.upper())
    if direction:
      clauses.append("direction = ?")
      params.append(direction.upper())
    if status:
      clauses.append("status = ?")
      params.append(status.upper())

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    rows = conn.execute(
        f"SELECT * FROM live_trades {where} ORDER BY entry_timestamp DESC LIMIT ?;",
        params,
    ).fetchall()
    return [self._row_to_live_trade(r) for r in rows]

  def sync_live_trades_from_positions(self, environment: str = "testnet") -> int:
    """Backfills/reconciles any positions not yet present in live_trades without overwriting."""
    conn = self.db.get_connection()
    rows = conn.execute(
        """
        SELECT p.* FROM positions p
        LEFT JOIN live_trades lt ON lt.position_id = p.position_id
        WHERE lt.position_id IS NULL;
        """
    ).fetchall()
    synced = 0
    for row in rows:
      pos = self._row_to_position(row)
      orders = self.get_orders_for_position(pos.position_id)
      entry_ord = next(
          (o for o in orders if o.order_type == "MARKET" and not o.reduce_only),
          None,
      )
      prot_ids = ",".join(
          filter(None, [pos.sl_client_order_id, pos.tp_client_order_id])
      )
      exit_ord = next(
          (o for o in orders if o.reduce_only and o.status == "FILLED"),
          None,
      )
      direction = "LONG" if pos.side == "BUY" else "SHORT"
      initial_margin = (
          (pos.quantity * pos.entry_price) / pos.leverage
          if pos.leverage > 0
          else 0.0
      )
      gross = pos.realized_pnl or 0.0
      trade = LiveTradeRecord(
          trade_id=f"lt_{pos.position_id}",
          position_id=pos.position_id,
          symbol=pos.symbol,
          direction=direction,
          environment=environment,
          status="CLOSED" if pos.status == "CLOSED" else "OPEN",
          signal_timestamp=pos.created_at,
          entry_order_id=entry_ord.client_order_id if entry_ord else None,
          entry_timestamp=pos.created_at,
          entry_price=pos.entry_price,
          quantity=pos.quantity,
          leverage=pos.leverage,
          initial_margin=initial_margin,
          stop_loss=pos.stop_loss_price,
          take_profit=pos.take_profit_price,
          protective_order_ids=prot_ids,
          exit_order_id=exit_ord.client_order_id if exit_ord else None,
          exit_timestamp=pos.closed_at,
          exit_price=pos.exit_price,
          exit_reason=pos.exit_reason,
          gross_pnl=gross,
          net_pnl=gross,
          exchange_order_status=exit_ord.status if exit_ord else (entry_ord.status if entry_ord else pos.status),
          created_at=pos.created_at,
          updated_at=pos.updated_at,
      )
      self.save_live_trade(trade)
      synced += 1
    return synced

  def upsert_live_trade_from_position(
      self,
      position_id: str,
      environment: str = "testnet",
      signal_timestamp: Optional[int] = None,
      execution_error: str = "",
  ) -> Optional[LiveTradeRecord]:
    """Creates or updates a live_trades row from the current positions & orders state."""
    pos = self.get_position(position_id)
    if pos is None:
      return None

    orders = self.get_orders_for_position(pos.position_id)
    entry_ord = next(
        (o for o in orders if o.order_type == "MARKET" and not o.reduce_only),
        None,
    )
    prot_ids = ",".join(
        filter(None, [pos.sl_client_order_id, pos.tp_client_order_id])
    )
    exit_ord = next(
        (o for o in orders if o.reduce_only and o.status == "FILLED"),
        None,
    )
    existing = self.get_live_trade_by_position(pos.position_id)
    sig_ts = (
        signal_timestamp
        if signal_timestamp is not None
        else (existing.signal_timestamp if existing else pos.created_at)
    )
    direction = "LONG" if pos.side == "BUY" else "SHORT"
    initial_margin = (
        (pos.quantity * pos.entry_price) / pos.leverage
        if pos.leverage > 0
        else 0.0
    )
    gross = pos.realized_pnl or 0.0
    trade = LiveTradeRecord(
        trade_id=existing.trade_id if existing else f"lt_{pos.position_id}",
        position_id=pos.position_id,
        symbol=pos.symbol,
        direction=direction,
        environment=existing.environment if existing else environment,
        status="CLOSED" if pos.status == "CLOSED" else ("ERROR" if execution_error else "OPEN"),
        signal_timestamp=sig_ts,
        entry_order_id=entry_ord.client_order_id if entry_ord else None,
        entry_timestamp=pos.created_at,
        entry_price=pos.entry_price,
        quantity=pos.quantity,
        leverage=pos.leverage,
        initial_margin=initial_margin,
        stop_loss=pos.stop_loss_price,
        take_profit=pos.take_profit_price,
        protective_order_ids=prot_ids,
        exit_order_id=exit_ord.client_order_id if exit_ord else None,
        exit_timestamp=pos.closed_at,
        exit_price=pos.exit_price,
        exit_reason=pos.exit_reason,
        gross_pnl=gross,
        entry_fee=existing.entry_fee if existing else 0.0,
        exit_fee=existing.exit_fee if existing else 0.0,
        funding=existing.funding if existing else 0.0,
        net_pnl=gross - (existing.entry_fee if existing else 0.0) - (existing.exit_fee if existing else 0.0) - (existing.funding if existing else 0.0),
        exchange_order_status=exit_ord.status if exit_ord else (entry_ord.status if entry_ord else pos.status),
        execution_errors=execution_error or (existing.execution_errors if existing else ""),
        created_at=pos.created_at,
        updated_at=pos.updated_at,
    )
    self.save_live_trade(trade)
    return trade

  def export_live_trades_csv(self) -> str:
    """Exports all live trade ledger records to CSV."""
    trades = self.list_live_trades(limit=10_000)
    output = io.StringIO()
    fieldnames = [
        "trade_id",
        "position_id",
        "symbol",
        "direction",
        "environment",
        "status",
        "signal_timestamp",
        "entry_order_id",
        "entry_timestamp",
        "entry_price",
        "quantity",
        "leverage",
        "initial_margin",
        "stop_loss",
        "take_profit",
        "protective_order_ids",
        "exit_order_id",
        "exit_timestamp",
        "exit_price",
        "exit_reason",
        "gross_pnl",
        "entry_fee",
        "exit_fee",
        "funding",
        "net_pnl",
        "exchange_order_status",
        "execution_errors",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for t in trades:
      writer.writerow(t.to_dict())
    return output.getvalue()

  def _row_to_live_trade(self, row: Any) -> LiveTradeRecord:
    return LiveTradeRecord(
        trade_id=row["trade_id"],
        position_id=row["position_id"],
        symbol=row["symbol"],
        direction=row["direction"],
        environment=row["environment"],
        status=row["status"],
        signal_timestamp=int(row["signal_timestamp"]),
        entry_order_id=row["entry_order_id"],
        entry_timestamp=int(row["entry_timestamp"]),
        entry_price=float(row["entry_price"]),
        quantity=float(row["quantity"]),
        leverage=int(row["leverage"]),
        initial_margin=float(row["initial_margin"]),
        stop_loss=float(row["stop_loss"]),
        take_profit=float(row["take_profit"]),
        protective_order_ids=row["protective_order_ids"] or "",
        exit_order_id=row["exit_order_id"],
        exit_timestamp=int(row["exit_timestamp"]) if row["exit_timestamp"] is not None else None,
        exit_price=float(row["exit_price"]) if row["exit_price"] is not None else None,
        exit_reason=row["exit_reason"],
        gross_pnl=float(row["gross_pnl"]),
        entry_fee=float(row["entry_fee"]),
        exit_fee=float(row["exit_fee"]),
        funding=float(row["funding"]),
        net_pnl=float(row["net_pnl"]),
        exchange_order_status=row["exchange_order_status"] or "",
        execution_errors=row["execution_errors"] or "",
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
    )
