"""Unit tests for Phase 11 — Position Manager & Single-Position Enforcement.

Validates:
1. Global 1-position lock enforcement (rejects concurrent entries).
2. Complete entry lifecycle: market entry -> actual fill -> protective orders placement -> OPEN state.
3. Database persistence of positions and orders during entry.
4. Entry failure handling and database status transitions.
5. Protection failure handling and fail-safe emergency close tracking.
6. Exit fill processing: atomic sibling cancellation, PnL calculation, CLOSED status in DB.
7. Manual/emergency position closure.
"""

from pathlib import Path
import time
from unittest.mock import AsyncMock, MagicMock
import pytest
from src.exchange.client import BinanceFuturesClient
from src.execution.exceptions import ExecutionError, ProtectiveOrderError
from src.execution.models import OrderFill, OrderSide, OrderStatus, OrderType
from src.execution.order_router import OrderRouter
from src.positions.exceptions import PositionLockError, PositionNotFoundError
from src.positions.manager import PositionManager
from src.positions.models import (
    EmergencyExitResult,
    ExitReason,
    ExitTriggerResult,
    PositionState,
    ProtectiveOrderPair,
)
from src.positions.protective import ProtectiveOrderManager
from src.risk.models import SizingResult
from src.state.database import DatabaseManager
from src.state.repository import StateRepository
from src.strategy.models import Direction, TradeSignal


@pytest.fixture
def db_manager(tmp_path: Path):
  mgr = DatabaseManager(tmp_path / "test_positions.db")
  mgr.initialize()
  yield mgr
  mgr.close()


@pytest.fixture
def repository(db_manager: DatabaseManager):
  return StateRepository(db_manager)


@pytest.fixture
def mock_client():
  client = MagicMock(spec=BinanceFuturesClient)
  client.place_order = AsyncMock()
  client.query_order = AsyncMock()
  client.cancel_order = AsyncMock()
  client.get_positions = AsyncMock(return_value=[])
  return client


@pytest.fixture
def mock_router():
  router = MagicMock(spec=OrderRouter)
  router.execute_market_entry = AsyncMock()
  router.cancel_order = AsyncMock()
  return router


@pytest.fixture
def mock_protective():
  pm = MagicMock(spec=ProtectiveOrderManager)
  pm.place_protective_orders = AsyncMock()
  pm.cancel_sibling_order = AsyncMock()
  pm.execute_emergency_market_close = AsyncMock()
  pm.get_active_pair = MagicMock(return_value=None)
  pm._lock = AsyncMock()
  pm._active_pairs = {}
  return pm


@pytest.fixture
def position_manager(mock_client, repository, mock_router, mock_protective):
  return PositionManager(
      exchange_client=mock_client,
      repository=repository,
      order_router=mock_router,
      protective_manager=mock_protective,
  )


def make_signal(symbol: str = "BTCUSDT", direction: Direction = Direction.LONG) -> TradeSignal:
  now = int(time.time() * 1000)
  return TradeSignal(
      signal_id=f"sig_{symbol}",
      window_id="win_1",
      symbol=symbol,
      direction=direction,
      trigger_15m_timestamp=now - 60000,
      confirmation_1m_timestamp=now,
      trigger_price=50000.0,
      confirmation_price=50100.0,
      candidate_rank=1,
      created_at=now,
      expires_at=now + 840000,
  )


def make_sizing(
    symbol: str = "BTCUSDT",
    is_long: bool = True,
    qty: float = 0.5,
    entry: float = 50000.0,
    sl: float = 49000.0,
    tp: float = 53000.0,
    leverage: int = 10,
) -> SizingResult:
  return SizingResult(
      is_valid=True,
      rejection_reason=None,
      symbol=symbol,
      is_long=is_long,
      equity=10000.0,
      risk_budget=1000.0,
      stop_distance=abs(entry - sl),
      raw_quantity=qty,
      friction_adjusted_qty=qty,
      final_quantity=qty,
      entry_price=entry,
      sl_price=sl,
      tp_price=tp,
      leverage=leverage,
      required_margin=(qty * entry) / leverage,
      estimated_liq_price=45000.0 if is_long else 55000.0,
      maint_margin_ratio=0.004,
  )


def make_order_fill(
    symbol: str = "BTCUSDT",
    client_order_id: str = "HA_E_12345",
    order_id: int = 1001,
    status: OrderStatus = OrderStatus.FILLED,
    side: OrderSide = OrderSide.BUY,
    qty: float = 0.5,
    avg_price: float = 50000.0,
) -> OrderFill:
  return OrderFill(
      symbol=symbol,
      client_order_id=client_order_id,
      order_id=order_id,
      status=status,
      side=side,
      order_type=OrderType.MARKET,
      orig_qty=qty,
      executed_qty=qty,
      avg_price=avg_price,
      cum_quote=qty * avg_price,
      transact_time=int(time.time() * 1000),
  )



class TestPositionManagerOpenPosition:
  """Tests for opening new positions."""

  @pytest.mark.asyncio
  async def test_open_position_long_success(
      self, position_manager: PositionManager, mock_router, mock_protective, repository
  ):
    signal = make_signal("BTCUSDT", Direction.LONG)
    sizing = make_sizing("BTCUSDT", is_long=True, qty=0.5, entry=50000.0, sl=49000.0, tp=53000.0)

    # Mock order router entry fill
    mock_router.execute_market_entry.return_value = make_order_fill(
        symbol="BTCUSDT",
        client_order_id="HA_E_12345",
        order_id=1001,
        status=OrderStatus.FILLED,
        side=OrderSide.BUY,
        qty=0.5,
        avg_price=50050.0,
    )

    # Mock protective orders
    mock_pair = ProtectiveOrderPair(
        symbol="BTCUSDT",
        entry_side=OrderSide.BUY,
        exit_side=OrderSide.SELL,
        quantity=0.5,
        avg_entry_price=50050.0,
        sl_order_id=2001,
        sl_client_order_id="HA_SL_12345",
        sl_price=49000.0,
        tp_order_id=2002,
        tp_client_order_id="HA_TP_12345",
        tp_price=53200.0,
    )
    mock_protective.place_protective_orders.return_value = mock_pair

    assert not position_manager.has_open_position()

    pos = await position_manager.open_position(
        signal=signal,
        sizing=sizing,
        tick_size=0.1,
        step_size=0.001,
    )

    assert pos is not None
    assert pos.symbol == "BTCUSDT"
    assert pos.status == PositionState.OPEN
    assert pos.quantity == 0.5
    assert pos.entry_price == 50050.0
    assert pos.protective_pair == mock_pair
    assert position_manager.has_open_position()

    # Verify repository saved state
    db_pos = repository.get_open_position()
    assert db_pos is not None
    assert db_pos.position_id == pos.position_id
    assert db_pos.status == "OPEN"
    assert db_pos.entry_price == 50050.0

    # Verify orders saved in database
    orders = repository.get_orders_for_position(pos.position_id)
    assert len(orders) == 3
    order_types = [o.order_type for o in orders]
    assert "MARKET" in order_types
    assert "STOP_MARKET" in order_types
    assert "TAKE_PROFIT_MARKET" in order_types

  @pytest.mark.asyncio
  async def test_global_position_lock_rejects_second_entry(
      self, position_manager: PositionManager, mock_router, mock_protective
  ):
    signal = make_signal("BTCUSDT", Direction.LONG)
    sizing = make_sizing("BTCUSDT", is_long=True, qty=0.5)

    mock_router.execute_market_entry.return_value = make_order_fill(
        symbol="BTCUSDT",
        client_order_id="HA_E_1",
        order_id=1,
        status=OrderStatus.FILLED,
        side=OrderSide.BUY,
        qty=0.5,
        avg_price=50000.0,
    )
    mock_protective.place_protective_orders.return_value = ProtectiveOrderPair(
        symbol="BTCUSDT",
        entry_side=OrderSide.BUY,
        exit_side=OrderSide.SELL,
        quantity=0.5,
        avg_entry_price=50000.0,
        sl_order_id=2,
        sl_client_order_id="HA_SL_1",
        sl_price=49000.0,
        tp_order_id=3,
        tp_client_order_id="HA_TP_1",
        tp_price=53000.0,
    )

    await position_manager.open_position(signal, sizing, 0.1, 0.001)
    assert position_manager.has_open_position()

    # Second entry attempt must raise PositionLockError
    signal2 = make_signal("ETHUSDT", Direction.LONG)
    sizing2 = make_sizing("ETHUSDT", is_long=True, qty=2.0)

    with pytest.raises(PositionLockError, match="lock is active"):
      await position_manager.open_position(signal2, sizing2, 0.01, 0.01)

  @pytest.mark.asyncio
  async def test_entry_order_failure_releases_lock(
      self, position_manager: PositionManager, mock_router, repository
  ):
    signal = make_signal("BTCUSDT", Direction.LONG)
    sizing = make_sizing("BTCUSDT", is_long=True, qty=0.5)

    mock_router.execute_market_entry.side_effect = ExecutionError("Binance margin error")

    with pytest.raises(ExecutionError, match="Binance margin error"):
      await position_manager.open_position(signal, sizing, 0.1, 0.001)

    # Position lock must be released
    assert not position_manager.has_open_position()
    assert repository.get_open_position() is None

  @pytest.mark.asyncio
  async def test_protective_orders_failure_releases_lock(
      self, position_manager: PositionManager, mock_router, mock_protective, repository
  ):
    signal = make_signal("BTCUSDT", Direction.LONG)
    sizing = make_sizing("BTCUSDT", is_long=True, qty=0.5)

    mock_router.execute_market_entry.return_value = make_order_fill(
        symbol="BTCUSDT",
        client_order_id="HA_E_FAIL",
        order_id=1,
        status=OrderStatus.FILLED,
        side=OrderSide.BUY,
        qty=0.5,
        avg_price=50000.0,
    )
    mock_protective.place_protective_orders.side_effect = ProtectiveOrderError(
        "SL placement rejected. Emergency market close executed."
    )

    with pytest.raises(ProtectiveOrderError):
      await position_manager.open_position(signal, sizing, 0.1, 0.001)

    # Position lock must be released because position was emergency closed
    assert not position_manager.has_open_position()
    assert repository.get_open_position() is None


class TestPositionManagerExitFill:
  """Tests for handling exit triggers (SL/TP fills)."""

  @pytest.mark.asyncio
  async def test_handle_exit_fill_stop_loss(
      self, position_manager: PositionManager, mock_router, mock_protective, repository
  ):
    # 1. Establish position
    signal = make_signal("BTCUSDT", Direction.LONG)
    sizing = make_sizing("BTCUSDT", is_long=True, qty=0.5, entry=50000.0, sl=49000.0, tp=53000.0)

    mock_router.execute_market_entry.return_value = make_order_fill(
        symbol="BTCUSDT",
        client_order_id="HA_E_SL_TEST",
        order_id=10,
        status=OrderStatus.FILLED,
        side=OrderSide.BUY,
        qty=0.5,
        avg_price=50000.0,
    )
    pair = ProtectiveOrderPair(
        symbol="BTCUSDT",
        entry_side=OrderSide.BUY,
        exit_side=OrderSide.SELL,
        quantity=0.5,
        avg_entry_price=50000.0,
        sl_order_id=20,
        sl_client_order_id="HA_SL_TEST",
        sl_price=49000.0,
        tp_order_id=30,
        tp_client_order_id="HA_TP_TEST",
        tp_price=53000.0,
    )
    mock_protective.place_protective_orders.return_value = pair
    mock_protective.get_active_pair.return_value = pair

    pos = await position_manager.open_position(signal, sizing, 0.1, 0.001)
    pos_id = pos.position_id

    # 2. Simulate SL triggering
    mock_protective.cancel_sibling_order.return_value = ExitTriggerResult(
        symbol="BTCUSDT",
        triggered_client_order_id="HA_SL_TEST",
        triggered_type=OrderType.STOP_MARKET,
        exit_reason=ExitReason.STOP_LOSS,
        cancelled_sibling_client_order_id="HA_TP_TEST",
        sibling_cancellation_success=True,
        position_closed_verified=True,
        timestamp_ms=int(time.time() * 1000),
    )

    exit_res = await position_manager.handle_exit_fill(
        triggered_client_order_id="HA_SL_TEST",
        exit_price=48950.0,
        exit_qty=0.5,
    )

    assert exit_res.exit_reason == ExitReason.STOP_LOSS
    assert not position_manager.has_open_position()

    # Check database closed position
    db_pos = repository.get_position(pos_id)
    assert db_pos is not None
    assert db_pos.status == "CLOSED"
    assert db_pos.exit_reason == "STOP_LOSS"
    assert db_pos.exit_price == 48950.0
    # PnL = (48950 - 50000) * 0.5 = -525.0
    assert db_pos.realized_pnl == pytest.approx(-525.0)

  @pytest.mark.asyncio
  async def test_handle_exit_fill_no_position_raises(
      self, position_manager: PositionManager
  ):
    with pytest.raises(PositionNotFoundError, match="No active or open position"):
      await position_manager.handle_exit_fill(triggered_client_order_id="HA_SL_NONEXISTENT")

  @pytest.mark.asyncio
  async def test_emergency_close_current_position(
      self, position_manager: PositionManager, mock_router, mock_protective, repository
  ):
    # Establish position
    signal = make_signal("BTCUSDT", Direction.LONG)
    sizing = make_sizing("BTCUSDT", is_long=True, qty=0.5)

    mock_router.execute_market_entry.return_value = make_order_fill(
        symbol="BTCUSDT",
        client_order_id="HA_E_EMG",
        order_id=1,
        status=OrderStatus.FILLED,
        side=OrderSide.BUY,
        qty=0.5,
        avg_price=50000.0,
    )
    pair = ProtectiveOrderPair(
        symbol="BTCUSDT",
        entry_side=OrderSide.BUY,
        exit_side=OrderSide.SELL,
        quantity=0.5,
        avg_entry_price=50000.0,
        sl_order_id=2,
        sl_client_order_id="HA_SL_EMG",
        sl_price=49000.0,
        tp_order_id=3,
        tp_client_order_id="HA_TP_EMG",
        tp_price=53000.0,
    )
    mock_protective.place_protective_orders.return_value = pair
    mock_protective.get_active_pair.return_value = pair
    mock_protective.execute_emergency_market_close.return_value = EmergencyExitResult(
        symbol="BTCUSDT",
        emergency_client_order_id="HA_EMG_123",
        quantity=0.5,
        is_closed=True,
    )

    pos = await position_manager.open_position(signal, sizing, 0.1, 0.001)

    emg_res = await position_manager.emergency_close_current_position(
        reason="OPERATOR_OVERRIDE"
    )
    assert emg_res.is_closed
    assert not position_manager.has_open_position()

    db_pos = repository.get_position(pos.position_id)
    assert db_pos.status == "CLOSED"
    assert db_pos.exit_reason == "OPERATOR_OVERRIDE"
