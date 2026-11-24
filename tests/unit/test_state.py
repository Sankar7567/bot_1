"""Unit tests for Phase 11 — SQLite State Persistence & Repository.

Validates:
1. SQLite WAL mode initialization and database schema creation.
2. Position CRUD operations, lifecycle status tracking, and single-open retrieval.
3. Order CRUD operations, status updates, and position association.
4. Strategy window persistence, retrieval, and consumption tracking.
5. Immutable audit log recording and querying.
"""

from pathlib import Path
import time
import pytest
from src.state.database import DatabaseManager
from src.state.repository import (
    OrderRecord,
    PositionRecord,
    StateRepository,
)


@pytest.fixture
def db_manager(tmp_path: Path):
  db_file = tmp_path / "test_trading.db"
  mgr = DatabaseManager(db_file)
  mgr.initialize()
  yield mgr
  mgr.close()


@pytest.fixture
def repository(db_manager: DatabaseManager):
  return StateRepository(db_manager)


class TestDatabaseManager:
  """Tests for SQLite database connection and PRAGMA settings."""

  def test_initialize_database_file_and_pragmas(self, db_manager: DatabaseManager):
    conn = db_manager.get_connection()
    # Check journal mode (WAL mode)
    cur = conn.execute("PRAGMA journal_mode;")
    mode = cur.fetchone()[0]
    assert mode.upper() == "WAL"

    # Check foreign keys
    cur = conn.execute("PRAGMA foreign_keys;")
    fk = cur.fetchone()[0]
    assert fk == 1

  def test_schema_tables_exist(self, db_manager: DatabaseManager):
    conn = db_manager.get_connection()
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
    )
    tables = [row[0] for row in cur.fetchall()]
    assert "positions" in tables
    assert "orders" in tables
    assert "strategy_windows" in tables
    assert "audit_log" in tables


class TestPositionRepository:
  """Tests for position persistence and lifecycle operations."""

  def test_save_and_get_position(self, repository: StateRepository):
    now = int(time.time() * 1000)
    pos = PositionRecord(
        position_id="pos_BTCUSDT_001",
        symbol="BTCUSDT",
        side="BUY",
        status="PENDING_ENTRY",
        quantity=0.15,
        entry_price=50000.0,
        leverage=10,
        stop_loss_price=49000.0,
        take_profit_price=53000.0,
        created_at=now,
        updated_at=now,
    )
    repository.save_position(pos)

    retrieved = repository.get_position("pos_BTCUSDT_001")
    assert retrieved is not None
    assert retrieved.position_id == "pos_BTCUSDT_001"
    assert retrieved.symbol == "BTCUSDT"
    assert retrieved.side == "BUY"
    assert retrieved.status == "PENDING_ENTRY"
    assert retrieved.quantity == 0.15
    assert retrieved.entry_price == 50000.0
    assert retrieved.leverage == 10

  def test_get_open_position_filters(self, repository: StateRepository):
    now = int(time.time() * 1000)
    # 1. Closed position
    p_closed = PositionRecord(
        position_id="pos_old_closed",
        symbol="ETHUSDT",
        side="BUY",
        status="CLOSED",
        quantity=1.0,
        entry_price=3000.0,
        leverage=5,
        stop_loss_price=2900.0,
        take_profit_price=3300.0,
        created_at=now - 5000,
        updated_at=now - 2000,
        closed_at=now - 2000,
    )
    repository.save_position(p_closed)
    assert repository.get_open_position() is None

    # 2. Open position
    p_open = PositionRecord(
        position_id="pos_active_001",
        symbol="SOLUSDT",
        side="BUY",
        status="OPEN",
        quantity=5.0,
        entry_price=150.0,
        leverage=10,
        stop_loss_price=145.0,
        take_profit_price=165.0,
        created_at=now,
        updated_at=now,
        sl_client_order_id="HA_SL_123",
        tp_client_order_id="HA_TP_456",
    )
    repository.save_position(p_open)

    active = repository.get_open_position()
    assert active is not None
    assert active.position_id == "pos_active_001"
    assert active.status == "OPEN"
    assert active.sl_client_order_id == "HA_SL_123"

  def test_close_position(self, repository: StateRepository):
    now = int(time.time() * 1000)
    pos = PositionRecord(
        position_id="pos_to_close",
        symbol="BNBUSDT",
        side="BUY",
        status="OPEN",
        quantity=2.0,
        entry_price=600.0,
        leverage=10,
        stop_loss_price=580.0,
        take_profit_price=660.0,
        created_at=now,
        updated_at=now,
    )
    repository.save_position(pos)

    # Close position
    repository.close_position(
        position_id="pos_to_close",
        exit_price=660.0,
        realized_pnl=120.0,
        exit_reason="TAKE_PROFIT",
    )

    retrieved = repository.get_position("pos_to_close")
    assert retrieved is not None
    assert retrieved.status == "CLOSED"
    assert retrieved.exit_price == 660.0
    assert retrieved.realized_pnl == 120.0
    assert retrieved.exit_reason == "TAKE_PROFIT"
    assert retrieved.closed_at is not None

    # Should no longer appear in get_open_position
    assert repository.get_open_position() is None


class TestOrderRepository:
  """Tests for order persistence, tracking, and position linking."""

  def test_save_and_get_order(self, repository: StateRepository):
    now = int(time.time() * 1000)
    pos = PositionRecord(
        position_id="pos_BTC_001",
        symbol="BTCUSDT",
        side="BUY",
        status="OPEN",
        quantity=0.5,
        entry_price=50000.0,
        leverage=10,
        stop_loss_price=49000.0,
        take_profit_price=53000.0,
        created_at=now,
        updated_at=now,
    )
    repository.save_position(pos)

    order = OrderRecord(
        client_order_id="HA_E_1711234567000_1234",
        symbol="BTCUSDT",
        order_type="MARKET",
        side="BUY",
        status="NEW",
        orig_qty=0.5,
        created_at=now,
        updated_at=now,
        position_id="pos_BTC_001",
    )
    repository.save_order(order)

    retrieved = repository.get_order_by_client_id("HA_E_1711234567000_1234")
    assert retrieved is not None
    assert retrieved.client_order_id == "HA_E_1711234567000_1234"
    assert retrieved.symbol == "BTCUSDT"
    assert retrieved.position_id == "pos_BTC_001"
    assert retrieved.status == "NEW"

  def test_update_order_status(self, repository: StateRepository):
    now = int(time.time() * 1000)
    order = OrderRecord(
        client_order_id="HA_SL_9999",
        symbol="BTCUSDT",
        order_type="STOP_MARKET",
        side="SELL",
        status="NEW",
        orig_qty=0.5,
        stop_price=49000.0,
        reduce_only=True,
        created_at=now,
        updated_at=now,
    )
    repository.save_order(order)

    # Update to FILLED
    repository.update_order_status(
        client_order_id="HA_SL_9999",
        status="FILLED",
        executed_qty=0.5,
        avg_price=48995.0,
        exchange_order_id=987654321,
    )

    retrieved = repository.get_order_by_client_id("HA_SL_9999")
    assert retrieved is not None
    assert retrieved.status == "FILLED"
    assert retrieved.executed_qty == 0.5
    assert retrieved.avg_price == 48995.0
    assert retrieved.exchange_order_id == 987654321

  def test_get_orders_for_position(self, repository: StateRepository):
    now = int(time.time() * 1000)
    pos_id = "pos_multi_orders"

    pos = PositionRecord(
        position_id=pos_id,
        symbol="ETHUSDT",
        side="BUY",
        status="OPEN",
        quantity=1.0,
        entry_price=3000.0,
        leverage=10,
        stop_loss_price=2900.0,
        take_profit_price=3300.0,
        created_at=now,
        updated_at=now,
    )
    repository.save_position(pos)

    o1 = OrderRecord(
        client_order_id="HA_E_1",
        symbol="ETHUSDT",
        order_type="MARKET",
        side="BUY",
        status="FILLED",
        orig_qty=1.0,
        position_id=pos_id,
        created_at=now,
        updated_at=now,
    )
    o2 = OrderRecord(
        client_order_id="HA_SL_1",
        symbol="ETHUSDT",
        order_type="STOP_MARKET",
        side="SELL",
        status="NEW",
        orig_qty=1.0,
        position_id=pos_id,
        created_at=now + 10,
        updated_at=now + 10,
    )
    repository.save_order(o1)
    repository.save_order(o2)

    pos_orders = repository.get_orders_for_position(pos_id)
    assert len(pos_orders) == 2
    assert [o.client_order_id for o in pos_orders] == ["HA_E_1", "HA_SL_1"]


class TestStrategyWindowRepository:
  """Tests for strategy confirmation window state persistence."""

  def test_save_and_get_active_strategy_window(self, repository: StateRepository):
    now = int(time.time() * 1000)
    repository.save_strategy_window(
        window_id="win_15m_100",
        start_ts=now,
        end_ts=now + 900000,
        status="ACTIVE",
        payload_json='{"candidates": ["BTCUSDT", "ETHUSDT"]}',
    )

    win = repository.get_active_strategy_window()
    assert win is not None
    assert win["window_id"] == "win_15m_100"
    assert win["status"] == "ACTIVE"
    assert not win["window_consumed"]
    assert win["payload"]["candidates"] == ["BTCUSDT", "ETHUSDT"]

  def test_consumed_window_not_returned_as_active(self, repository: StateRepository):
    now = int(time.time() * 1000)
    repository.save_strategy_window(
        window_id="win_15m_consumed",
        start_ts=now,
        end_ts=now + 900000,
        status="ACTIVE",
        window_consumed=True,
    )
    assert repository.get_active_strategy_window() is None


class TestAuditLogRepository:
  """Tests for audit trail recording."""

  def test_log_audit_records_event(
      self, repository: StateRepository, db_manager: DatabaseManager
  ):
    repository.log_audit(
        event_type="TEST_EVENT",
        entity_type="ORDER",
        entity_id="HA_E_001",
        details="Order placed successfully",
    )

    conn = db_manager.get_connection()
    cur = conn.execute(
        "SELECT * FROM audit_log WHERE entity_id = 'HA_E_001';"
    )
    row = cur.fetchone()
    assert row is not None
    assert row["event_type"] == "TEST_EVENT"
    assert row["entity_type"] == "ORDER"
    assert row["details"] == "Order placed successfully"
