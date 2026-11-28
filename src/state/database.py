"""SQLite Database manager with WAL mode, foreign keys, and migration schemas."""

import os
from pathlib import Path
import sqlite3
from typing import Optional
from src.monitoring.logger import get_logger

logger = get_logger("state.database")

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS positions (
    position_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    status TEXT NOT NULL,
    quantity REAL NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL,
    leverage INTEGER NOT NULL,
    stop_loss_price REAL NOT NULL,
    take_profit_price REAL NOT NULL,
    realized_pnl REAL,
    exit_reason TEXT,
    sl_client_order_id TEXT,
    tp_client_order_id TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    closed_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol);

CREATE TABLE IF NOT EXISTS orders (
    order_id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT UNIQUE NOT NULL,
    exchange_order_id INTEGER,
    symbol TEXT NOT NULL,
    position_id TEXT,
    order_type TEXT NOT NULL,
    side TEXT NOT NULL,
    status TEXT NOT NULL,
    price REAL,
    stop_price REAL,
    orig_qty REAL NOT NULL,
    executed_qty REAL NOT NULL DEFAULT 0.0,
    avg_price REAL NOT NULL DEFAULT 0.0,
    reduce_only INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    FOREIGN KEY(position_id) REFERENCES positions(position_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_client_id ON orders(client_order_id);
CREATE INDEX IF NOT EXISTS idx_orders_position_id ON orders(position_id);

CREATE TABLE IF NOT EXISTS strategy_windows (
    window_id TEXT PRIMARY KEY,
    start_timestamp INTEGER NOT NULL,
    end_timestamp INTEGER NOT NULL,
    status TEXT NOT NULL,
    winning_symbol TEXT,
    window_consumed INTEGER NOT NULL DEFAULT 0,
    payload TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_windows_status ON strategy_windows(status);

CREATE TABLE IF NOT EXISTS audit_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    details TEXT,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);

CREATE TABLE IF NOT EXISTS live_trades (
    trade_id TEXT PRIMARY KEY,
    position_id TEXT UNIQUE,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    environment TEXT NOT NULL DEFAULT 'testnet',
    status TEXT NOT NULL,
    signal_timestamp INTEGER NOT NULL DEFAULT 0,
    entry_order_id TEXT,
    entry_timestamp INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    quantity REAL NOT NULL,
    leverage INTEGER NOT NULL,
    initial_margin REAL NOT NULL DEFAULT 0.0,
    stop_loss REAL NOT NULL,
    take_profit REAL NOT NULL,
    protective_order_ids TEXT DEFAULT '',
    exit_order_id TEXT,
    exit_timestamp INTEGER,
    exit_price REAL,
    exit_reason TEXT,
    gross_pnl REAL NOT NULL DEFAULT 0.0,
    entry_fee REAL NOT NULL DEFAULT 0.0,
    exit_fee REAL NOT NULL DEFAULT 0.0,
    funding REAL NOT NULL DEFAULT 0.0,
    net_pnl REAL NOT NULL DEFAULT 0.0,
    exchange_order_status TEXT DEFAULT '',
    execution_errors TEXT DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_live_trades_symbol ON live_trades(symbol);
CREATE INDEX IF NOT EXISTS idx_live_trades_status ON live_trades(status);
CREATE INDEX IF NOT EXISTS idx_live_trades_entry_ts ON live_trades(entry_timestamp);
"""


class DatabaseManager:
  """Manages SQLite database connections, WAL configuration, and schema migrations."""

  def __init__(self, db_path: Path | str = "data/trading_bot.db"):
    self.db_path = Path(db_path) if isinstance(db_path, str) else db_path
    self._connection: Optional[sqlite3.Connection] = None

  def initialize(self) -> None:
    """Creates parent directory, sets PRAGMA settings, and applies schema migrations."""
    if self.db_path != Path(":memory:"):
      self.db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = self.get_connection()
    conn.row_factory = sqlite3.Row
    # Configure WAL mode and performance pragmas
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.execute("PRAGMA foreign_keys = ON;")

    # Apply schema
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    logger.info("Initialized SQLite database at %s with WAL mode.", self.db_path)

  def get_connection(self) -> sqlite3.Connection:
    """Returns an active SQLite connection."""
    if self._connection is None:
      self._connection = sqlite3.connect(
          str(self.db_path),
          timeout=5.0,
          check_same_thread=False,
      )
      self._connection.row_factory = sqlite3.Row
    return self._connection

  def close(self) -> None:
    """Closes the active database connection if open."""
    if self._connection:
      try:
        self._connection.close()
      except Exception as exc:
        logger.warning("Error closing database connection: %s", exc)
      finally:
        self._connection = None
