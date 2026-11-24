"""Unit tests for Phase 10 — Protective Order Manager.

Validates:
1. Directional tick-size price rounding (LONG: SL floor, TP ceil; SHORT: SL ceil, TP floor).
2. Sizing protective orders strictly against entry executedQty.
3. Recalculation of SL and 3:1 TP levels based on actual entry fill price.
4. Submission and verification of exchange-side STOP_MARKET and TAKE_PROFIT_MARKET orders with reduceOnly=true.
5. Sibling order cancellation when SL or TP triggers and fills.
6. Position closure verification (net zero exposure).
7. Fail-safe emergency market close if protective orders cannot be established or verified.
"""

from unittest.mock import AsyncMock, MagicMock
import pytest
from src.exchange.client import BinanceFuturesClient
from src.exchange.exceptions import BinanceAPIError
from src.exchange.models import PositionRisk
from src.execution.exceptions import ProtectiveOrderError
from src.execution.models import OrderSide, OrderStatus, OrderType
from src.positions.models import (
    ExitReason,
    ProtectiveOrderPair,
)
from src.positions.protective import (
    ProtectiveOrderManager,
    round_price_directional,
)


# ==============================================================================
# 1. Price Rounding Tests
# ==============================================================================


class TestRoundPriceDirectional:
  """Tests for directional tick-size rounding."""

  def test_round_down_floor(self):
    assert round_price_directional(98.009, 0.01, round_up=False) == pytest.approx(98.00)
    assert round_price_directional(98.004, 0.01, round_up=False) == pytest.approx(98.00)
    assert round_price_directional(98.000, 0.01, round_up=False) == pytest.approx(98.00)

  def test_round_up_ceil(self):
    assert round_price_directional(106.001, 0.01, round_up=True) == pytest.approx(106.01)
    assert round_price_directional(106.009, 0.01, round_up=True) == pytest.approx(106.01)
    assert round_price_directional(106.000, 0.01, round_up=True) == pytest.approx(106.00)

  def test_large_tick_size(self):
    # tick = 0.5
    assert round_price_directional(101.2, 0.5, round_up=False) == pytest.approx(101.0)
    assert round_price_directional(101.2, 0.5, round_up=True) == pytest.approx(101.5)

    # tick = 1.0
    assert round_price_directional(105.8, 1.0, round_up=False) == pytest.approx(105.0)
    assert round_price_directional(105.1, 1.0, round_up=True) == pytest.approx(106.0)

  def test_zero_or_negative_tick_raises(self):
    with pytest.raises(ValueError, match="positive"):
      round_price_directional(100.0, 0.0, round_up=False)


# ==============================================================================
# 2. ProtectiveOrderPair Model Tests
# ==============================================================================


class TestProtectiveOrderPairModel:
  """Tests for ProtectiveOrderPair identification and sibling lookup."""

  @pytest.fixture
  def sample_pair(self):
    return ProtectiveOrderPair(
        symbol="BTCUSDT",
        entry_side=OrderSide.BUY,
        exit_side=OrderSide.SELL,
        quantity=0.5,
        avg_entry_price=50000.0,
        sl_order_id=111,
        sl_client_order_id="HA_SL_001",
        sl_price=49000.0,
        tp_order_id=222,
        tp_client_order_id="HA_TP_002",
        tp_price=53000.0,
    )

  def test_is_sl_and_tp_order(self, sample_pair):
    assert sample_pair.is_sl_order(client_order_id="HA_SL_001") is True
    assert sample_pair.is_sl_order(order_id=111) is True
    assert sample_pair.is_sl_order(client_order_id="HA_TP_002") is False

    assert sample_pair.is_tp_order(client_order_id="HA_TP_002") is True
    assert sample_pair.is_tp_order(order_id=222) is True
    assert sample_pair.is_tp_order(client_order_id="HA_SL_001") is False

  def test_get_sibling_order_details(self, sample_pair):
    # When SL triggers, sibling is TP
    tp_cid, tp_oid, s_type = sample_pair.get_sibling_order_details(
        triggered_client_order_id="HA_SL_001"
    )
    assert tp_cid == "HA_TP_002"
    assert tp_oid == 222
    assert s_type == OrderType.TAKE_PROFIT_MARKET

    # When TP triggers, sibling is SL
    sl_cid, sl_oid, s_type = sample_pair.get_sibling_order_details(
        triggered_client_order_id="HA_TP_002"
    )
    assert sl_cid == "HA_SL_001"
    assert sl_oid == 111
    assert s_type == OrderType.STOP_MARKET

  def test_get_sibling_unrecognized_order_raises(self, sample_pair):
    with pytest.raises(ValueError, match="Neither SL nor TP matches"):
      sample_pair.get_sibling_order_details(triggered_client_order_id="UNKNOWN_123")


# ==============================================================================
# 3. Protective Order Placement & Recalculation Tests
# ==============================================================================


class TestProtectiveOrderPlacement:
  """Tests for ProtectiveOrderManager order placement."""

  @pytest.fixture
  def mock_client(self):
    client = MagicMock(spec=BinanceFuturesClient)
    client.place_order = AsyncMock()
    client.query_order = AsyncMock()
    client.cancel_order = AsyncMock(return_value={"code": 200, "status": "CANCELED"})
    client.get_positions = AsyncMock(return_value=[])
    return client

  @pytest.mark.asyncio
  async def test_place_protective_orders_long_recalculation(self, mock_client):
    """LONG trade: Entry=100.0, SL=98.0 -> dist=2.0 -> TP=100 + 3*2 = 106.0.

    SL rounded DOWN, TP rounded UP.
    """
    manager = ProtectiveOrderManager(mock_client)

    mock_client.place_order.side_effect = [
        {"orderId": 101, "clientOrderId": "HA_SL_1", "status": "NEW"},  # SL
        {"orderId": 102, "clientOrderId": "HA_TP_1", "status": "NEW"},  # TP
    ]
    mock_client.query_order.side_effect = [
        {"status": "NEW"},  # query SL
        {"status": "NEW"},  # query TP
    ]

    pair = await manager.place_protective_orders(
        symbol="BTCUSDT",
        entry_side=OrderSide.BUY,
        executed_qty=0.5,
        avg_entry_price=100.0,
        stop_loss_price=98.0,
        tick_size=0.01,
        step_size=0.001,
    )

    assert pair.symbol == "BTCUSDT"
    assert pair.exit_side == OrderSide.SELL
    assert pair.quantity == 0.5
    assert pair.sl_price == 98.00
    assert pair.tp_price == 106.00
    assert pair.sl_order_id == 101
    assert pair.tp_order_id == 102
    assert pair.is_active is True

    # Check place_order was called twice with reduceOnly=true
    assert mock_client.place_order.call_count == 2
    sl_call_params = mock_client.place_order.call_args_list[0][0][0]
    tp_call_params = mock_client.place_order.call_args_list[1][0][0]

    assert sl_call_params["type"] == "STOP_MARKET"
    assert sl_call_params["reduceOnly"] == "true"
    assert sl_call_params["stopPrice"] == 98.00
    assert sl_call_params["quantity"] == 0.5

    assert tp_call_params["type"] == "TAKE_PROFIT_MARKET"
    assert tp_call_params["reduceOnly"] == "true"
    assert tp_call_params["stopPrice"] == 106.00
    assert tp_call_params["quantity"] == 0.5

    # Check pair is saved in registry
    assert manager.get_active_pair("BTCUSDT") is pair

  @pytest.mark.asyncio
  async def test_place_protective_orders_short_recalculation(self, mock_client):
    """SHORT trade: Entry=100.0, SL=103.0 -> dist=3.0 -> TP=100 - 3*3 = 91.0.

    SL rounded UP, TP rounded DOWN.
    """
    manager = ProtectiveOrderManager(mock_client)

    mock_client.place_order.side_effect = [
        {"orderId": 201, "clientOrderId": "HA_SL_2", "status": "NEW"},
        {"orderId": 202, "clientOrderId": "HA_TP_2", "status": "NEW"},
    ]
    mock_client.query_order.side_effect = [
        {"status": "NEW"},
        {"status": "NEW"},
    ]

    pair = await manager.place_protective_orders(
        symbol="ETHUSDT",
        entry_side=OrderSide.SELL,
        executed_qty=2.0,
        avg_entry_price=100.0,
        stop_loss_price=103.0,
        tick_size=0.01,
        step_size=0.01,
    )

    assert pair.symbol == "ETHUSDT"
    assert pair.exit_side == OrderSide.BUY
    assert pair.quantity == 2.0
    assert pair.sl_price == 103.00
    assert pair.tp_price == 91.00

  @pytest.mark.asyncio
  async def test_invalid_stop_placement_raises_error(self, mock_client):
    manager = ProtectiveOrderManager(mock_client)

    # LONG with SL above entry
    with pytest.raises(ProtectiveOrderError, match="must be strictly below"):
      await manager.place_protective_orders(
          symbol="BTCUSDT",
          entry_side=OrderSide.BUY,
          executed_qty=0.5,
          avg_entry_price=100.0,
          stop_loss_price=102.0,
          tick_size=0.01,
          step_size=0.001,
      )

    # SHORT with SL below entry
    with pytest.raises(ProtectiveOrderError, match="must be strictly above"):
      await manager.place_protective_orders(
          symbol="BTCUSDT",
          entry_side=OrderSide.SELL,
          executed_qty=0.5,
          avg_entry_price=100.0,
          stop_loss_price=98.0,
          tick_size=0.01,
          step_size=0.001,
      )

  @pytest.mark.asyncio
  async def test_invalid_qty_or_entry_price_raises_error(self, mock_client):
    manager = ProtectiveOrderManager(mock_client)

    with pytest.raises(ProtectiveOrderError, match="non-positive quantity"):
      await manager.place_protective_orders(
          symbol="BTCUSDT",
          entry_side=OrderSide.BUY,
          executed_qty=0.0,
          avg_entry_price=100.0,
          stop_loss_price=98.0,
          tick_size=0.01,
          step_size=0.001,
      )


# ==============================================================================
# 4. Emergency Fail-Safe Handling on Placement Failure Tests
# ==============================================================================


class TestEmergencyHandlingOnFailure:
  """Tests for emergency fail-safe market close when protection fails."""

  @pytest.fixture
  def mock_client(self):
    client = MagicMock(spec=BinanceFuturesClient)
    client.place_order = AsyncMock()
    client.query_order = AsyncMock()
    client.cancel_order = AsyncMock(return_value={"code": 200, "status": "CANCELED"})
    client.get_positions = AsyncMock(return_value=[
        PositionRisk(
            symbol="BTCUSDT",
            position_amt=0.0,  # verified closed
            entry_price=0.0,
            mark_price=50000.0,
            unrealized_profit=0.0,
            liquidation_price=0.0,
            leverage=20,
            margin_type="ISOLATED",
            position_side="BOTH",
        )
    ])
    return client

  @pytest.mark.asyncio
  async def test_sl_placement_fails_triggers_emergency_close(self, mock_client):
    manager = ProtectiveOrderManager(mock_client)
    # SL placement rejects
    mock_client.place_order.side_effect = [
        BinanceAPIError(message="Margin insufficient for SL.", code=-2010),
        {"status": "FILLED"},  # emergency market close succeeds
    ]

    with pytest.raises(ProtectiveOrderError, match="Emergency market close executed"):
      await manager.place_protective_orders(
          symbol="BTCUSDT",
          entry_side=OrderSide.BUY,
          executed_qty=0.5,
          avg_entry_price=100.0,
          stop_loss_price=98.0,
          tick_size=0.01,
          step_size=0.001,
      )

    # Verify emergency order was placed
    assert mock_client.place_order.call_count == 2
    emg_call = mock_client.place_order.call_args_list[1][0][0]
    assert emg_call["type"] == "MARKET"
    assert emg_call["side"] == "SELL"
    assert emg_call["reduceOnly"] == "true"
    assert emg_call["newClientOrderId"].startswith("HA_EMG_")

  @pytest.mark.asyncio
  async def test_tp_placement_fails_cancels_sl_and_triggers_emergency_close(self, mock_client):
    manager = ProtectiveOrderManager(mock_client)
    # SL succeeds, TP rejects, Emergency close succeeds
    mock_client.place_order.side_effect = [
        {"orderId": 101, "clientOrderId": "HA_SL_101", "status": "NEW"},  # SL
        BinanceAPIError(message="Filter failure on TP.", code=-1013),       # TP fails
        {"status": "FILLED"},                                               # Emergency close
    ]

    with pytest.raises(ProtectiveOrderError, match="Emergency market close executed"):
      await manager.place_protective_orders(
          symbol="BTCUSDT",
          entry_side=OrderSide.BUY,
          executed_qty=0.5,
          avg_entry_price=100.0,
          stop_loss_price=98.0,
          tick_size=0.01,
          step_size=0.001,
      )

    # Verify SL was cancelled before emergency close
    assert mock_client.cancel_order.await_count == 1
    call_kwargs = mock_client.cancel_order.call_args[1]
    assert call_kwargs["client_order_id"].startswith("HA_SL_")

  @pytest.mark.asyncio
  async def test_verification_status_not_new_triggers_emergency_close(self, mock_client):
    manager = ProtectiveOrderManager(mock_client)
    # SL & TP placed, but verification query returns CANCELED
    mock_client.place_order.side_effect = [
        {"orderId": 101, "clientOrderId": "HA_SL_101", "status": "NEW"},
        {"orderId": 102, "clientOrderId": "HA_TP_102", "status": "NEW"},
        {"status": "FILLED"},  # emergency close
    ]
    mock_client.query_order.side_effect = [
        {"status": "NEW"},
        {"status": "CANCELED"},  # TP unexpectedly not NEW
    ]

    with pytest.raises(ProtectiveOrderError, match="TP order verification failed"):
      await manager.place_protective_orders(
          symbol="BTCUSDT",
          entry_side=OrderSide.BUY,
          executed_qty=0.5,
          avg_entry_price=100.0,
          stop_loss_price=98.0,
          tick_size=0.01,
          step_size=0.001,
      )


# ==============================================================================
# 5. Sibling Order Cancellation & Position Closure Tests
# ==============================================================================


class TestSiblingOrderCancellation:
  """Tests for sibling order cancellation on exit fill."""

  @pytest.fixture
  def mock_client(self):
    client = MagicMock(spec=BinanceFuturesClient)
    client.cancel_order = AsyncMock(return_value={"code": 200, "status": "CANCELED"})
    client.get_positions = AsyncMock(return_value=[
        PositionRisk(
            symbol="BTCUSDT",
            position_amt=0.0,  # closed
            entry_price=0.0,
            mark_price=50000.0,
            unrealized_profit=0.0,
            liquidation_price=0.0,
            leverage=20,
            margin_type="ISOLATED",
            position_side="BOTH",
        )
    ])
    return client

  @pytest.mark.asyncio
  async def test_sl_fills_cancels_tp_sibling(self, mock_client):
    manager = ProtectiveOrderManager(mock_client)
    pair = ProtectiveOrderPair(
        symbol="BTCUSDT",
        entry_side=OrderSide.BUY,
        exit_side=OrderSide.SELL,
        quantity=0.5,
        avg_entry_price=100.0,
        sl_order_id=111,
        sl_client_order_id="HA_SL_111",
        sl_price=98.0,
        tp_order_id=222,
        tp_client_order_id="HA_TP_222",
        tp_price=106.0,
    )
    manager._active_pairs["BTCUSDT"] = pair

    # SL fills
    result = await manager.cancel_sibling_order(
        triggered_client_order_id="HA_SL_111",
        symbol="BTCUSDT",
    )

    assert result.exit_reason == ExitReason.STOP_LOSS
    assert result.triggered_type == OrderType.STOP_MARKET
    assert result.cancelled_sibling_client_order_id == "HA_TP_222"
    assert result.sibling_cancellation_success is True
    assert result.position_closed_verified is True
    assert pair.is_active is False
    assert manager.get_active_pair("BTCUSDT") is None

    mock_client.cancel_order.assert_awaited_once_with("BTCUSDT", client_order_id="HA_TP_222")

  @pytest.mark.asyncio
  async def test_tp_fills_cancels_sl_sibling(self, mock_client):
    manager = ProtectiveOrderManager(mock_client)
    pair = ProtectiveOrderPair(
        symbol="BTCUSDT",
        entry_side=OrderSide.BUY,
        exit_side=OrderSide.SELL,
        quantity=0.5,
        avg_entry_price=100.0,
        sl_order_id=111,
        sl_client_order_id="HA_SL_111",
        sl_price=98.0,
        tp_order_id=222,
        tp_client_order_id="HA_TP_222",
        tp_price=106.0,
    )
    manager._active_pairs["BTCUSDT"] = pair

    # TP fills
    result = await manager.cancel_sibling_order(
        triggered_client_order_id="HA_TP_222",
        symbol="BTCUSDT",
    )

    assert result.exit_reason == ExitReason.TAKE_PROFIT
    assert result.triggered_type == OrderType.TAKE_PROFIT_MARKET
    assert result.cancelled_sibling_client_order_id == "HA_SL_111"
    assert result.sibling_cancellation_success is True
    assert result.position_closed_verified is True

    mock_client.cancel_order.assert_awaited_once_with("BTCUSDT", client_order_id="HA_SL_111")

  @pytest.mark.asyncio
  async def test_sibling_already_gone_absorbed(self, mock_client):
    manager = ProtectiveOrderManager(mock_client)
    pair = ProtectiveOrderPair(
        symbol="BTCUSDT",
        entry_side=OrderSide.BUY,
        exit_side=OrderSide.SELL,
        quantity=0.5,
        avg_entry_price=100.0,
        sl_order_id=111,
        sl_client_order_id="HA_SL_111",
        sl_price=98.0,
        tp_order_id=222,
        tp_client_order_id="HA_TP_222",
        tp_price=106.0,
    )
    manager._active_pairs["BTCUSDT"] = pair

    # Binance returns -2011 (Unknown order) because sibling already cancelled/filled
    mock_client.cancel_order.side_effect = BinanceAPIError(message="Unknown order sent.", code=-2011)

    result = await manager.cancel_sibling_order(
        triggered_client_order_id="HA_SL_111",
        symbol="BTCUSDT",
    )
    assert result.sibling_cancellation_success is True

  @pytest.mark.asyncio
  async def test_no_active_pair_raises_value_error(self, mock_client):
    manager = ProtectiveOrderManager(mock_client)
    with pytest.raises(ValueError, match="No active protective order pair found"):
      await manager.cancel_sibling_order(triggered_client_order_id="UNKNOWN")


# ==============================================================================
# 6. Position Closure Verification Tests
# ==============================================================================


class TestPositionClosureVerification:
  """Tests for verify_position_closed."""

  @pytest.fixture
  def mock_client(self):
    return MagicMock(spec=BinanceFuturesClient)

  @pytest.mark.asyncio
  async def test_verify_position_closed_immediate(self, mock_client):
    mock_client.get_positions = AsyncMock(return_value=[
        PositionRisk(
            symbol="BTCUSDT",
            position_amt=0.0,  # closed
            entry_price=0.0,
            mark_price=50000.0,
            unrealized_profit=0.0,
            liquidation_price=0.0,
            leverage=20,
            margin_type="ISOLATED",
            position_side="BOTH",
        )
    ])
    manager = ProtectiveOrderManager(mock_client)
    assert await manager.verify_position_closed("BTCUSDT") is True

  @pytest.mark.asyncio
  async def test_verify_position_closed_after_delay(self, mock_client):
    # First call: position open (0.5), second call: position closed (0.0)
    mock_client.get_positions = AsyncMock(side_effect=[
        [PositionRisk("BTCUSDT", 0.5, 50000.0, 50000.0, 0.0, 48000.0, 20, "ISOLATED", "BOTH")],
        [PositionRisk("BTCUSDT", 0.0, 0.0, 50000.0, 0.0, 0.0, 20, "ISOLATED", "BOTH")],
    ])
    manager = ProtectiveOrderManager(mock_client)
    is_closed = await manager.verify_position_closed(
        "BTCUSDT", timeout_seconds=1.0, poll_interval_seconds=0.01
    )
    assert is_closed is True

  @pytest.mark.asyncio
  async def test_verify_position_closed_timeout_fails(self, mock_client):
    # Position remains open
    mock_client.get_positions = AsyncMock(return_value=[
        PositionRisk("BTCUSDT", 0.5, 50000.0, 50000.0, 0.0, 48000.0, 20, "ISOLATED", "BOTH")
    ])
    manager = ProtectiveOrderManager(mock_client)
    is_closed = await manager.verify_position_closed(
        "BTCUSDT", timeout_seconds=0.05, poll_interval_seconds=0.01
    )
    assert is_closed is False
