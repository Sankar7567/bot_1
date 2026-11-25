"""Unit tests for Phase 12 — User Data Stream, CLI & Orchestrator Helpers."""

import asyncio
import json
from pathlib import Path
import time
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from src.config.settings import Settings
from src.exchange.client import BinanceFuturesClient
from src.exchange.models import (
    AccountInfo,
    LeverageBracketTier,
    SymbolFilter,
    SymbolInfo,
    SymbolLeverageBrackets,
    Ticker24hr,
)
from src.execution.user_stream import BinanceUserDataStream
from src.main import main as cli_main
from src.main import run_bot
from src.orchestrator import TradingBotOrchestrator
from src.scanner.models import ScannedSymbol, ScanResult
from src.state.database import DatabaseManager
from src.strategy.models import Direction, TradeSignal


class TestListenKeyAndUserStream:
  """Tests for listenKey REST helpers and User Data Stream event parsing."""

  def test_build_stream_url_strips_trailing_ws(self):
    client = MagicMock(spec=BinanceFuturesClient)
    us = BinanceUserDataStream(
        client=client, ws_base_url="wss://stream.binancefuture.com/ws"
    )
    assert (
        us._build_stream_url("abc_listen_key")
        == "wss://stream.binancefuture.com/ws/abc_listen_key"
    )

  @pytest.mark.asyncio
  async def test_process_order_trade_update_dispatches(self):
    client = MagicMock(spec=BinanceFuturesClient)
    us = BinanceUserDataStream(client=client)
    received = []

    async def handler(payload: dict):
      received.append(payload)

    us.register_order_update_handler(handler)

    await us.process_event({
        "e": "ORDER_TRADE_UPDATE",
        "o": {"s": "BTCUSDT", "X": "FILLED", "c": "HA_TP_123"},
    })
    assert len(received) == 1
    assert received[0]["o"]["c"] == "HA_TP_123"

    # Non-order event should not trigger order handlers
    await us.process_event({"e": "ACCOUNT_UPDATE"})
    assert len(received) == 1


class TestOrchestratorUnit:
  """Unit tests for Orchestrator startup, universe protection, and CLI check."""

  def test_cli_check_config_exits_zero(self):
    rc = cli_main(["--check-config"])
    assert rc == 0

  @pytest.mark.asyncio
  async def test_sync_universe_protects_open_position_symbol(self, tmp_path: Path):
    settings = Settings(
        app_env="testnet",
        db_file_path=tmp_path / "orch_test.db",
    )
    client = MagicMock(spec=BinanceFuturesClient)
    client.get_synchronized_time = MagicMock(return_value=1700000000000)
    client.get_klines = AsyncMock(return_value=[])
    client.get_positions = AsyncMock(return_value=[])
    client.get_open_orders = AsyncMock(return_value=[])
    client.close = AsyncMock()

    orch = TradingBotOrchestrator(settings=settings, client=client)
    orch.db_manager.initialize()

    # Mock an open position in SOLUSDT even though top-20 only has BTCUSDT
    mock_pos = MagicMock()
    mock_pos.is_active = True
    mock_pos.symbol = "SOLUSDT"
    orch.position_manager.set_current_position(mock_pos)

    await orch.sync_universe(["BTCUSDT"], start_ws=False)

    assert "BTCUSDT" in orch._ha_15m
    assert "SOLUSDT" in orch._ha_15m
    assert "SOLUSDT" in orch._ha_1m

    await orch.shutdown()

  @pytest.mark.asyncio
  async def test_graceful_shutdown_idempotent(self, tmp_path: Path):
    settings = Settings(
        app_env="testnet",
        db_file_path=tmp_path / "orch_shutdown.db",
    )
    client = MagicMock(spec=BinanceFuturesClient)
    client.get_synchronized_time = MagicMock(return_value=1700000000000)
    client.close = AsyncMock()

    orch = TradingBotOrchestrator(settings=settings, client=client)
    # Shutdown before startup should be a clean no-op
    await orch.shutdown()

    # Initialize and mark started
    orch.db_manager.initialize()
    orch._started_components = True
    orch._running = True
    await orch.shutdown()
    client.close.assert_awaited_once()

  @pytest.mark.asyncio
  async def test_listen_key_rest_endpoints(self):
    settings = Settings(
        app_env="testnet",
        binance_api_key="test_key_12345678",
        binance_api_secret="test_sec_12345678",
    )
    client = BinanceFuturesClient(settings)
    client._request = AsyncMock(
        side_effect=[
            {"listenKey": "lk_abc_123"},
            {},
            {},
        ]
    )

    lk = await client.create_listen_key()
    assert lk == "lk_abc_123"
    await client.keepalive_listen_key()
    await client.close_listen_key()
    assert client._request.await_count == 3
    await client.close()

  @pytest.mark.asyncio
  async def test_startup_verifies_connectivity_and_sets_one_way_mode(
      self, tmp_path: Path
  ):
    now_ms = int(time.time() * 1000)
    settings = Settings(
        app_env="testnet",
        binance_api_key="test_key_12345678",
        binance_api_secret="test_sec_12345678",
        db_file_path=tmp_path / "orch_conn.db",
    )
    client = MagicMock(spec=BinanceFuturesClient)
    client.get_synchronized_time = MagicMock(return_value=now_ms)
    client.synchronize_time = AsyncMock(return_value=0)
    client.test_connectivity_and_auth = AsyncMock(return_value=True)
    client.get_position_mode = AsyncMock(return_value={"dualSidePosition": True})
    client.set_position_mode = AsyncMock(return_value={"code": 200})
    client.get_positions = AsyncMock(return_value=[])
    client.get_open_orders = AsyncMock(return_value=[])
    filters = SymbolFilter("BTCUSDT", 0.01, 0.001, 0.001, 1000.0, 5.0, 2, 3)
    client.get_exchange_info = AsyncMock(
        return_value={"BTCUSDT": SymbolInfo("BTCUSDT", "TRADING", "BTC", "USDT", "PERPETUAL", filters)}
    )
    client.get_24hr_tickers = AsyncMock(
        return_value=[
            Ticker24hr("BTCUSDT", 5.0, 5.0, 100.0, 100.0, 1.0, 95.0, 101.0, 94.0, 1e5, 1e7, 0, now_ms, 1, 2, 2)
        ]
    )
    client.get_klines = AsyncMock(return_value=[])
    client.close = AsyncMock()

    orch = TradingBotOrchestrator(settings=settings, client=client)
    orch.db_manager.initialize()
    # Save active strategy window to test restoration
    orch.repository.save_strategy_window(
        window_id="win_saved",
        start_ts=now_ms - 60000,
        end_ts=now_ms + 600000,
        status="ACTIVE",
        payload_json=json.dumps({
            "window_id": "win_saved",
            "start_timestamp": now_ms - 60000,
            "end_timestamp": now_ms + 600000,
            "status": "ACTIVE",
            "is_slot_claimed": False,
            "winning_symbol": None,
            "window_consumed": False,
            "candidates": {},
        }),
    )

    await orch.startup(verify_connectivity=True, start_streams=False)
    client.set_position_mode.assert_awaited_once_with(dual_side=False)
    assert orch.strategy_engine.active_window is not None
    assert orch.strategy_engine.active_window.window_id == "win_saved"
    await orch.shutdown()

  @pytest.mark.asyncio
  async def test_run_bot_clean_exit(self, tmp_path: Path):
    settings = Settings(
        app_env="testnet",
        db_file_path=tmp_path / "run_bot.db",
        log_file=tmp_path / "bot.log",
    )
    with patch("src.main.load_settings", return_value=settings):
      with patch("src.main.TradingBotOrchestrator") as mock_orch_cls:
        mock_orch = MagicMock()
        mock_orch.startup = AsyncMock()
        mock_orch.run_forever = AsyncMock()
        mock_orch.shutdown = AsyncMock()
        mock_orch_cls.return_value = mock_orch

        await run_bot(verify_connectivity=False)
        mock_orch.startup.assert_awaited_once()
        mock_orch.run_forever.assert_awaited_once()
        mock_orch.shutdown.assert_awaited_once()

