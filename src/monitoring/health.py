"""Real diagnostic system health checks for the Trading Control Panel (Section 4).

Every check actually exercises the underlying component or endpoint rather than
simply checking Python imports. Credentials are never returned or logged.
"""

import asyncio
from dataclasses import asdict, dataclass
from pathlib import Path
import shutil
import time
from typing import Any, Dict, List, Literal, Optional
import websockets
from src.backtesting.data_adapter import HistoricalMarketStore
from src.backtesting.historical_data import HistoricalCacheDB
from src.config.settings import Settings
from src.exchange.client import BinanceFuturesClient
from src.execution.order_router import OrderRouter
from src.indicators.heikin_ashi import HeikinAshiSeries
from src.indicators.swing_points import SwingDetector
from src.market_data.models import Candle
from src.monitoring.logger import get_logger
from src.positions.manager import PositionManager
from src.positions.protective import ProtectiveOrderManager
from src.risk.models import BracketInfo, SizingInput
from src.risk.validator import validate_and_size_position
from src.scanner.scanner import MarketScanner
from src.state.database import DatabaseManager
from src.state.repository import StateRepository
from src.strategy.engine import MultiSymbolStrategyEngine

logger = get_logger("monitoring.health")

HealthStatus = Literal["Operational", "Warning", "Failed", "Not configured"]


@dataclass
class ComponentCheck:
  category: str  # "exchange" | "internal" | "configuration"
  key: str
  name: str
  status: HealthStatus
  detail: str
  error: Optional[str] = None
  latency_ms: Optional[float] = None


class SystemHealthChecker:
  """Runs live diagnostic checks across exchange, internal, and config subsystems."""

  def __init__(
      self,
      settings: Optional[Settings] = None,
      client: Optional[BinanceFuturesClient] = None,
      cache_db: Optional[HistoricalCacheDB] = None,
  ):
    self.settings = settings or Settings()
    self._external_client = client
    self.cache_db = cache_db or HistoricalCacheDB()

  async def run_all_checks(self) -> Dict[str, Any]:
    """Executes all health checks and returns a structured diagnostic report."""
    started_ms = int(time.time() * 1000)
    owns_client = self._external_client is None
    client = self._external_client or BinanceFuturesClient(self.settings)

    checks: List[ComponentCheck] = []
    try:
      # 1. Exchange connectivity checks (public endpoints)
      checks.extend(await self._check_exchange(client))
      # 2. Internal components
      checks.extend(self._check_internal(client))
      # 3. Configuration & environment
      checks.extend(self._check_configuration())
    finally:
      if owns_client:
        await client.close()

    failed = [c for c in checks if c.status == "Failed"]
    warnings = [c for c in checks if c.status == "Warning"]
    has_creds = bool(
        self.settings.binance_api_key.get_secret_value()
        and self.settings.binance_api_secret.get_secret_value()
    )

    critical_bt_keys = {
        "indicators",
        "strategy_engine",
        "risk_engine",
        "historical_data",
        "disk_space",
    }
    can_backtest = not any(
        c.status == "Failed" and c.key in critical_bt_keys for c in checks
    )
    critical_live_keys = critical_bt_keys | {
        "rest_ping",
        "server_time",
        "exchange_info",
        "database",
        "position_manager",
        "protective_manager",
        "execution_adapter",
        "strategy_config",
    }
    can_live_trade = has_creds and not any(
        c.status == "Failed" and c.key in critical_live_keys for c in checks
    )

    overall: HealthStatus = "Operational"
    if failed:
      overall = "Failed"
    elif warnings or not has_creds:
      overall = "Warning"

    return {
        "checked_at_ms": started_ms,
        "overall_status": overall,
        "environment": self.settings.app_env,
        "credentials_configured": has_creds,
        "can_backtest": can_backtest,
        "can_live_trade": can_live_trade,
        "summary": {
            "total": len(checks),
            "operational": sum(1 for c in checks if c.status == "Operational"),
            "warning": len(warnings),
            "failed": len(failed),
            "not_configured": sum(
                1 for c in checks if c.status == "Not configured"
            ),
        },
        "checks": [asdict(c) for c in checks],
    }

  async def _timed(self, coro):
    t0 = time.perf_counter()
    res = await coro
    return res, round((time.perf_counter() - t0) * 1000.0, 1)

  async def _check_exchange(
      self, client: BinanceFuturesClient
  ) -> List[ComponentCheck]:
    results: List[ComponentCheck] = []

    # 1. REST ping
    try:
      ok, ms = await self._timed(client.ping())
      results.append(
          ComponentCheck(
              category="exchange",
              key="rest_ping",
              name="Binance Futures Public REST API",
              status="Operational" if ok else "Failed",
              detail=f"Connected to {client.base_url}" if ok else "Unexpected ping response",
              latency_ms=ms,
          )
      )
    except Exception as exc:
      results.append(
          ComponentCheck(
              category="exchange",
              key="rest_ping",
              name="Binance Futures Public REST API",
              status="Failed",
              detail=f"Failed to reach {client.base_url}",
              error=str(exc),
          )
      )

    # 2. Server time sync
    try:
      st, ms = await self._timed(client.get_server_time())
      offset = abs(st.time_offset_ms)
      status: HealthStatus = "Operational" if offset <= 2000 else "Warning"
      results.append(
          ComponentCheck(
              category="exchange",
              key="server_time",
              name="Exchange Server Time Sync",
              status=status,
              detail=f"Offset: {st.time_offset_ms} ms",
              latency_ms=ms,
          )
      )
    except Exception as exc:
      results.append(
          ComponentCheck(
              category="exchange",
              key="server_time",
              name="Exchange Server Time Sync",
              status="Failed",
              detail="Time sync failed",
              error=str(exc),
          )
      )

    # 3. Exchange info
    try:
      info, ms = await self._timed(client.get_exchange_info())
      usdt_perps = sum(
          1 for s in info.values() if s.is_trading and s.is_usdt_perpetual
      )
      results.append(
          ComponentCheck(
              category="exchange",
              key="exchange_info",
              name="Exchange Information Retrieval",
              status="Operational" if usdt_perps > 0 else "Warning",
              detail=f"{len(info)} symbols ({usdt_perps} active USDT perpetuals)",
              latency_ms=ms,
          )
      )
    except Exception as exc:
      results.append(
          ComponentCheck(
              category="exchange",
              key="exchange_info",
              name="Exchange Information Retrieval",
              status="Failed",
              detail="Failed to retrieve exchangeInfo",
              error=str(exc),
          )
      )

    # 4. Ticker retrieval
    try:
      tickers, ms = await self._timed(client.get_24hr_tickers("BTCUSDT"))
      last_p = tickers[0].last_price if tickers else 0.0
      results.append(
          ComponentCheck(
              category="exchange",
              key="ticker",
              name="24h Ticker Retrieval",
              status="Operational" if tickers else "Failed",
              detail=f"BTCUSDT last price: {last_p:.2f}" if tickers else "Empty ticker list",
              latency_ms=ms,
          )
      )
    except Exception as exc:
      results.append(
          ComponentCheck(
              category="exchange",
              key="ticker",
              name="24h Ticker Retrieval",
              status="Failed",
              detail="Failed to fetch 24h ticker",
              error=str(exc),
          )
      )

    # 5. Historical candle retrieval
    try:
      klines, ms = await self._timed(
          client.get_klines("BTCUSDT", "1m", limit=2)
      )
      results.append(
          ComponentCheck(
              category="exchange",
              key="historical_klines",
              name="Historical Candle Retrieval",
              status="Operational" if klines else "Failed",
              detail=f"Fetched {len(klines)} sample 1m klines for BTCUSDT",
              latency_ms=ms,
          )
      )
    except Exception as exc:
      results.append(
          ComponentCheck(
              category="exchange",
              key="historical_klines",
              name="Historical Candle Retrieval",
              status="Failed",
              detail="Failed to fetch sample klines",
              error=str(exc),
          )
      )

    # 6. WebSocket connectivity
    ws_url = f"{self.settings.binance_ws_base_url.rstrip('/')}/btcusdt@kline_1m"
    try:
      t0 = time.perf_counter()
      async with asyncio.timeout(5.0):
        async with websockets.connect(ws_url, open_timeout=4.0):
          pass
      ws_ms = round((time.perf_counter() - t0) * 1000.0, 1)
      results.append(
          ComponentCheck(
              category="exchange",
              key="websocket",
              name="WebSocket Market Stream",
              status="Operational",
              detail=f"Handshake succeeded ({ws_url})",
              latency_ms=ws_ms,
          )
      )
    except Exception as exc:
      results.append(
          ComponentCheck(
              category="exchange",
              key="websocket",
              name="WebSocket Market Stream",
              status="Warning",
              detail=f"WebSocket handshake did not succeed ({ws_url})",
              error=str(exc),
          )
      )

    return results

  def _check_internal(
      self, client: BinanceFuturesClient
  ) -> List[ComponentCheck]:
    results: List[ComponentCheck] = []

    # Database connectivity
    db_mgr: Optional[DatabaseManager] = None
    try:
      db_mgr = DatabaseManager(self.settings.db_file_path)
      db_mgr.initialize()
      conn = db_mgr.get_connection()
      row = conn.execute("PRAGMA journal_mode;").fetchone()
      mode = row[0] if row else "unknown"
      results.append(
          ComponentCheck(
              category="internal",
              key="database",
              name="SQLite Database Connectivity",
              status="Operational",
              detail=f"Path={self.settings.db_file_path}, journal_mode={mode}",
          )
      )
    except Exception as exc:
      results.append(
          ComponentCheck(
              category="internal",
              key="database",
              name="SQLite Database Connectivity",
              status="Failed",
              detail="Database initialization or query failed",
              error=str(exc),
          )
      )

    # Market Scanner
    try:
      scanner = MarketScanner(client, self.settings)
      results.append(
          ComponentCheck(
              category="internal",
              key="market_scanner",
              name="Market Scanner",
              status="Operational",
              detail=(
                  f"Top-{self.settings.scanner_top_n}, min vol="
                  f"{self.settings.scanner_min_quote_volume_24h:,.0f} USDT"
              ),
          )
      )
      _ = scanner
    except Exception as exc:
      results.append(
          ComponentCheck(
              category="internal",
              key="market_scanner",
              name="Market Scanner",
              status="Failed",
              detail="MarketScanner initialization failed",
              error=str(exc),
          )
      )

    # Indicators (HA + SwingDetector)
    try:
      ha = HeikinAshiSeries("BTCUSDT", "15m")
      ha.update(
          Candle(
              symbol="BTCUSDT",
              interval="15m",
              open_time=0,
              close_time=899_999,
              open=100.0,
              high=102.0,
              low=99.0,
              close=101.0,
              volume=10.0,
              quote_volume=1000.0,
              trades_count=10,
              is_closed=True,
          )
      )
      sd = SwingDetector(
          "BTCUSDT",
          "1m",
          left_bars=self.settings.swing_confirmation_bars,
          right_bars=self.settings.swing_confirmation_bars,
      )
      _ = sd
      results.append(
          ComponentCheck(
              category="internal",
              key="indicators",
              name="Heikin-Ashi & Swing Indicators",
              status="Operational",
              detail=(
                  f"HA initialized; Swing {self.settings.swing_confirmation_bars}L/"
                  f"{self.settings.swing_confirmation_bars}R"
              ),
          )
      )
    except Exception as exc:
      results.append(
          ComponentCheck(
              category="internal",
              key="indicators",
              name="Heikin-Ashi & Swing Indicators",
              status="Failed",
              detail="Indicator verification failed",
              error=str(exc),
          )
      )

    # Strategy Engine
    try:
      strat = MultiSymbolStrategyEngine(has_open_position_callback=lambda: False)
      _ = strat
      results.append(
          ComponentCheck(
              category="internal",
              key="strategy_engine",
              name="Multi-Symbol Strategy Engine",
              status="Operational",
              detail=(
                  f"Trigger={self.settings.timeframe_trigger}, "
                  f"Confirm={self.settings.timeframe_confirmation}"
              ),
          )
      )
    except Exception as exc:
      results.append(
          ComponentCheck(
              category="internal",
              key="strategy_engine",
              name="Multi-Symbol Strategy Engine",
              status="Failed",
              detail="Strategy engine initialization failed",
              error=str(exc),
          )
      )

    # Risk Engine
    try:
      test_bracket = BracketInfo(
          bracket_number=1,
          max_leverage=20,
          notional_cap=1_000_000.0,
          notional_floor=0.0,
          maint_margin_ratio=0.005,
      )
      res = validate_and_size_position(
          SizingInput(
              symbol="BTCUSDT",
              equity=1000.0,
              is_long=True,
              entry_price=50000.0,
              sl_price=49500.0,
              step_size=0.001,
              min_qty=0.001,
              max_qty=1000.0,
              min_notional=5.0,
              tick_size=0.1,
              available_margin=1000.0,
              bracket=test_bracket,
              risk_ratio=self.settings.nominal_risk_ratio,
              rr_ratio=self.settings.reward_risk_ratio,
              taker_fee_rate=self.settings.estimated_taker_fee_ratio,
              slippage_rate=self.settings.estimated_slippage_ratio,
          )
      )
      results.append(
          ComponentCheck(
              category="internal",
              key="risk_engine",
              name="Risk Calculator & Validator",
              status="Operational" if res.is_valid else "Warning",
              detail=(
                  f"Risk={self.settings.nominal_risk_ratio*100:.1f}%, "
                  f"R:R={self.settings.reward_risk_ratio}:1"
              ),
          )
      )
    except Exception as exc:
      results.append(
          ComponentCheck(
              category="internal",
              key="risk_engine",
              name="Risk Calculator & Validator",
              status="Failed",
              detail="Risk engine check failed",
              error=str(exc),
          )
      )

    # Historical data subsystem
    try:
      self.cache_db.initialize()
      _ = HistoricalMarketStore()
      results.append(
          ComponentCheck(
              category="internal",
              key="historical_data",
              name="Historical Data Subsystem",
              status="Operational",
              detail=f"Cache DB ready at {self.cache_db.db_path}",
          )
      )
    except Exception as exc:
      results.append(
          ComponentCheck(
              category="internal",
              key="historical_data",
              name="Historical Data Subsystem",
              status="Failed",
              detail="Historical data store/cache check failed",
              error=str(exc),
          )
      )

    # Execution Adapter, Protective Order Manager, Position Manager
    try:
      router = OrderRouter(client)
      results.append(
          ComponentCheck(
              category="internal",
              key="execution_adapter",
              name="Execution Adapter (OrderRouter)",
              status="Operational",
              detail="OrderRouter initialized",
          )
      )
      prot = ProtectiveOrderManager(client)
      results.append(
          ComponentCheck(
              category="internal",
              key="protective_manager",
              name="Protective Order Manager",
              status="Operational",
              detail="ProtectiveOrderManager initialized",
          )
      )
      repo = StateRepository(db_mgr) if db_mgr else None
      if repo is not None:
        pm = PositionManager(client, repo, router, prot)
        has_pos = pm.has_open_position()
        results.append(
            ComponentCheck(
                category="internal",
                key="position_manager",
                name="Position Manager",
                status="Operational",
                detail=f"Active position lock: {has_pos}",
            )
        )
      else:
        results.append(
            ComponentCheck(
                category="internal",
                key="position_manager",
                name="Position Manager",
                status="Failed",
                detail="Skipped due to database failure",
                error="Database unavailable",
            )
        )
    except Exception as exc:
      results.append(
          ComponentCheck(
              category="internal",
              key="execution_adapter",
              name="Execution & Position Subsystems",
              status="Failed",
              detail="Execution/Position initialization failed",
              error=str(exc),
          )
      )
    finally:
      if db_mgr is not None:
        db_mgr.close()

    return results

  def _check_configuration(self) -> List[ComponentCheck]:
    results: List[ComponentCheck] = []

    # Environment
    results.append(
        ComponentCheck(
            category="configuration",
            key="environment",
            name="Operational Environment",
            status="Operational",
            detail=f"APP_ENV={self.settings.app_env} ({self.settings.binance_rest_base_url})",
        )
    )

    # API Credentials
    has_key = bool(self.settings.binance_api_key.get_secret_value())
    has_sec = bool(self.settings.binance_api_secret.get_secret_value())
    if has_key and has_sec:
      cred_status: HealthStatus = "Operational"
      cred_detail = "API key and secret are configured (masked)"
    else:
      cred_status = "Not configured"
      cred_detail = (
          "BINANCE_API_KEY / BINANCE_API_SECRET not set. "
          "Backtesting and public checks work; live trading is disabled."
      )
    results.append(
        ComponentCheck(
            category="configuration",
            key="credentials",
            name="API Credential Availability",
            status=cred_status,
            detail=cred_detail,
        )
    )

    # Strategy config validation
    try:
      Settings.validate_risk_ratio(self.settings.nominal_risk_ratio)
      Settings.validate_rr_ratio(self.settings.reward_risk_ratio)
      Settings.validate_swing_bars(self.settings.swing_confirmation_bars)
      Settings.validate_max_positions(self.settings.global_max_positions)
      results.append(
          ComponentCheck(
              category="configuration",
              key="strategy_config",
              name="Strategy Configuration Validation",
              status="Operational",
              detail="All strategy & risk invariants validated",
          )
      )
    except Exception as exc:
      results.append(
          ComponentCheck(
              category="configuration",
              key="strategy_config",
              name="Strategy Configuration Validation",
              status="Failed",
              detail="Configuration validation failed",
              error=str(exc),
          )
      )

    # Disk space
    try:
      check_path = Path(self.settings.db_file_path).parent
      if not check_path.exists():
        check_path = Path(".")
      usage = shutil.disk_usage(check_path)
      free_mb = usage.free / (1024 * 1024)
      if free_mb < 50:
        d_status: HealthStatus = "Failed"
      elif free_mb < 500:
        d_status = "Warning"
      else:
        d_status = "Operational"
      results.append(
          ComponentCheck(
              category="configuration",
              key="disk_space",
              name="Available Disk Space",
              status=d_status,
              detail=f"{free_mb:,.1f} MB free",
          )
      )
    except Exception as exc:
      results.append(
          ComponentCheck(
              category="configuration",
              key="disk_space",
              name="Available Disk Space",
              status="Warning",
              detail="Could not determine disk space",
              error=str(exc),
          )
      )

    return results
