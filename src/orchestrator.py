"""Main Trading Bot Orchestrator for Phase 12 — Full System Integration.

Wires together all 11 core domain layers into a cohesive event-driven pipeline:
  Scanner (60s) -> MarketData (15m/1m WS + REST) -> Heikin-Ashi Series ->
  Multi-Symbol Strategy Engine (15m Window + Concurrent 1m Confirmation) ->
  Swing Detector (5L/5R OHLC) -> Risk Management (10% Equity, 3R, Brackets) ->
  Order Router (Market Entry) -> Protective Order Manager (SL/TP & Sibling Cancel) ->
  Position Manager (Global 1-Position Lock) -> SQLite State & Startup Reconciliation.
"""

import asyncio
from collections import defaultdict
import json
from typing import Callable, Dict, List, Optional
from src.config.settings import Settings
from src.exchange.client import BinanceFuturesClient
from src.execution.client_id import OrderPrefix
from src.execution.models import OrderStatus
from src.execution.order_router import OrderRouter
from src.execution.user_stream import BinanceUserDataStream
from src.indicators.heikin_ashi import HeikinAshiCandle, HeikinAshiSeries
from src.indicators.momentum import compute_htf_trend_direction
from src.indicators.swing_points import SwingDetector, calculate_swing_stop_loss
from src.market_data.engine import MarketDataEngine
from src.market_data.models import CandleCloseEvent
from src.monitoring.logger import get_logger
from src.positions.manager import PositionManager
from src.positions.protective import ProtectiveOrderManager
from src.risk.brackets import get_bracket_for_notional
from src.risk.calculator import calculate_risk_budget, calculate_stop_distance
from src.risk.models import SizingInput, SizingResult
from src.risk.validator import validate_and_size_position
from src.scanner.models import ScannedSymbol, ScanResult
from src.scanner.momentum_scanner import MomentumScanner
from src.scanner.scanner import MarketScanner
from src.state.database import DatabaseManager
from src.state.reconciler import ReconciliationReport, StartupReconciler
from src.state.repository import StateRepository
from src.strategy.engine import MultiSymbolStrategyEngine
from src.strategy.models import Direction, TradeSignal

logger = get_logger("orchestrator")


class TradingBotOrchestrator:
  """Coordinates the complete trading bot lifecycle from startup to graceful shutdown."""

  def __init__(
      self,
      settings: Optional[Settings] = None,
      client: Optional[BinanceFuturesClient] = None,
      db_manager: Optional[DatabaseManager] = None,
      repository: Optional[StateRepository] = None,
      scanner: Optional[MarketScanner] = None,
      market_data: Optional[MarketDataEngine] = None,
      strategy_engine: Optional[MultiSymbolStrategyEngine] = None,
      order_router: Optional[OrderRouter] = None,
      protective_manager: Optional[ProtectiveOrderManager] = None,
      position_manager: Optional[PositionManager] = None,
      reconciler: Optional[StartupReconciler] = None,
      user_stream: Optional[BinanceUserDataStream] = None,
      batch_debounce_seconds: float = 0.25,
      position_watchdog_interval_seconds: float = 15.0,
  ):
    self.settings = settings or Settings()
    self.batch_debounce_seconds = batch_debounce_seconds
    self.position_watchdog_interval = position_watchdog_interval_seconds

    # Persistence layer
    self.db_manager = db_manager or DatabaseManager(self.settings.db_file_path)
    self.repository = repository or StateRepository(self.db_manager)

    # Exchange client
    self.client = client or BinanceFuturesClient(self.settings)

    # Execution & Position layers
    self.order_router = order_router or OrderRouter(self.client)
    self.protective_manager = protective_manager or ProtectiveOrderManager(
        self.client
    )
    self.position_manager = position_manager or PositionManager(
        exchange_client=self.client,
        repository=self.repository,
        order_router=self.order_router,
        protective_manager=self.protective_manager,
    )
    self.reconciler = reconciler or StartupReconciler(
        exchange_client=self.client,
        repository=self.repository,
        protective_manager=self.protective_manager,
        position_manager=self.position_manager,
    )

    # Market & Strategy layers
    if scanner is not None:
      self.scanner = scanner
    elif self.settings.strategy_name == "momentum_breakout":
      self.scanner = MomentumScanner(self.client, self.settings)
    else:
      self.scanner = MarketScanner(self.client, self.settings)
    self.market_data = market_data or MarketDataEngine(self.client, self.settings)
    is_momentum = self.settings.strategy_name == "momentum_breakout"

    def _htf_dir(sym: str) -> Optional[Direction]:
      candles_15m = self.market_data.get_candles(
          sym, self.settings.timeframe_trigger
      )
      d = compute_htf_trend_direction(candles_15m)
      if d == "LONG":
        return Direction.LONG
      if d == "SHORT":
        return Direction.SHORT
      return None

    self.strategy_engine = strategy_engine or MultiSymbolStrategyEngine(
        has_open_position_callback=self.position_manager.has_open_position,
        require_strong_ha=is_momentum,
        htf_direction_provider=_htf_dir if is_momentum else None,
    )
    self.user_stream = user_stream or BinanceUserDataStream(
        client=self.client,
        ws_base_url=self.settings.binance_ws_base_url,
    )

    # Per-symbol indicator series
    self._ha_15m: Dict[str, HeikinAshiSeries] = {}
    self._ha_1m: Dict[str, HeikinAshiSeries] = {}

    # Buffered 15m closes awaiting batch trigger construction
    self._pending_15m_ha: Dict[int, Dict[str, HeikinAshiCandle]] = defaultdict(dict)
    self._15m_flush_tasks: Dict[int, asyncio.Task] = {}
    self._1m_flush_tasks: Dict[int, asyncio.Task] = {}

    # Lifecycle tasks & events
    self._running = False
    self._stop_event = asyncio.Event()
    self._scanner_task: Optional[asyncio.Task] = None
    self._watchdog_task: Optional[asyncio.Task] = None
    self._started_components = False

    # Wire internal event subscriptions
    self.market_data.subscribe_candle_closed(self.on_candle_closed)
    self.strategy_engine.subscribe_signal(self.on_trade_signal)
    self.user_stream.register_order_update_handler(self.on_order_trade_update)

  # --------------------------------------------------------------------------
  # Startup & Recovery
  # --------------------------------------------------------------------------

  async def startup(
      self,
      verify_connectivity: bool = True,
      start_streams: bool = True,
  ) -> ReconciliationReport:
    """Initializes DB, synchronizes clock, reconciles exchange state, and seeds universe."""
    logger.info("Starting TradingBotOrchestrator (env=%s)...", self.settings.app_env)

    # 1. Initialize database
    self.db_manager.initialize()

    # 2. Verify connectivity and exchange settings
    if verify_connectivity:
      await self.client.synchronize_time()
      if self.settings.binance_api_key.get_secret_value():
        await self.client.test_connectivity_and_auth()
        # Enforce One-Way mode as required by specification
        try:
          mode_info = await self.client.get_position_mode()
          if mode_info.get("dualSidePosition", False):
            logger.info("Switching Binance Futures account to ONE-WAY position mode...")
            await self.client.set_position_mode(dual_side=False)
        except Exception as exc:
          logger.warning("Could not verify/set One-Way position mode: %s", exc)

    # 3. Reconcile state with exchange before touching market scanning
    report = await self.reconciler.reconcile_startup_state()
    logger.info("Startup reconciliation status: %s", report.status.value)
    self.repository.sync_live_trades_from_positions(
        environment=self.settings.app_env
    )

    # 4. Restore active strategy window if persisted and still valid
    active_win = self.repository.get_active_strategy_window()
    now_ms = self.client.get_synchronized_time()
    if active_win and active_win.get("end_timestamp", 0) > now_ms:
      payload = active_win.get("payload")
      if payload:
        self.strategy_engine.restore_state(payload)

    # 5. Initial market scan and historical pre-seeding
    scan_res = await self.scanner.scan()
    await self.sync_universe(
        [s.symbol for s in scan_res.symbols],
        start_ws=start_streams,
    )

    # 6. Start streams and background loops
    self._running = True
    self._stop_event.clear()
    if start_streams:
      if self.settings.binance_api_key.get_secret_value():
        self.user_stream.start()
      self.market_data.start()
      self._scanner_task = asyncio.create_task(
          self._scanner_refresh_loop(), name="OrchestratorScannerLoop"
      )
      self._watchdog_task = asyncio.create_task(
          self._position_watchdog_loop(), name="OrchestratorWatchdogLoop"
      )

    self._started_components = True
    logger.info(
        "TradingBotOrchestrator started cleanly with %d universe symbols.",
        len(scan_res.symbols),
    )
    return report

  def _ensure_ha_series(self, symbol: str) -> None:
    """Ensures Heikin-Ashi series exist and are pre-seeded for a symbol."""
    sym = symbol.upper()
    if sym not in self._ha_15m:
      s15 = HeikinAshiSeries(sym, self.settings.timeframe_trigger)
      candles_15m = self.market_data.get_candles(sym, self.settings.timeframe_trigger)
      if candles_15m:
        s15.load_from_candles(candles_15m)
      self._ha_15m[sym] = s15

    if sym not in self._ha_1m:
      s1 = HeikinAshiSeries(sym, self.settings.timeframe_confirmation)
      candles_1m = self.market_data.get_candles(sym, self.settings.timeframe_confirmation)
      if candles_1m:
        s1.load_from_candles(candles_1m)
      self._ha_1m[sym] = s1

  async def sync_universe(
      self,
      top_symbols: List[str],
      start_ws: bool = True,
  ) -> None:
    """Updates monitored symbols, retaining active window and open position symbols."""
    monitored = {s.upper() for s in top_symbols}

    # Protect in-flight confirmation window symbols
    win = self.strategy_engine.active_window
    if win and win.is_active:
      monitored.update(win.candidates.keys())

    # Protect active open position symbol
    cur_pos = self.position_manager.get_current_position()
    if cur_pos and cur_pos.is_active:
      monitored.add(cur_pos.symbol.upper())

    sorted_symbols = sorted(monitored)
    if start_ws:
      await self.market_data.set_monitored_universe(
          sorted_symbols,
          intervals=(
              self.settings.timeframe_confirmation,
              self.settings.timeframe_trigger,
          ),
      )
    else:
      # Still pre-seed history without starting websocket subscriptions
      tasks = [
          self.market_data.seed_symbol_history(
              sym,
              intervals=(
                  self.settings.timeframe_confirmation,
                  self.settings.timeframe_trigger,
              ),
          )
          for sym in sorted_symbols
      ]
      if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    # Seed HA state from newly loaded buffers
    for sym in sorted_symbols:
      self._ensure_ha_series(sym)

  # --------------------------------------------------------------------------
  # Candle Event Pipeline
  # --------------------------------------------------------------------------

  async def on_candle_closed(self, event: CandleCloseEvent) -> None:
    """Ingests closed candles from MarketDataEngine and updates HA + Strategy Engine."""
    candle = event.candle
    if not candle.is_closed:
      return

    sym = candle.symbol.upper()
    self._ensure_ha_series(sym)

    if candle.interval == self.settings.timeframe_trigger:  # 15m
      ha_15m = self._ha_15m[sym].update(candle)
      if ha_15m is not None:
        self._pending_15m_ha[candle.close_time][sym] = ha_15m
        self._schedule_15m_flush(candle.close_time)

    elif candle.interval == self.settings.timeframe_confirmation:  # 1m
      ha_1m = self._ha_1m[sym].update(candle)
      if ha_1m is not None:
        await self.strategy_engine.on_1m_candle_closed(event, ha_1m)
        self._schedule_1m_flush(candle.close_time)

  def _schedule_15m_flush(self, close_time_ms: int) -> None:
    """Schedules a short debounce flush to collect all symbols closing at the same 15m mark."""
    if close_time_ms in self._15m_flush_tasks:
      return

    async def _delayed():
      await asyncio.sleep(self.batch_debounce_seconds)
      self._15m_flush_tasks.pop(close_time_ms, None)
      await self.flush_15m_close(close_time_ms)

    self._15m_flush_tasks[close_time_ms] = asyncio.create_task(_delayed())

  def _schedule_1m_flush(self, close_time_ms: int) -> None:
    """Schedules a short debounce flush to arbitrate simultaneous 1m confirmations by rank."""
    if close_time_ms in self._1m_flush_tasks:
      return

    async def _delayed():
      await asyncio.sleep(self.batch_debounce_seconds)
      self._1m_flush_tasks.pop(close_time_ms, None)
      await self.flush_1m_close(close_time_ms)

    self._1m_flush_tasks[close_time_ms] = asyncio.create_task(_delayed())

  async def flush_15m_close(self, close_time_ms: int) -> None:
    """Creates a new global confirmation window from buffered 15m HA closes."""
    closed_15m_ha = self._pending_15m_ha.pop(close_time_ms, {})
    if not closed_15m_ha:
      return

    scan = self.scanner.last_scan
    snapshot: List[ScannedSymbol] = scan.symbols if scan else []
    if not snapshot:
      logger.warning(
          "15m close at %d ignored: no scanner snapshot available.", close_time_ms
      )
      return

    window = await self.strategy_engine.on_15m_candle_closed(
        symbols_snapshot=snapshot,
        closed_15m_ha=closed_15m_ha,
        timestamp_ms=close_time_ms,
    )
    if window:
      self.repository.save_strategy_window(
          window_id=window.window_id,
          start_ts=window.start_timestamp,
          end_ts=window.end_timestamp,
          status=window.status.value,
          winning_symbol=window.winning_symbol,
          window_consumed=window.window_consumed,
          payload_json=json.dumps(self.strategy_engine.serialize_state()),
      )

  async def flush_1m_close(self, close_time_ms: int) -> Optional[TradeSignal]:
    """Triggers deterministic rank-based arbitration for all confirmations at close_time_ms."""
    return await self.strategy_engine.flush_1m_timestamp_batch(close_time_ms)

  # --------------------------------------------------------------------------
  # Signal -> Swing -> Risk -> Execution Pipeline
  # --------------------------------------------------------------------------

  async def on_trade_signal(self, signal: TradeSignal) -> Optional[SizingResult]:
    """Processes a winning TradeSignal through Swing Detection, Risk Validation, and Execution."""
    logger.info(
        "Processing winning TradeSignal: %s [%s] (rank=%d, signal_id=%s)",
        signal.symbol,
        signal.direction.value,
        signal.candidate_rank,
        signal.signal_id,
    )

    # Enforce global 1-position guard
    if self.position_manager.has_open_position():
      logger.info(
          "Signal %s skipped: global position lock already active.", signal.signal_id
      )
      return None

    is_long = signal.direction == Direction.LONG

    # 1. Retrieve 1m OHLC market candles for Swing Detection
    candles_1m = self.market_data.get_candles(
        signal.symbol, self.settings.timeframe_confirmation
    )
    if not candles_1m:
      reason = f"No 1m OHLC market history available for {signal.symbol}."
      self.strategy_engine.handle_risk_validation_failure(
          signal.window_id, signal.symbol, reason
      )
      self._persist_window_state()
      return None

    latest_1m_candle = candles_1m[-1]
    entry_price = latest_1m_candle.close

    # 2. Detect latest confirmed 5L/5R swing pivot (never HA prices)
    detector = SwingDetector(
        symbol=signal.symbol,
        interval=self.settings.timeframe_confirmation,
        left_bars=self.settings.swing_confirmation_bars,
        right_bars=self.settings.swing_confirmation_bars,
    )
    pivot = (
        detector.get_latest_swing_low(candles_1m)
        if is_long
        else detector.get_latest_swing_high(candles_1m)
    )
    if pivot is None:
      reason = (
          f"No confirmed {self.settings.swing_confirmation_bars}L/"
          f"{self.settings.swing_confirmation_bars}R swing pivot found for {signal.symbol}."
      )
      self.strategy_engine.handle_risk_validation_failure(
          signal.window_id, signal.symbol, reason
      )
      self.repository.log_audit("SIGNAL_REJECTED", "SIGNAL", signal.signal_id, reason)
      self._persist_window_state()
      return None

    # 3. Calculate stop-loss with buffer
    sl_calc = calculate_swing_stop_loss(
        entry_price=entry_price,
        pivot=pivot,
        is_long=is_long,
        buffer_ratio=self.settings.swing_stop_buffer_ratio,
    )
    if not sl_calc.is_valid:
      reason = sl_calc.rejection_reason or "Invalid swing stop-loss calculation."
      self.strategy_engine.handle_risk_validation_failure(
          signal.window_id, signal.symbol, reason
      )
      self.repository.log_audit("SIGNAL_REJECTED", "SIGNAL", signal.signal_id, reason)
      self._persist_window_state()
      return None

    # 4. Query account balance, exchange filters, and leverage brackets
    try:
      account_info, filters, brackets_map = await asyncio.gather(
          self.client.get_account_info(),
          self.client.get_symbol_filters(signal.symbol),
          self.client.get_leverage_brackets(signal.symbol),
      )
    except Exception as exc:
      reason = f"Failed to fetch exchange account/filters/brackets: {exc}"
      logger.error("Signal %s aborted: %s", signal.signal_id, reason)
      self.strategy_engine.handle_risk_validation_failure(
          signal.window_id, signal.symbol, reason
      )
      self._persist_window_state()
      return None

    symbol_brackets = brackets_map.get(signal.symbol)
    if not symbol_brackets:
      reason = f"No leverage brackets returned for {signal.symbol}."
      self.strategy_engine.handle_risk_validation_failure(
          signal.window_id, signal.symbol, reason
      )
      self._persist_window_state()
      return None

    # 5. Match leverage bracket to estimated position notional and validate sizing
    equity = account_info.total_margin_balance
    risk_budget = calculate_risk_budget(equity, self.settings.nominal_risk_ratio)
    stop_dist = calculate_stop_distance(entry_price, sl_calc.stop_price, is_long)
    est_notional = (risk_budget / stop_dist) * entry_price
    bracket = get_bracket_for_notional(symbol_brackets, est_notional)
    if bracket is None:
      reason = f"Could not resolve leverage bracket for {signal.symbol}."
      self.strategy_engine.handle_risk_validation_failure(
          signal.window_id, signal.symbol, reason
      )
      self._persist_window_state()
      return None

    sizing_input = SizingInput(
        symbol=signal.symbol,
        equity=equity,
        is_long=is_long,
        entry_price=entry_price,
        sl_price=sl_calc.stop_price,
        step_size=filters.step_size,
        min_qty=filters.min_qty,
        max_qty=filters.max_qty,
        min_notional=filters.min_notional,
        tick_size=filters.tick_size,
        available_margin=account_info.available_balance,
        bracket=bracket,
        risk_ratio=self.settings.nominal_risk_ratio,
        rr_ratio=self.settings.reward_risk_ratio,
        taker_fee_rate=self.settings.estimated_taker_fee_ratio,
        slippage_rate=self.settings.estimated_slippage_ratio,
    )
    sizing_res = validate_and_size_position(sizing_input)

    # Re-resolve bracket against final truncated notional if bracket tier changed
    if sizing_res.is_valid:
      final_notional = sizing_res.final_quantity * entry_price
      final_bracket = get_bracket_for_notional(symbol_brackets, final_notional)
      if (
          final_bracket is not None
          and final_bracket.bracket_number != bracket.bracket_number
      ):
        sizing_input = SizingInput(
            symbol=signal.symbol,
            equity=equity,
            is_long=is_long,
            entry_price=entry_price,
            sl_price=sl_calc.stop_price,
            step_size=filters.step_size,
            min_qty=filters.min_qty,
            max_qty=filters.max_qty,
            min_notional=filters.min_notional,
            tick_size=filters.tick_size,
            available_margin=account_info.available_balance,
            bracket=final_bracket,
            risk_ratio=self.settings.nominal_risk_ratio,
            rr_ratio=self.settings.reward_risk_ratio,
            taker_fee_rate=self.settings.estimated_taker_fee_ratio,
            slippage_rate=self.settings.estimated_slippage_ratio,
        )
        sizing_res = validate_and_size_position(sizing_input)

    if not sizing_res.is_valid:
      reason = sizing_res.rejection_reason or "Risk validation rejected trade."
      self.strategy_engine.handle_risk_validation_failure(
          signal.window_id, signal.symbol, reason
      )
      self.repository.log_audit("SIGNAL_REJECTED", "SIGNAL", signal.signal_id, reason)
      self._persist_window_state()
      return sizing_res

    # 6. Execute market entry + place SL/TP protective orders via PositionManager
    try:
      opened_pos = await self.position_manager.open_position(
          signal=signal,
          sizing=sizing_res,
          tick_size=filters.tick_size,
          step_size=filters.step_size,
      )
      self.repository.upsert_live_trade_from_position(
          opened_pos.position_id,
          environment=self.settings.app_env,
          signal_timestamp=signal.confirmation_timestamp,
      )
      self._persist_window_state()
    except Exception as exc:
      logger.error(
          "Position opening failed for %s: %s", signal.symbol, exc, exc_info=True
      )
      self.repository.log_audit(
          "EXECUTION_FAILED", "SIGNAL", signal.signal_id, str(exc)
      )

    return sizing_res

  def _persist_window_state(self) -> None:
    """Persists active confirmation window state into SQLite."""
    win = self.strategy_engine.active_window
    if win:
      self.repository.save_strategy_window(
          window_id=win.window_id,
          start_ts=win.start_timestamp,
          end_ts=win.end_timestamp,
          status=win.status.value,
          winning_symbol=win.winning_symbol,
          window_consumed=win.window_consumed,
          payload_json=json.dumps(self.strategy_engine.serialize_state()),
      )

  # --------------------------------------------------------------------------
  # User Data Events & Watchdog
  # --------------------------------------------------------------------------

  async def on_order_trade_update(self, payload: dict) -> None:
    """Processes real-time ORDER_TRADE_UPDATE WebSocket notifications for exit fills."""
    o = payload.get("o", {})
    status = str(o.get("X", "")).upper()
    if status != OrderStatus.FILLED.value:
      return

    cid = str(o.get("c", ""))
    order_id = int(o.get("i", 0))
    symbol = str(o.get("s", "")).upper()
    avg_price = float(o.get("ap", 0.0) or o.get("L", 0.0))
    executed_qty = float(o.get("z", 0.0) or o.get("l", 0.0))

    cur_pos = self.position_manager.get_current_position()
    if cur_pos is None or cur_pos.symbol != symbol:
      return

    pair = cur_pos.protective_pair
    is_exit_order = (
        (pair and (pair.is_sl_order(cid, order_id) or pair.is_tp_order(cid, order_id)))
        or cid.startswith(OrderPrefix.STOP_LOSS.value)
        or cid.startswith(OrderPrefix.TAKE_PROFIT.value)
    )

    if is_exit_order:
      logger.info(
          "Detected protective exit FILL on %s (%s @ %.4f). Coordinating closure...",
          symbol,
          cid,
          avg_price,
      )
      try:
        pos_id = cur_pos.position_id
        await self.position_manager.handle_exit_fill(
            triggered_client_order_id=cid or None,
            triggered_order_id=order_id or None,
            exit_price=avg_price if avg_price > 0 else None,
            exit_qty=executed_qty if executed_qty > 0 else None,
        )
        self.repository.upsert_live_trade_from_position(
            pos_id, environment=self.settings.app_env
        )
      except Exception as exc:
        logger.error("Failed handling exit fill for %s: %s", symbol, exc)

  async def _scanner_refresh_loop(self) -> None:
    """Refreshes Top-20 universe every configured interval and syncs market data streams."""
    interval = float(self.settings.scanner_refresh_interval_sec)
    while not self._stop_event.is_set():
      try:
        await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
        break
      except asyncio.TimeoutError:
        try:
          scan_res: ScanResult = await self.scanner.scan()
          await self.sync_universe([s.symbol for s in scan_res.symbols], start_ws=True)
        except Exception as exc:
          logger.warning("Periodic scanner refresh error: %s", exc)

  async def _position_watchdog_loop(self) -> None:
    """Periodic safety fallback: reconciles position if WS missed an exit fill."""
    while not self._stop_event.is_set():
      try:
        await asyncio.wait_for(
            self._stop_event.wait(), timeout=self.position_watchdog_interval
        )
        break
      except asyncio.TimeoutError:
        cur_pos = self.position_manager.get_current_position()
        if cur_pos is None or not cur_pos.is_active:
          continue
        try:
          ex_positions = await self.client.get_positions(cur_pos.symbol)
          is_still_open = any(p.symbol == cur_pos.symbol and p.is_open for p in ex_positions)
          if not is_still_open and cur_pos.protective_pair:
            # Query SL / TP on exchange to find which one filled
            sl_q = await self.client.query_order(
                cur_pos.symbol,
                client_order_id=cur_pos.protective_pair.sl_client_order_id,
            )
            tp_q = await self.client.query_order(
                cur_pos.symbol,
                client_order_id=cur_pos.protective_pair.tp_client_order_id,
            )
            if sl_q.get("status") == "FILLED":
              pos_id = cur_pos.position_id
              await self.position_manager.handle_exit_fill(
                  triggered_client_order_id=cur_pos.protective_pair.sl_client_order_id,
                  exit_price=float(sl_q.get("avgPrice", 0.0) or 0.0) or None,
              )
              self.repository.upsert_live_trade_from_position(
                  pos_id, environment=self.settings.app_env
              )
            elif tp_q.get("status") == "FILLED":
              pos_id = cur_pos.position_id
              await self.position_manager.handle_exit_fill(
                  triggered_client_order_id=cur_pos.protective_pair.tp_client_order_id,
                  exit_price=float(tp_q.get("avgPrice", 0.0) or 0.0) or None,
              )
              self.repository.upsert_live_trade_from_position(
                  pos_id, environment=self.settings.app_env
              )
        except Exception as exc:
          logger.debug("Watchdog check failed: %s", exc)

  # --------------------------------------------------------------------------
  # Main Event Loop & Shutdown
  # --------------------------------------------------------------------------

  async def run_forever(self) -> None:
    """Blocks until shutdown signal is triggered."""
    await self._stop_event.wait()

  def request_shutdown(self) -> None:
    """Signals the orchestrator to begin graceful shutdown."""
    self._stop_event.set()

  async def shutdown(self) -> None:
    """Gracefully cancels background tasks, closes streams, client, and DB."""
    if not self._running and not self._started_components:
      return
    logger.info("Shutting down TradingBotOrchestrator cleanly...")
    self._running = False
    self._stop_event.set()

    for task in list(self._15m_flush_tasks.values()) + list(
        self._1m_flush_tasks.values()
    ):
      task.cancel()
    self._15m_flush_tasks.clear()
    self._1m_flush_tasks.clear()

    for bg_task in (self._scanner_task, self._watchdog_task):
      if bg_task:
        bg_task.cancel()
        try:
          await bg_task
        except (asyncio.CancelledError, Exception):
          pass
    self._scanner_task = None
    self._watchdog_task = None

    await self.scanner.stop_periodic_scan()
    await self.market_data.stop()
    await self.user_stream.stop()
    await self.client.close()
    self.db_manager.close()
    self._started_components = False
    logger.info("TradingBotOrchestrator shutdown complete.")
