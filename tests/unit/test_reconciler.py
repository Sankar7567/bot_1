"""Unit tests for Phase 11 — Startup State Reconciliation.

Validates the complete 6-case reconciliation matrix:
- Case 1: Clean startup (Exchange 0, DB 0, Orders 0 -> CLEAN)
- Case 2: Matching open position with verified protection -> POSITION_RECOVERED
- Case 3: Orphaned orders on exchange with 0 positions -> ORPHANED_ORDERS_CLEARED
- Case 4: Unprotected open position on exchange -> UNPROTECTED_EMERGENCY_CLOSE
- Case 5: Position closed on exchange while offline -> CLOSED_WHILE_OFFLINE
- Case 6: Multiple open positions on exchange -> ReconciliationError (Fatal)
"""

from pathlib import Path
import time
from unittest.mock import AsyncMock, MagicMock
import pytest
from src.exchange.client import BinanceFuturesClient
from src.exchange.models import OpenOrder, PositionRisk
from src.positions.manager import PositionManager
from src.positions.models import EmergencyExitResult, PositionState
from src.positions.protective import ProtectiveOrderManager
from src.state.database import DatabaseManager
from src.state.reconciler import (
    ReconciliationError,
    ReconciliationStatus,
    StartupReconciler,
)
from src.state.repository import PositionRecord, StateRepository


@pytest.fixture
def db_manager(tmp_path: Path):
  mgr = DatabaseManager(tmp_path / "test_reconciler.db")
  mgr.initialize()
  yield mgr
  mgr.close()


@pytest.fixture
def repository(db_manager: DatabaseManager):
  return StateRepository(db_manager)


@pytest.fixture
def mock_client():
  client = MagicMock(spec=BinanceFuturesClient)
  client.get_positions = AsyncMock(return_value=[])
  client.get_open_orders = AsyncMock(return_value=[])
  client.cancel_order = AsyncMock()
  return client


@pytest.fixture
def mock_protective():
  pm = MagicMock(spec=ProtectiveOrderManager)
  pm._active_pairs = {}
  pm._lock = AsyncMock()
  pm.execute_emergency_market_close = AsyncMock()
  return pm


@pytest.fixture
def mock_position_manager():
  pm = MagicMock(spec=PositionManager)
  pm.set_current_position = MagicMock()
  return pm


@pytest.fixture
def reconciler(mock_client, repository, mock_protective, mock_position_manager):
  return StartupReconciler(
      exchange_client=mock_client,
      repository=repository,
      protective_manager=mock_protective,
      position_manager=mock_position_manager,
  )


class TestStartupReconciliationMatrix:
  """Tests verifying each branch of the startup reconciliation matrix."""

  @pytest.mark.asyncio
  async def test_case_1_clean_startup(
      self, reconciler: StartupReconciler, mock_client, repository
  ):
    mock_client.get_positions.return_value = []
    mock_client.get_open_orders.return_value = []

    report = await reconciler.reconcile_startup_state()

    assert report.status == ReconciliationStatus.CLEAN
    assert "Clean startup" in report.details
    assert report.recovered_position is None

  @pytest.mark.asyncio
  async def test_case_2_matching_open_position_recovered(
      self,
      reconciler: StartupReconciler,
      mock_client,
      repository,
      mock_protective,
      mock_position_manager,
  ):
    # Exchange has 1 open BTCUSDT position
    mock_client.get_positions.return_value = [
        PositionRisk(
            symbol="BTCUSDT",
            position_amt=0.5,
            entry_price=50000.0,
            mark_price=50200.0,
            unrealized_profit=100.0,
            liquidation_price=45000.0,
            leverage=10,
            margin_type="isolated",
            position_side="BOTH",
        )
    ]
    # Exchange has active SL and TP orders
    mock_client.get_open_orders.return_value = [
        OpenOrder(
            symbol="BTCUSDT",
            order_id=101,
            client_order_id="HA_SL_1711234567000_1234",
            price=0.0,
            orig_qty=0.5,
            executed_qty=0.0,
            status="NEW",
            order_type="STOP_MARKET",
            side="SELL",
            time_in_force="GTC",
            stop_price=49000.0,
            reduce_only=True,
            update_time=int(time.time() * 1000),
        ),
        OpenOrder(
            symbol="BTCUSDT",
            order_id=102,
            client_order_id="HA_TP_1711234567000_1234",
            price=0.0,
            orig_qty=0.5,
            executed_qty=0.0,
            status="NEW",
            order_type="TAKE_PROFIT_MARKET",
            side="SELL",
            time_in_force="GTC",
            stop_price=53000.0,
            reduce_only=True,
            update_time=int(time.time() * 1000),
        ),
    ]

    # Local DB has matching open position
    now = int(time.time() * 1000)
    pos_rec = PositionRecord(
        position_id="pos_reconcile_match",
        symbol="BTCUSDT",
        side="BUY",
        status="OPEN",
        quantity=0.5,
        entry_price=50000.0,
        leverage=10,
        stop_loss_price=49000.0,
        take_profit_price=53000.0,
        created_at=now - 10000,
        updated_at=now - 10000,
    )
    repository.save_position(pos_rec)

    report = await reconciler.reconcile_startup_state()

    assert report.status == ReconciliationStatus.POSITION_RECOVERED
    assert report.recovered_position is not None
    assert report.recovered_position.symbol == "BTCUSDT"
    assert report.recovered_position.quantity == 0.5
    assert report.recovered_position.stop_loss_price == 49000.0
    assert report.recovered_position.take_profit_price == 53000.0

    # Ensure position manager and protective manager rehydrated
    mock_position_manager.set_current_position.assert_called_once()
    assert "BTCUSDT" in mock_protective._active_pairs

  @pytest.mark.asyncio
  async def test_case_3_orphaned_orders_cleared(
      self, reconciler: StartupReconciler, mock_client
  ):
    mock_client.get_positions.return_value = []
    # Orphaned orders
    mock_client.get_open_orders.return_value = [
        OpenOrder(
            symbol="ETHUSDT",
            order_id=201,
            client_order_id="HA_SL_1711234567000_abcd",
            price=0.0,
            orig_qty=1.0,
            executed_qty=0.0,
            status="NEW",
            order_type="STOP_MARKET",
            side="SELL",
            time_in_force="GTC",
            stop_price=3000.0,
            reduce_only=True,
            update_time=int(time.time() * 1000),
        ),
        OpenOrder(
            symbol="ETHUSDT",
            order_id=202,
            client_order_id="HA_TP_1711234567000_abcd",
            price=0.0,
            orig_qty=1.0,
            executed_qty=0.0,
            status="NEW",
            order_type="TAKE_PROFIT_MARKET",
            side="SELL",
            time_in_force="GTC",
            stop_price=3500.0,
            reduce_only=True,
            update_time=int(time.time() * 1000),
        ),
    ]

    report = await reconciler.reconcile_startup_state()

    assert report.status == ReconciliationStatus.ORPHANED_ORDERS_CLEARED
    assert len(report.cancelled_order_ids) == 2
    assert mock_client.cancel_order.call_count == 2

  @pytest.mark.asyncio
  async def test_case_4_unprotected_open_position_emergency_close(
      self, reconciler: StartupReconciler, mock_client, mock_protective, repository
  ):
    # Exchange has 1 open position
    mock_client.get_positions.return_value = [
        PositionRisk(
            symbol="SOLUSDT",
            position_amt=10.0,
            entry_price=150.0,
            mark_price=151.0,
            unrealized_profit=10.0,
            liquidation_price=135.0,
            leverage=10,
            margin_type="isolated",
            position_side="BOTH",
        )
    ]
    # But ONLY SL exists, TP is missing!
    mock_client.get_open_orders.return_value = [
        OpenOrder(
            symbol="SOLUSDT",
            order_id=301,
            client_order_id="HA_SL_1711234567000_ef01",
            price=0.0,
            orig_qty=10.0,
            executed_qty=0.0,
            status="NEW",
            order_type="STOP_MARKET",
            side="SELL",
            time_in_force="GTC",
            stop_price=140.0,
            reduce_only=True,
            update_time=int(time.time() * 1000),
        )
    ]

    mock_protective.execute_emergency_market_close.return_value = EmergencyExitResult(
        symbol="SOLUSDT",
        emergency_client_order_id="HA_EMG_123",
        quantity=10.0,
        is_closed=True,
    )

    report = await reconciler.reconcile_startup_state()

    assert report.status == ReconciliationStatus.UNPROTECTED_EMERGENCY_CLOSE
    assert report.emergency_closed
    mock_protective.execute_emergency_market_close.assert_called_once()
    # Cancelled the partial SL order
    assert "HA_SL_1711234567000_ef01" in report.cancelled_order_ids

  @pytest.mark.asyncio
  async def test_case_5_position_closed_while_offline(
      self, reconciler: StartupReconciler, mock_client, repository
  ):
    # Exchange has 0 positions
    mock_client.get_positions.return_value = []
    # Exchange has 1 leftover order
    mock_client.get_open_orders.return_value = [
        OpenOrder(
            symbol="BNBUSDT",
            order_id=401,
            client_order_id="HA_TP_1711234567000_2222",
            price=0.0,
            orig_qty=1.0,
            executed_qty=0.0,
            status="NEW",
            order_type="TAKE_PROFIT_MARKET",
            side="SELL",
            time_in_force="GTC",
            stop_price=600.0,
            reduce_only=True,
            update_time=int(time.time() * 1000),
        )
    ]

    # Local DB has open position
    now = int(time.time() * 1000)
    pos_rec = PositionRecord(
        position_id="pos_offline_close",
        symbol="BNBUSDT",
        side="BUY",
        status="OPEN",
        quantity=1.0,
        entry_price=550.0,
        leverage=10,
        stop_loss_price=530.0,
        take_profit_price=600.0,
        created_at=now - 50000,
        updated_at=now - 50000,
    )
    repository.save_position(pos_rec)

    report = await reconciler.reconcile_startup_state()

    assert report.status == ReconciliationStatus.CLOSED_WHILE_OFFLINE
    assert "closed while offline" in report.details
    assert "HA_TP_1711234567000_2222" in report.cancelled_order_ids

    # Local DB must now be marked CLOSED
    closed_pos = repository.get_position("pos_offline_close")
    assert closed_pos.status == "CLOSED"
    assert closed_pos.exit_reason == "CLOSED_WHILE_OFFLINE"

  @pytest.mark.asyncio
  async def test_case_6_multiple_open_positions_raises_error(
      self, reconciler: StartupReconciler, mock_client
  ):
    # Exchange has 2 open positions (violates system invariant)
    mock_client.get_positions.return_value = [
        PositionRisk(
            symbol="BTCUSDT",
            position_amt=0.5,
            entry_price=50000.0,
            mark_price=50100.0,
            unrealized_profit=50.0,
            liquidation_price=45000.0,
            leverage=10,
            margin_type="isolated",
            position_side="BOTH",
        ),
        PositionRisk(
            symbol="ETHUSDT",
            position_amt=2.0,
            entry_price=3000.0,
            mark_price=3050.0,
            unrealized_profit=100.0,
            liquidation_price=2700.0,
            leverage=10,
            margin_type="isolated",
            position_side="BOTH",
        ),
    ]

    with pytest.raises(ReconciliationError, match="multiple open positions"):
      await reconciler.reconcile_startup_state()
