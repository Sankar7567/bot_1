"""Unit tests for Phase 09 — Binance Order Execution Engine.

Validates:
1. Deterministic clientOrderId generation, formats, uniqueness, and parsing.
2. OrderExecutionRequest parameter mapping and OrderFill parsing.
3. OrderRouter market order execution, isolated margin / leverage preparation.
4. Network failure reconciliation (query by clientOrderId before retry/failure).
5. Immediate fill vs polling for fill completion.
6. Partial fill handling and fill timeout exceptions.
7. Idempotent cancellation and order querying.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock
import pytest
from src.exchange.client import BinanceFuturesClient
from src.exchange.exceptions import (
    BinanceAPIError,
    BinanceNetworkError,
)
from src.execution.client_id import (
    MAX_CLIENT_ORDER_ID_LENGTH,
    OrderPrefix,
    generate_client_order_id,
    is_bot_client_order_id,
    parse_client_order_id,
)
from src.execution.exceptions import (
    ExecutionError,
    FillTimeoutError,
    OrderRejectedError,
)
from src.execution.models import (
    OrderExecutionRequest,
    OrderFill,
    OrderSide,
    OrderStatus,
    OrderType,
)
from src.execution.order_router import OrderRouter


# ==============================================================================
# 1. Client Order ID Generation Tests
# ==============================================================================


class TestClientId:
  """Tests for clientOrderId generation and parsing."""

  def test_generate_client_order_id_format(self):
    cid = generate_client_order_id(OrderPrefix.ENTRY, timestamp_ms=1711234567890)
    assert cid.startswith("HA_E_1711234567890_")
    assert len(cid) <= MAX_CLIENT_ORDER_ID_LENGTH
    assert is_bot_client_order_id(cid) is True

  def test_all_prefixes_supported(self):
    for pfx in OrderPrefix:
      cid = generate_client_order_id(pfx)
      assert is_bot_client_order_id(cid) is True
      parsed = parse_client_order_id(cid)
      assert parsed is not None
      assert parsed["prefix"] == pfx

  def test_uniqueness_no_collisions(self):
    generated = {generate_client_order_id(OrderPrefix.ENTRY) for _ in range(1000)}
    assert len(generated) == 1000

  def test_max_length_constraint(self):
    cid = generate_client_order_id(OrderPrefix.EMERGENCY, hex_len=8)
    assert len(cid) <= MAX_CLIENT_ORDER_ID_LENGTH

  def test_invalid_client_ids_rejected(self):
    assert is_bot_client_order_id("") is False
    assert is_bot_client_order_id("random_123456") is False
    assert is_bot_client_order_id("web_1711234567890_abcd") is False
    assert is_bot_client_order_id("HA_UNKNOWN_1711234567890_abcd") is False
    assert parse_client_order_id("invalid") is None


# ==============================================================================
# 2. Execution Models Tests
# ==============================================================================


class TestExecutionModels:
  """Tests for OrderExecutionRequest and OrderFill parsing."""

  def test_order_execution_request_to_binance_params(self):
    req = OrderExecutionRequest(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=0.123,
        client_order_id="HA_E_1711234567890_a1f9",
        reduce_only=False,
    )
    params = req.to_binance_params()
    assert params["symbol"] == "BTCUSDT"
    assert params["side"] == "BUY"
    assert params["type"] == "MARKET"
    assert params["quantity"] == 0.123
    assert params["newClientOrderId"] == "HA_E_1711234567890_a1f9"
    assert "reduceOnly" not in params

  def test_protective_order_request_to_binance_params(self):
    req = OrderExecutionRequest(
        symbol="ETHUSDT",
        side=OrderSide.SELL,
        order_type=OrderType.STOP_MARKET,
        quantity=1.5,
        client_order_id="HA_SL_1711234567890_b2c3",
        reduce_only=True,
        stop_price=3000.5,
        time_in_force="GTC",
    )
    params = req.to_binance_params()
    assert params["reduceOnly"] == "true"
    assert params["stopPrice"] == 3000.5
    assert params["timeInForce"] == "GTC"

  def test_order_fill_from_raw_response_filled(self):
    raw = {
        "symbol": "BTCUSDT",
        "orderId": 987654321,
        "clientOrderId": "HA_E_1711234567890_a1f9",
        "status": "FILLED",
        "side": "BUY",
        "type": "MARKET",
        "origQty": "0.500",
        "executedQty": "0.500",
        "cumQuote": "32500.00",
        "avgPrice": "65000.00",
        "updateTime": 1711234568000,
    }
    fill = OrderFill.from_raw_response(raw)
    assert fill.symbol == "BTCUSDT"
    assert fill.order_id == 987654321
    assert fill.client_order_id == "HA_E_1711234567890_a1f9"
    assert fill.status == OrderStatus.FILLED
    assert fill.side == OrderSide.BUY
    assert fill.executed_qty == 0.5
    assert fill.avg_price == 65000.0
    assert fill.cum_quote == 32500.0
    assert fill.is_fully_filled is True
    assert fill.is_partially_filled is False

  def test_order_fill_avg_price_derived_from_cum_quote(self):
    raw = {
        "symbol": "BTCUSDT",
        "orderId": 12345,
        "clientOrderId": "HA_E_1711234567890_a1f9",
        "status": "FILLED",
        "side": "BUY",
        "type": "MARKET",
        "origQty": "2.0",
        "executedQty": "2.0",
        "cumQuote": "100.0",
        "avgPrice": "0",  # Binance sometimes returns "0" in initial response
    }
    fill = OrderFill.from_raw_response(raw)
    assert fill.avg_price == 50.0  # 100 / 2.0


# ==============================================================================
# 3. Order Router Execution Tests
# ==============================================================================


class TestOrderRouter:
  """Unit tests for OrderRouter market entry execution."""

  @pytest.fixture
  def mock_client(self):
    client = MagicMock(spec=BinanceFuturesClient)
    client.change_margin_type = AsyncMock(return_value={"code": 200, "msg": "success"})
    client.change_leverage = AsyncMock(return_value={"leverage": 20, "symbol": "BTCUSDT"})
    client.place_order = AsyncMock()
    client.query_order = AsyncMock()
    client.cancel_order = AsyncMock(return_value={"code": 200, "status": "CANCELED"})
    return client

  @pytest.mark.asyncio
  async def test_execute_market_entry_success_immediate_fill(self, mock_client):
    router = OrderRouter(mock_client)
    mock_client.place_order.return_value = {
        "symbol": "BTCUSDT",
        "orderId": 1001,
        "clientOrderId": "HA_E_1711234567890_a1f9",
        "status": "FILLED",
        "side": "BUY",
        "type": "MARKET",
        "origQty": "0.100",
        "executedQty": "0.100",
        "avgPrice": "50000.00",
        "cumQuote": "5000.00",
        "updateTime": 1711234568000,
    }

    fill = await router.execute_market_entry(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        quantity=0.1,
        leverage=20,
        client_order_id="HA_E_1711234567890_a1f9",
    )

    mock_client.change_margin_type.assert_awaited_once_with("BTCUSDT", "ISOLATED")
    mock_client.change_leverage.assert_awaited_once_with("BTCUSDT", 20)
    mock_client.place_order.assert_awaited_once()

    assert fill.is_fully_filled is True
    assert fill.executed_qty == 0.1
    assert fill.avg_price == 50000.0

  @pytest.mark.asyncio
  async def test_execute_market_entry_margin_type_already_set_handled(self, mock_client):
    router = OrderRouter(mock_client)
    mock_client.change_margin_type.side_effect = BinanceAPIError(message="No need to change margin type.", code=-4046)
    mock_client.place_order.return_value = {
        "symbol": "BTCUSDT",
        "orderId": 1001,
        "clientOrderId": "HA_E_123",
        "status": "FILLED",
        "side": "BUY",
        "type": "MARKET",
        "origQty": "0.1",
        "executedQty": "0.1",
        "avgPrice": "50000.0",
    }

    fill = await router.execute_market_entry(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        quantity=0.1,
        leverage=20,
    )
    assert fill.is_fully_filled is True

  @pytest.mark.asyncio
  async def test_execute_market_entry_margin_error_raises_execution_error(self, mock_client):
    router = OrderRouter(mock_client)
    mock_client.change_margin_type.side_effect = BinanceAPIError(message="Invalid symbol.", code=-4001)

    with pytest.raises(ExecutionError, match="Failed to set margin type"):
      await router.execute_market_entry("BTCUSDT", OrderSide.BUY, 0.1, 20)

  @pytest.mark.asyncio
  async def test_execute_market_entry_leverage_error_raises_execution_error(self, mock_client):
    router = OrderRouter(mock_client)
    mock_client.change_leverage.side_effect = BinanceAPIError(message="Leverage 150 exceeds max 125.", code=-4028)

    with pytest.raises(ExecutionError, match="Failed to set leverage"):
      await router.execute_market_entry("BTCUSDT", OrderSide.BUY, 0.1, 150)

  @pytest.mark.asyncio
  async def test_execute_market_entry_polling_to_fill(self, mock_client):
    router = OrderRouter(mock_client)
    # Initial response is NEW
    mock_client.place_order.return_value = {
        "symbol": "BTCUSDT",
        "orderId": 1002,
        "clientOrderId": "HA_E_POLL",
        "status": "NEW",
        "side": "BUY",
        "type": "MARKET",
        "origQty": "0.1",
        "executedQty": "0.0",
        "avgPrice": "0",
    }
    # Subsequent query returns FILLED
    mock_client.query_order.return_value = {
        "symbol": "BTCUSDT",
        "orderId": 1002,
        "clientOrderId": "HA_E_POLL",
        "status": "FILLED",
        "side": "BUY",
        "type": "MARKET",
        "origQty": "0.1",
        "executedQty": "0.1",
        "avgPrice": "50100.0",
    }

    fill = await router.execute_market_entry(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        quantity=0.1,
        leverage=20,
        client_order_id="HA_E_POLL",
        fill_timeout_seconds=2.0,
        poll_interval_seconds=0.01,
    )
    assert fill.is_fully_filled is True
    assert fill.avg_price == 50100.0
    mock_client.query_order.assert_awaited()

  @pytest.mark.asyncio
  async def test_execute_market_entry_network_timeout_reconciliation_success(self, mock_client):
    router = OrderRouter(mock_client)
    # Network disconnect occurs when placing order
    mock_client.place_order.side_effect = BinanceNetworkError("Connection timed out.")
    # Query finds that exchange DID receive and fill the order
    mock_client.query_order.return_value = {
        "symbol": "BTCUSDT",
        "orderId": 1003,
        "clientOrderId": "HA_E_RECON",
        "status": "FILLED",
        "side": "BUY",
        "type": "MARKET",
        "origQty": "0.2",
        "executedQty": "0.2",
        "avgPrice": "50200.0",
    }

    fill = await router.execute_market_entry(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        quantity=0.2,
        leverage=20,
        client_order_id="HA_E_RECON",
    )
    assert fill.is_fully_filled is True
    assert fill.executed_qty == 0.2
    assert fill.avg_price == 50200.0

  @pytest.mark.asyncio
  async def test_execute_market_entry_network_timeout_order_not_found(self, mock_client):
    router = OrderRouter(mock_client)
    mock_client.place_order.side_effect = BinanceNetworkError("Connection timed out.")
    # Order does not exist on exchange (-2013)
    mock_client.query_order.side_effect = BinanceAPIError(message="Order does not exist.", code=-2013)

    with pytest.raises(ExecutionError, match="Failed to submit or reconcile"):
      await router.execute_market_entry(
          symbol="BTCUSDT",
          side=OrderSide.BUY,
          quantity=0.2,
          leverage=20,
          client_order_id="HA_E_LOST",
      )

  @pytest.mark.asyncio
  async def test_execute_market_entry_exchange_rejection_raises(self, mock_client):
    router = OrderRouter(mock_client)
    mock_client.place_order.side_effect = BinanceAPIError(message="Account has insufficient balance.", code=-2010)

    with pytest.raises(OrderRejectedError, match="Account has insufficient balance"):
      await router.execute_market_entry("BTCUSDT", OrderSide.BUY, 10.0, 20)

  @pytest.mark.asyncio
  async def test_execute_market_entry_terminal_rejected_status(self, mock_client):
    router = OrderRouter(mock_client)
    mock_client.place_order.return_value = {
        "symbol": "BTCUSDT",
        "orderId": 1004,
        "clientOrderId": "HA_E_REJ",
        "status": "REJECTED",
        "side": "BUY",
        "type": "MARKET",
        "origQty": "0.1",
        "executedQty": "0.0",
    }

    with pytest.raises(OrderRejectedError, match="ended in terminal status: REJECTED"):
      await router.execute_market_entry("BTCUSDT", OrderSide.BUY, 0.1, 20, client_order_id="HA_E_REJ")

  @pytest.mark.asyncio
  async def test_execute_market_entry_fill_timeout_zero_fill(self, mock_client):
    router = OrderRouter(mock_client)
    mock_client.place_order.return_value = {
        "symbol": "BTCUSDT",
        "orderId": 1005,
        "clientOrderId": "HA_E_TIMEOUT",
        "status": "NEW",
        "side": "BUY",
        "type": "MARKET",
        "origQty": "0.1",
        "executedQty": "0.0",
    }
    mock_client.query_order.return_value = {
        "symbol": "BTCUSDT",
        "orderId": 1005,
        "clientOrderId": "HA_E_TIMEOUT",
        "status": "NEW",
        "side": "BUY",
        "type": "MARKET",
        "origQty": "0.1",
        "executedQty": "0.0",
    }

    with pytest.raises(FillTimeoutError, match="failed to fill within"):
      await router.execute_market_entry(
          symbol="BTCUSDT",
          side=OrderSide.BUY,
          quantity=0.1,
          leverage=20,
          client_order_id="HA_E_TIMEOUT",
          fill_timeout_seconds=0.05,
          poll_interval_seconds=0.01,
      )

  @pytest.mark.asyncio
  async def test_execute_market_entry_partial_fill_returned(self, mock_client):
    router = OrderRouter(mock_client)
    mock_client.place_order.return_value = {
        "symbol": "BTCUSDT",
        "orderId": 1006,
        "clientOrderId": "HA_E_PARTIAL",
        "status": "PARTIALLY_FILLED",
        "side": "BUY",
        "type": "MARKET",
        "origQty": "1.0",
        "executedQty": "0.4",
        "avgPrice": "50000.0",
        "cumQuote": "20000.0",
    }
    mock_client.query_order.return_value = {
        "symbol": "BTCUSDT",
        "orderId": 1006,
        "clientOrderId": "HA_E_PARTIAL",
        "status": "PARTIALLY_FILLED",
        "side": "BUY",
        "type": "MARKET",
        "origQty": "1.0",
        "executedQty": "0.4",
        "avgPrice": "50000.0",
        "cumQuote": "20000.0",
    }

    fill = await router.execute_market_entry(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        quantity=1.0,
        leverage=20,
        client_order_id="HA_E_PARTIAL",
        fill_timeout_seconds=0.05,
        poll_interval_seconds=0.01,
    )
    assert fill.is_partially_filled is True
    assert fill.executed_qty == 0.4
    assert fill.avg_price == 50000.0

  @pytest.mark.asyncio
  async def test_cancel_order_idempotency(self, mock_client):
    router = OrderRouter(mock_client)
    # Normal cancel
    res = await router.cancel_order("BTCUSDT", client_order_id="HA_SL_123")
    assert res["status"] == "CANCELED"

    # Unknown order code -2011 is absorbed
    mock_client.cancel_order.side_effect = BinanceAPIError(message="Unknown order sent.", code=-2011)
    res_absorb = await router.cancel_order("BTCUSDT", client_order_id="HA_SL_123")
    assert res_absorb["code"] == 200

    # Unrelated error is raised
    mock_client.cancel_order.side_effect = BinanceAPIError(message="An unknown error occured.", code=-1000)
    with pytest.raises(BinanceAPIError):
      await router.cancel_order("BTCUSDT", client_order_id="HA_SL_123")

  @pytest.mark.asyncio
  async def test_query_order_fill(self, mock_client):
    router = OrderRouter(mock_client)
    mock_client.query_order.return_value = {
        "symbol": "ETHUSDT",
        "orderId": 555,
        "clientOrderId": "HA_E_ETH",
        "status": "FILLED",
        "side": "SELL",
        "type": "MARKET",
        "origQty": "2.0",
        "executedQty": "2.0",
        "avgPrice": "3000.0",
        "cumQuote": "6000.0",
    }
    fill = await router.query_order_fill("ETHUSDT", client_order_id="HA_E_ETH")
    assert fill.symbol == "ETHUSDT"
    assert fill.status == OrderStatus.FILLED
    assert fill.side == OrderSide.SELL
    assert fill.avg_price == 3000.0
