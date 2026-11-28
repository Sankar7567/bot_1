"""Integration & unit tests for the Trading Control Panel (Phases 3-7)."""

import asyncio
from http.server import ThreadingHTTPServer
import json
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
from pydantic import SecretStr
import pytest
from src.backtesting.historical_data import HistoricalCacheDB
from src.backtesting.service import BacktestService
from src.config.settings import Settings
from src.exchange.models import (
    AccountInfo,
    ServerTimeResponse,
    SymbolFilter,
    SymbolInfo,
    Ticker24hr,
)
from src.live_controller import LiveTradingController, PRODUCTION_CONFIRMATION_PHRASE
from src.main import parse_args
from src.monitoring.health import SystemHealthChecker
from src.state.database import DatabaseManager
from src.state.reconciler import ReconciliationReport, ReconciliationStatus
from src.state.repository import LiveTradeRecord, PositionRecord, StateRepository
from src.web.server import ControlPanelApp, make_handler


def _make_mock_client():
  client = MagicMock()
  client.base_url = "https://testnet.binancefuture.com"
  client.ping = AsyncMock(return_value=True)
  client.get_server_time = AsyncMock(
      return_value=ServerTimeResponse(
          server_time=1_700_000_000_000,
          local_time=1_700_000_000_050,
          time_offset_ms=-50,
      )
  )
  client.get_exchange_info = AsyncMock(
      return_value={
          "BTCUSDT": SymbolInfo(
              symbol="BTCUSDT",
              status="TRADING",
              base_asset="BTC",
              quote_asset="USDT",
              contract_type="PERPETUAL",
              filters=SymbolFilter(
                  symbol="BTCUSDT",
                  tick_size=0.1,
                  step_size=0.001,
                  min_qty=0.001,
                  max_qty=1000.0,
                  min_notional=5.0,
                  price_precision=1,
                  quantity_precision=3,
              ),
          )
      }
  )
  client.get_24hr_tickers = AsyncMock(
      return_value=[
          Ticker24hr(
              symbol="BTCUSDT",
              price_change=100.0,
              price_change_percent=2.5,
              weighted_avg_price=50000.0,
              last_price=50100.0,
              last_qty=0.1,
              open_price=49000.0,
              high_price=50500.0,
              low_price=48900.0,
              volume=1000.0,
              quote_volume=50_000_000.0,
              open_time=0,
              close_time=1000,
              first_id=1,
              last_id=10,
              count=10,
          )
      ]
  )
  client.get_klines = AsyncMock(return_value=[[0, "100", "101", "99", "100.5", "10", 59999, "1000", 5]])
  client.close = AsyncMock()
  return client


@pytest.mark.integration
class TestSystemHealthChecker:

  @pytest.mark.asyncio
  async def test_health_check_without_credentials_allows_backtest_disables_live(self, tmp_path):
    settings = Settings(app_env="testnet", db_file_path=tmp_path / "bot.db")
    cache = HistoricalCacheDB(tmp_path / "hist.db")
    client = _make_mock_client()

    with patch("src.monitoring.health.websockets.connect", side_effect=OSError("offline")):
      checker = SystemHealthChecker(settings=settings, client=client, cache_db=cache)
      report = await checker.run_all_checks()

    assert report["credentials_configured"] is False
    assert report["can_backtest"] is True
    assert report["can_live_trade"] is False
    cred_check = next(c for c in report["checks"] if c["key"] == "credentials")
    assert cred_check["status"] == "Not configured"
    cache.close()

  @pytest.mark.asyncio
  async def test_health_check_reports_failed_exchange(self, tmp_path):
    settings = Settings(
        app_env="testnet",
        db_file_path=tmp_path / "bot.db",
        binance_api_key=SecretStr("test_key"),
        binance_api_secret=SecretStr("test_secret"),
    )
    cache = HistoricalCacheDB(tmp_path / "hist.db")
    client = _make_mock_client()
    client.ping = AsyncMock(side_effect=RuntimeError("DNS failure"))

    with patch("src.monitoring.health.websockets.connect", side_effect=OSError("offline")):
      checker = SystemHealthChecker(settings=settings, client=client, cache_db=cache)
      report = await checker.run_all_checks()

    assert report["overall_status"] == "Failed"
    assert report["can_live_trade"] is False
    ping_check = next(c for c in report["checks"] if c["key"] == "rest_ping")
    assert ping_check["status"] == "Failed"
    assert "DNS failure" in ping_check["error"]
    cache.close()


@pytest.mark.integration
class TestLiveTradingControllerSafety:

  @pytest.mark.asyncio
  async def test_missing_credentials_refuses_start(self, tmp_path):
    settings = Settings(app_env="testnet", db_file_path=tmp_path / "bot.db")
    ctrl = LiveTradingController(settings=settings)

    status = await ctrl.get_status()
    assert status["live_enabled"] is False
    assert status["credentials_configured"] is False

    with pytest.raises(PermissionError, match="not configured"):
      await ctrl.start_trading(target_env="testnet")

  @pytest.mark.asyncio
  async def test_production_safety_gates(self, tmp_path):
    settings = Settings(
        app_env="testnet",
        db_file_path=tmp_path / "bot.db",
        binance_api_key=SecretStr("k"),
        binance_api_secret=SecretStr("s"),
    )
    ctrl = LiveTradingController(settings=settings)

    with pytest.raises(PermissionError, match="ENABLE LIVE TRADING"):
      await ctrl.start_trading(target_env="production", confirm_production=False)

    with pytest.raises(PermissionError, match="ENABLE LIVE TRADING"):
      await ctrl.start_trading(
          target_env="production",
          confirm_production=True,
          confirmation_phrase="wrong phrase",
      )

  @pytest.mark.asyncio
  async def test_testnet_start_duplicate_prevention_and_stop(self, tmp_path):
    settings = Settings(
        app_env="testnet",
        db_file_path=tmp_path / "bot.db",
        binance_api_key=SecretStr("k"),
        binance_api_secret=SecretStr("s"),
    )
    mock_health = MagicMock()
    mock_health.run_all_checks = AsyncMock(
        return_value={"can_live_trade": True, "checks": []}
    )

    stop_evt = asyncio.Event()
    mock_orch = MagicMock()
    mock_orch.startup = AsyncMock(
        return_value=MagicMock(status=ReconciliationStatus.CLEAN)
    )
    mock_orch.run_forever = AsyncMock(side_effect=stop_evt.wait)
    mock_orch.request_shutdown = MagicMock(side_effect=stop_evt.set)
    mock_orch.shutdown = AsyncMock()
    mock_orch.position_manager = MagicMock()
    mock_orch.position_manager.get_current_position.return_value = None
    mock_orch.client = MagicMock()
    mock_orch.client.get_account_info = AsyncMock(
        return_value=AccountInfo(
            fee_tier=0,
            can_trade=True,
            can_deposit=True,
            can_withdraw=False,
            total_margin_balance=500.0,
            total_wallet_balance=500.0,
            total_unrealized_profit=0.0,
            available_balance=500.0,
        )
    )

    ctrl = LiveTradingController(
        settings=settings,
        orchestrator_factory=lambda _: mock_orch,
        health_checker_factory=lambda _: mock_health,
    )

    st = await ctrl.start_trading(target_env="testnet")
    assert st["state"] == "running"
    assert st["account_balance"] == 500.0

    with pytest.raises(RuntimeError, match="already running"):
      await ctrl.start_trading(target_env="testnet")

    st2 = await ctrl.stop_trading()
    assert st2["state"] == "stopped"
    mock_orch.shutdown.assert_awaited()


@pytest.mark.integration
class TestLiveTradePersistenceAndRecovery:

  def test_live_trades_survive_restart_and_export_csv(self, tmp_path):
    db_path = tmp_path / "bot.db"
    db1 = DatabaseManager(db_path)
    db1.initialize()
    repo1 = StateRepository(db1)

    now = int(time.time() * 1000)
    repo1.save_position(
        PositionRecord(
            position_id="pos_BTCUSDT_1",
            symbol="BTCUSDT",
            side="BUY",
            status="CLOSED",
            quantity=0.01,
            entry_price=50000.0,
            exit_price=51500.0,
            leverage=20,
            stop_loss_price=49500.0,
            take_profit_price=51500.0,
            realized_pnl=15.0,
            exit_reason="TAKE_PROFIT",
            created_at=now - 60_000,
            updated_at=now,
            closed_at=now,
        )
    )
    synced = repo1.sync_live_trades_from_positions(environment="testnet")
    assert synced == 1
    db1.close()

    # Re-open DB (simulates app/computer restart)
    db2 = DatabaseManager(db_path)
    db2.initialize()
    repo2 = StateRepository(db2)
    trades = repo2.list_live_trades()
    assert len(trades) == 1
    assert trades[0].symbol == "BTCUSDT"
    assert trades[0].net_pnl == 15.0
    csv_data = repo2.export_live_trades_csv()
    assert "pos_BTCUSDT_1" in csv_data
    assert "TAKE_PROFIT" in csv_data
    db2.close()


@pytest.mark.integration
class TestControlPanelHTTP:

  def test_cli_ui_args_and_http_endpoints(self, tmp_path):
    args = parse_args(["--ui", "--port", "9091"])
    assert args.ui is True
    assert args.port == 9091

    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()

    settings = Settings(app_env="testnet", db_file_path=tmp_path / "bot.db")
    cache = HistoricalCacheDB(tmp_path / "hist.db")
    bt_svc = BacktestService(settings=settings, cache_db=cache)
    checker = SystemHealthChecker(
        settings=settings, client=_make_mock_client(), cache_db=cache
    )
    app = ControlPanelApp(
        settings=settings,
        loop=loop,
        backtest_service=bt_svc,
        health_checker=checker,
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    port = httpd.server_address[1]
    srv_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    srv_thread.start()

    try:
      base = f"http://127.0.0.1:{port}"
      r_html = httpx.get(f"{base}/")
      assert r_html.status_code == 200
      assert "Binance HA Bot" in r_html.text

      r_def = httpx.get(f"{base}/api/backtest/defaults")
      assert r_def.status_code == 200
      assert r_def.json()["default_initial_balance"] == 100.0

      r_live = httpx.get(f"{base}/api/live/status")
      assert r_live.status_code == 200
      assert r_live.json()["live_enabled"] is False

      r_start_live = httpx.post(
          f"{base}/api/live/start", json={"target_env": "testnet"}
      )
      assert r_start_live.status_code == 403

      with patch("src.monitoring.health.websockets.connect", side_effect=OSError("offline")):
        r_health = httpx.post(f"{base}/api/health/run", json={})
      assert r_health.status_code == 200
      assert r_health.json()["can_backtest"] is True

      r_trades = httpx.get(f"{base}/api/live/trades")
      assert r_trades.status_code == 200
      assert r_trades.json()["trades"] == []
    finally:
      httpd.shutdown()
      httpd.server_close()
      loop.call_soon_threadsafe(loop.stop)
      t.join(timeout=2.0)
      cache.close()
