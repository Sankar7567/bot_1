"""Phase 14: Fault Injection & Resilience Integration Tests.

Verifies system behavior under simulated disconnects, WebSocket drops,
listenKey expirations, missed exit fills, scanner failures, and partial
order-execution failures.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from websockets.exceptions import ConnectionClosed
from websockets.frames import Close

from src.config.settings import Settings
from src.exchange.models import PositionRisk
from src.execution.client_id import OrderPrefix, generate_client_order_id
from src.execution.exceptions import ProtectiveOrderError
from src.execution.order_router import OrderRouter
from src.execution.user_stream import BinanceUserDataStream
from src.orchestrator import TradingBotOrchestrator
from src.positions.manager import PositionManager
from src.positions.models import PositionState
from src.positions.protective import ProtectiveOrderManager
from src.risk.models import SizingResult
from src.state.database import DatabaseManager
from src.state.repository import StateRepository
from src.strategy.models import Direction, TradeSignal


class _FakeWebSocket:
  """Minimal async context-manager WebSocket stub for fault injection."""

  def __init__(self, messages):
    self._messages = list(messages)
    self._idx = 0

  async def __aenter__(self):
    return self

  async def __aexit__(self, exc_type, exc, tb):
    return False

  async def recv(self):
    if self._idx >= len(self._messages):
      raise ConnectionClosed(Close(1006, "simulated drop"), None)
    item = self._messages[self._idx]
    self._idx += 1
    if isinstance(item, Exception):
      raise item
    return json.dumps(item)


class TestUserDataStreamFaultInjection:
  """Verifies BinanceUserDataStream reconnect, keepalive, and error isolation."""

  @pytest.mark.asyncio
  async def test_process_event_isolates_handler_error_and_handles_expiry(self):
    client = MagicMock()
    stream = BinanceUserDataStream(client=client)

    bad_handler = AsyncMock(side_effect=RuntimeError("boom"))
    good_handler = AsyncMock()
    stream.register_order_update_handler(bad_handler)
    stream.register_order_update_handler(good_handler)

    payload = {"e": "ORDER_TRADE_UPDATE", "o": {"s": "BTCUSDT", "X": "FILLED"}}
    await stream.process_event(payload)

    bad_handler.assert_awaited_once_with(payload)
    good_handler.assert_awaited_once_with(payload)

    # listenKeyExpired should be handled without raising
    await stream.process_event({"e": "listenKeyExpired"})

  @pytest.mark.asyncio
  async def test_keepalive_loop_survives_failures_and_stops(self):
    client = MagicMock()
    client.keepalive_listen_key = AsyncMock(
        side_effect=[RuntimeError("temp network"), None]
    )
    stream = BinanceUserDataStream(client=client, keepalive_interval_seconds=0.01)

    task = asyncio.create_task(stream._keepalive_loop())
    await asyncio.sleep(0.05)
    stream._stop_event.set()
    await asyncio.wait_for(task, timeout=1.0)

    assert client.keepalive_listen_key.await_count >= 2

  @pytest.mark.asyncio
  async def test_run_loop_reconnects_on_drop_and_listen_key_expired(self):
    client = MagicMock()
    client.create_listen_key = AsyncMock(side_effect=["lk_1", "lk_2", "lk_3"])
    client.keepalive_listen_key = AsyncMock(return_value=None)
    client.close_listen_key = AsyncMock(return_value=None)

    received = []

    async def on_update(payload):
      received.append(payload)

    stream = BinanceUserDataStream(
        client=client,
        keepalive_interval_seconds=10.0,
        reconnect_delay_seconds=0.01,
        max_reconnect_delay_seconds=0.02,
    )
    stream.register_order_update_handler(on_update)

    ws1 = _FakeWebSocket([
        {"e": "ORDER_TRADE_UPDATE", "o": {"s": "ETHUSDT", "X": "FILLED"}},
        {"e": "listenKeyExpired"},
    ])
    ws2 = _FakeWebSocket([
        ConnectionClosed(Close(1001, "going away"), None),
    ])
    ws3 = _FakeWebSocket([
        {"e": "ORDER_TRADE_UPDATE", "o": {"s": "BTCUSDT", "X": "FILLED"}},
    ])

    call_idx = 0

    def fake_connect(*args, **kwargs):
      nonlocal call_idx
      call_idx += 1
      if call_idx == 1:
        return ws1
      if call_idx == 2:
        return ws2
      if call_idx == 3:
        return ws3
      stream._stop_event.set()
      return _FakeWebSocket([])

    with patch("src.execution.user_stream.websockets.connect", side_effect=fake_connect):
      stream.start()
      # Duplicate start is a no-op
      stream.start()
      await asyncio.sleep(0.12)
      await stream.stop()

    assert len(received) == 2
    assert received[0]["o"]["s"] == "ETHUSDT"
    assert received[1]["o"]["s"] == "BTCUSDT"
    assert client.create_listen_key.await_count >= 3
    client.close_listen_key.assert_awaited()


class TestOrchestratorFaultInjection:
  """Verifies orchestrator watchdog recovery, scanner resilience, and protective failure rollback."""

  @pytest.mark.asyncio
  async def test_watchdog_recovers_missed_tp_exit_fill(self):
    settings = Settings(app_env="testnet", db_file_path=":memory:")
    db = DatabaseManager(":memory:")
    db.initialize()
    repo = StateRepository(db)

    client = MagicMock()
    client.get_symbol_filters = AsyncMock()
    client.change_leverage = AsyncMock(return_value=True)
    client.change_margin_type = AsyncMock(return_value=True)

    entry_cid = generate_client_order_id(OrderPrefix.ENTRY)
    sl_cid = generate_client_order_id(OrderPrefix.STOP_LOSS)
    tp_cid = generate_client_order_id(OrderPrefix.TAKE_PROFIT)

    # Mock OrderRouter & ProtectiveOrderManager outcomes via client.place_order
    order_seq = {"n": 0}

    async def fake_place_order(params: dict):
      order_seq["n"] += 1
      otype = params["type"]
      qty = float(params["quantity"])
      if otype == "MARKET":
        return {
            "orderId": 1001,
            "clientOrderId": params.get("newClientOrderId", entry_cid),
            "symbol": params["symbol"],
            "status": "FILLED",
            "executedQty": str(qty),
            "avgPrice": "100.0",
            "cumQuote": str(100.0 * qty),
        }
      cid = sl_cid if otype == "STOP_MARKET" else tp_cid
      return {
          "orderId": 1000 + order_seq["n"],
          "clientOrderId": params.get("newClientOrderId", cid),
          "symbol": params["symbol"],
          "status": "NEW",
          "stopPrice": str(params["stopPrice"]),
      }

    client.place_order = AsyncMock(side_effect=fake_place_order)
    client.cancel_order = AsyncMock(return_value={"status": "CANCELED"})
    client.query_order = AsyncMock(
        side_effect=lambda symbol, client_order_id=None, order_id=None: {
            "symbol": symbol,
            "clientOrderId": client_order_id,
            "status": "NEW",
            "avgPrice": "0.0",
        }
    )
    client.get_positions = AsyncMock(
        return_value=[
            PositionRisk(
                symbol="BTCUSDT",
                position_amt=0.0,
                entry_price=0.0,
                mark_price=110.0,
                unrealized_profit=0.0,
                liquidation_price=0.0,
                leverage=5,
                margin_type="isolated",
                position_side="BOTH",
            )
        ]
    )

    router = OrderRouter(client)
    pm_prot = ProtectiveOrderManager(client)
    pos_mgr = PositionManager(client, repo, router, pm_prot)

    sig = TradeSignal(
        signal_id="SIG_TEST_1",
        symbol="BTCUSDT",
        direction=Direction.LONG,
        candidate_rank=1,
        trigger_15m_timestamp=1700000000000,
        confirmation_1m_timestamp=1700000060000,
        trigger_price=100.0,
        confirmation_price=100.5,
        window_id="WIN_TEST",
        created_at=1700000060000,
        expires_at=1700000900000,
    )
    sizing = SizingResult(
        is_valid=True,
        rejection_reason=None,
        symbol="BTCUSDT",
        is_long=True,
        equity=1000.0,
        risk_budget=100.0,
        stop_distance=5.0,
        raw_quantity=2.0,
        friction_adjusted_qty=2.0,
        final_quantity=2.0,
        entry_price=100.0,
        sl_price=95.0,
        tp_price=115.0,
        leverage=5,
        required_margin=40.0,
        estimated_liq_price=80.0,
        maint_margin_ratio=0.005,
    )
    pos = await pos_mgr.open_position(sig, sizing, tick_size=0.1, step_size=0.001)
    assert pos_mgr.has_open_position() is True

    # Simulate missed WS fill: exchange reports TP order FILLED, SL order NEW
    async def fake_query(symbol, client_order_id=None, order_id=None):
      if client_order_id == pos.protective_pair.tp_client_order_id:
        return {"status": "FILLED", "avgPrice": "115.0"}
      return {"status": "NEW", "avgPrice": "0.0"}

    client.query_order = AsyncMock(side_effect=fake_query)

    orch = TradingBotOrchestrator(
        settings=settings,
        client=client,
        db_manager=db,
        repository=repo,
        position_manager=pos_mgr,
        position_watchdog_interval_seconds=0.01,
    )

    task = asyncio.create_task(orch._position_watchdog_loop())
    await asyncio.sleep(0.06)
    orch.request_shutdown()
    await asyncio.wait_for(task, timeout=1.0)

    assert pos_mgr.has_open_position() is False
    closed_rec = repo.get_position(pos.position_id)
    assert closed_rec is not None
    assert closed_rec.status == PositionState.CLOSED.value
    assert closed_rec.exit_reason == "TAKE_PROFIT"
    db.close()

  @pytest.mark.asyncio
  async def test_protective_order_failure_triggers_emergency_close_and_unlocks(self):
    db = DatabaseManager(":memory:")
    db.initialize()
    repo = StateRepository(db)

    client = MagicMock()
    client.change_leverage = AsyncMock(return_value=True)
    client.change_margin_type = AsyncMock(return_value=True)
    client.cancel_order = AsyncMock(return_value={"status": "CANCELED"})
    client.get_positions = AsyncMock(return_value=[])

    async def fake_place_order(params: dict):
      if params["type"] == "MARKET":
        qty = float(params["quantity"])
        return {
            "orderId": 2001,
            "clientOrderId": params.get("newClientOrderId", "ENT_1"),
            "symbol": params["symbol"],
            "status": "FILLED",
            "executedQty": str(qty),
            "avgPrice": "100.0",
            "cumQuote": str(100.0 * qty),
        }
      raise RuntimeError("Exchange rejected protective order")

    client.place_order = AsyncMock(side_effect=fake_place_order)

    router = OrderRouter(client)
    pm_prot = ProtectiveOrderManager(client)
    pos_mgr = PositionManager(client, repo, router, pm_prot)

    sig = TradeSignal(
        signal_id="SIG_ERR_1",
        symbol="ETHUSDT",
        direction=Direction.LONG,
        candidate_rank=1,
        trigger_15m_timestamp=1700000000000,
        confirmation_1m_timestamp=1700000060000,
        trigger_price=100.0,
        confirmation_price=100.5,
        window_id="WIN_ERR",
        created_at=1700000060000,
        expires_at=1700000900000,
    )
    sizing = SizingResult(
        is_valid=True,
        rejection_reason=None,
        symbol="ETHUSDT",
        is_long=True,
        equity=1000.0,
        risk_budget=100.0,
        stop_distance=5.0,
        raw_quantity=2.0,
        friction_adjusted_qty=2.0,
        final_quantity=2.0,
        entry_price=100.0,
        sl_price=95.0,
        tp_price=115.0,
        leverage=5,
        required_margin=40.0,
        estimated_liq_price=80.0,
        maint_margin_ratio=0.005,
    )

    with pytest.raises(ProtectiveOrderError):
      await pos_mgr.open_position(sig, sizing, tick_size=0.1, step_size=0.001)

    # Lock must be released and DB must not have an open position
    assert pos_mgr.has_open_position() is False
    assert repo.get_open_position() is None
    db.close()

  @pytest.mark.asyncio
  async def test_on_order_trade_update_ignores_non_filled_and_foreign_events(self):
    settings = Settings(app_env="testnet", db_file_path=":memory:")
    db = DatabaseManager(":memory:")
    db.initialize()
    repo = StateRepository(db)
    client = MagicMock()
    orch = TradingBotOrchestrator(
        settings=settings, client=client, db_manager=db, repository=repo
    )

    # Non-FILLED event
    await orch.on_order_trade_update({"o": {"X": "NEW", "s": "BTCUSDT"}})
    # FILLED event when no open position exists
    await orch.on_order_trade_update(
        {"o": {"X": "FILLED", "s": "BTCUSDT", "c": "SL_123", "i": 99}}
    )
    assert orch.position_manager.has_open_position() is False
    db.close()
