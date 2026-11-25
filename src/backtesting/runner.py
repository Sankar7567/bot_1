"""Chronological Backtest & Live-Trading Replay Runner (Amendment 01 §6.1).

Reuses the exact shared live trading modules:
  MarketScanner -> MarketDataEngine buffers -> HeikinAshiSeries ->
  MultiSymbolStrategyEngine -> SwingDetector -> Risk Validator ->
  OrderRouter -> ProtectiveOrderManager -> PositionManager
connected to SimulatedClock, HistoricalMarketStore, and HistoricalExchangeAdapter.
"""

import time
from typing import Dict, List, Optional
from src.backtesting.clock import SimulatedClock
from src.backtesting.data_adapter import HistoricalMarketStore
from src.backtesting.exec_adapter import (
    ExitSimulationResult,
    HistoricalExchangeAdapter,
)
from src.backtesting.ledger import AccountLedger
from src.backtesting.metrics import build_backtest_report
from src.backtesting.models import (
    BacktestConfig,
    BacktestReport,
    MarketSelectionMetrics,
    StrategyMetrics,
    TradeAuditRecord,
)
from src.config.settings import Settings
from src.execution.order_router import OrderRouter
from src.indicators.heikin_ashi import HeikinAshiCandle, HeikinAshiSeries
from src.indicators.swing_points import SwingDetector, calculate_swing_stop_loss
from src.market_data.engine import MarketDataEngine
from src.market_data.models import Candle, CandleCloseEvent
from src.monitoring.logger import get_logger
from src.positions.manager import PositionManager
from src.positions.protective import ProtectiveOrderManager
from src.risk.brackets import get_bracket_for_notional
from src.risk.calculator import calculate_risk_budget, calculate_stop_distance
from src.risk.models import SizingInput
from src.risk.validator import validate_and_size_position
from src.scanner.exceptions import EmptyUniverseError, StaleDataError
from src.scanner.models import ScannedSymbol
from src.scanner.scanner import MarketScanner
from src.state.database import DatabaseManager
from src.state.repository import StateRepository
from src.strategy.engine import MultiSymbolStrategyEngine
from src.strategy.models import CandidateStatus, Direction, TradeSignal

logger = get_logger("backtesting.runner")


class ChronologicalBacktestRunner:
  """Executes a deterministic forward-walking chronological replay of the live trading system."""

  def __init__(
      self,
      store: HistoricalMarketStore,
      config: BacktestConfig,
      settings: Optional[Settings] = None,
  ):
    self.store = store
    self.config = config
    self.settings = settings or Settings(app_env="testnet", db_file_path=":memory:")

    self.clock = SimulatedClock(
        initial_time_ms=max(0, config.start_time_ms - config.warmup_ms)
    )
    self.ledger = AccountLedger(initial_balance=config.initial_balance)
    self.adapter = HistoricalExchangeAdapter(
        clock=self.clock,
        store=self.store,
        ledger=self.ledger,
        assumptions=config.execution,
        min_24h_coverage_ratio=config.min_24h_coverage_ratio,
    )

    # In-memory SQLite state repository for shared PositionManager
    self.db_manager = DatabaseManager(":memory:")
    self.db_manager.initialize()
    self.repository = StateRepository(self.db_manager)

    # Shared live components wired to HistoricalExchangeAdapter
    self.scanner = MarketScanner(client=self.adapter, settings=self.settings)
    self.market_data = MarketDataEngine(client=self.adapter, settings=self.settings)
    self.order_router = OrderRouter(exchange_client=self.adapter)
    self.protective_manager = ProtectiveOrderManager(exchange_client=self.adapter)
    self.position_manager = PositionManager(
        exchange_client=self.adapter,
        repository=self.repository,
        order_router=self.order_router,
        protective_manager=self.protective_manager,
    )
    self.strategy_engine = MultiSymbolStrategyEngine(
        has_open_position_callback=self.position_manager.has_open_position
    )

    # Per-symbol HA series
    self._ha_1m: Dict[str, HeikinAshiSeries] = {}
    self._ha_15m: Dict[str, HeikinAshiSeries] = {}

    # Metrics & audit tracking
    self._market_metrics = MarketSelectionMetrics(
        eligible_contracts_count=len(self.store.get_all_symbols())
    )
    self._strategy_metrics = StrategyMetrics()
    self._trades: List[TradeAuditRecord] = []
    self._active_audit: Optional[TradeAuditRecord] = None
    self._last_scan_ts: int = -1
    self._seen_universe_symbols = set()

  def _ensure_ha(self, symbol: str) -> None:
    sym = symbol.upper()
    if sym not in self._ha_1m:
      self._ha_1m[sym] = HeikinAshiSeries(sym, "1m")
    if sym not in self._ha_15m:
      self._ha_15m[sym] = HeikinAshiSeries(sym, "15m")

  async def run(self) -> BacktestReport:
    """Runs the full chronological forward-walking simulation across warmup + backtest range."""
    sim_start = max(0, self.config.start_time_ms - self.config.warmup_ms)
    sim_end = self.config.end_time_ms
    all_symbols = self.store.get_all_symbols()

    # Pre-index close_time -> list of (symbol, candle) for O(1) chronological replay
    bars_1m_by_close: Dict[int, List[Candle]] = {}
    bars_15m_by_close: Dict[int, List[Candle]] = {}
    all_ts = set()

    for sym in all_symbols:
      self._ensure_ha(sym)
      for c in self.store.get_klines_up_to(sym, "1m", sim_end, limit=1_000_000):
        if sim_start <= c.close_time <= sim_end:
          bars_1m_by_close.setdefault(c.close_time, []).append(c)
          all_ts.add(c.close_time)
      for c in self.store.get_klines_up_to(sym, "15m", sim_end, limit=1_000_000):
        if sim_start <= c.close_time <= sim_end:
          bars_15m_by_close.setdefault(c.close_time, []).append(c)
          all_ts.add(c.close_time)

    # Sort timestamps deterministically
    sorted_timestamps = sorted(all_ts)

    for ts in sorted_timestamps:
      self.clock.advance_to(ts)
      in_eval_window = ts >= self.config.start_time_ms

      # ----------------------------------------------------------------------
      # 1. Check active position funding & SL/TP triggers on newly closed 1m bar
      # ----------------------------------------------------------------------
      closed_1m = bars_1m_by_close.get(ts, [])
      for c1 in closed_1m:
        self.adapter.apply_funding_if_due(c1)
        sub_bars = self.store.get_sub_candles_for_bar(
            c1.symbol, c1.open_time, c1.close_time
        )
        exit_sim = self.adapter.evaluate_bar_triggers(c1, sub_bars=sub_bars)
        if exit_sim is not None:
          await self._handle_simulated_exit(exit_sim)

      # ----------------------------------------------------------------------
      # 2. Ingest closed 1m candles into MarketDataEngine buffers & HA series
      # ----------------------------------------------------------------------
      newly_closed_1m_ha: Dict[str, tuple[Candle, HeikinAshiCandle]] = {}
      for c1 in closed_1m:
        self.market_data.get_buffer(c1.symbol, "1m").add_candle(c1)
        ha1 = self._ha_1m[c1.symbol].update(c1)
        if ha1 is not None:
          newly_closed_1m_ha[c1.symbol] = (c1, ha1)

      # ----------------------------------------------------------------------
      # 3. Scanner Refresh (every 60s in chronological UTC time)
      # ----------------------------------------------------------------------
      if (
          self._last_scan_ts < 0
          or (ts - self._last_scan_ts) >= self.config.scanner_refresh_interval_ms
      ):
        try:
          scan_res = await self.scanner.scan()
          self._last_scan_ts = ts
          if in_eval_window:
            for s in scan_res.symbols:
              self._seen_universe_symbols.add(s.symbol)
              self._market_metrics.top_20_appearances[s.symbol] = (
                  self._market_metrics.top_20_appearances.get(s.symbol, 0) + 1
              )
        except (EmptyUniverseError, StaleDataError):
          # Insufficient 24h warm-up data yet; do not fabricate rankings
          pass

      # ----------------------------------------------------------------------
      # 4. 15-Minute Trigger Window Creation (using frozen latest Top-20)
      # ----------------------------------------------------------------------
      closed_15m = bars_15m_by_close.get(ts, [])
      if closed_15m:
        closed_15m_ha: Dict[str, HeikinAshiCandle] = {}
        for c15 in closed_15m:
          self.market_data.get_buffer(c15.symbol, "15m").add_candle(c15)
          ha15 = self._ha_15m[c15.symbol].update(c15)
          if ha15 is not None:
            closed_15m_ha[c15.symbol] = ha15

        # Count expired candidates from previous window if inside eval period
        if in_eval_window and self.strategy_engine.active_window:
          prev_w = self.strategy_engine.active_window
          for cand in prev_w.candidates.values():
            if cand.status == CandidateStatus.MONITORING:
              self._strategy_metrics.expired_candidates += 1

        if in_eval_window and self.scanner.last_scan is not None:
          snapshot: List[ScannedSymbol] = self.scanner.last_scan.symbols
          # Only pass 15m HA candles for symbols that are in the top-20 snapshot
          # (i.e. listed and eligible at the trigger timestamp).  Symbols that
          # were not in the scanner result at this 15m boundary — because they
          # were not yet listed, were delisted, or failed eligibility — must be
          # excluded so that mid-window entrants cannot create candidates.
          snapshot_symbols = {s.symbol for s in snapshot}
          gated_15m_ha = {
              sym: ha for sym, ha in closed_15m_ha.items()
              if sym in snapshot_symbols
          }
          win = await self.strategy_engine.on_15m_candle_closed(
              symbols_snapshot=snapshot,
              closed_15m_ha=gated_15m_ha,
              timestamp_ms=ts,
          )
          if win:
            for cand in win.candidates.values():
              self._market_metrics.candidate_counts_by_symbol[cand.symbol] = (
                  self._market_metrics.candidate_counts_by_symbol.get(cand.symbol, 0)
                  + 1
              )
              self._market_metrics.candidate_counts_by_direction[
                  cand.direction.value
              ] += 1

      # ----------------------------------------------------------------------
      # 5. Concurrent 1-Minute Confirmation Monitoring (only inside eval interval)
      # ----------------------------------------------------------------------
      if in_eval_window and newly_closed_1m_ha:
        for sym, (c1, ha1) in newly_closed_1m_ha.items():
          ev = CandleCloseEvent(
              symbol=sym, interval="1m", candle=c1, emitted_at=ts
          )
          await self.strategy_engine.on_1m_candle_closed(ev, ha1)

        # Check if any candidates confirmed at `ts` while a position is open
        win = self.strategy_engine.active_window
        if win and win.is_active and self.position_manager.has_open_position():
          pending = self.strategy_engine._pending_confirmations.get(ts, [])
          if pending:
            self._strategy_metrics.trades_skipped_open_position += 1

        # Deterministic rank-based batch arbitration
        pending_batch = list(
            self.strategy_engine._pending_confirmations.get(ts, [])
        )
        if len(pending_batch) > 1:
          self._strategy_metrics.tie_breaking_events += 1

        signal = await self.strategy_engine.flush_1m_timestamp_batch(ts)
        if signal is not None:
          await self._handle_winning_signal(
              signal,
              tie_break_symbols=[c.symbol for c in pending_batch],
          )

    self._market_metrics.historical_universe_size = len(self._seen_universe_symbols)

    report = build_backtest_report(
        config=self.config,
        run_timestamp_ms=int(time.time() * 1000),
        data_quality=self.store.quality_report,
        market_selection=self._market_metrics,
        strategy=self._strategy_metrics,
        ledger=self.ledger,
        trades=self._trades,
    )
    self.db_manager.close()
    return report

  async def _handle_winning_signal(
      self,
      signal: TradeSignal,
      tie_break_symbols: List[str],
  ) -> None:
    """Validates Swing + Risk on historical data and executes simulated entry."""
    self._strategy_metrics.total_signals += 1
    self._strategy_metrics.confirmed_signals += 1

    if self.position_manager.has_open_position():
      self._strategy_metrics.trades_skipped_open_position += 1
      return

    is_long = signal.direction == Direction.LONG
    candles_1m = self.market_data.get_candles(signal.symbol, "1m")
    if not candles_1m:
      self.strategy_engine.handle_risk_validation_failure(
          signal.window_id, signal.symbol, "No 1m OHLC candles"
      )
      self._strategy_metrics.rejected_signals += 1
      self._strategy_metrics.risk_validation_failures += 1
      return

    signal_price = candles_1m[-1].close
    detector = SwingDetector(
        symbol=signal.symbol,
        interval="1m",
        left_bars=self.settings.swing_confirmation_bars,
        right_bars=self.settings.swing_confirmation_bars,
    )
    pivot = (
        detector.get_latest_swing_low(candles_1m)
        if is_long
        else detector.get_latest_swing_high(candles_1m)
    )
    if pivot is None:
      self.strategy_engine.handle_risk_validation_failure(
          signal.window_id, signal.symbol, "No confirmed 5L/5R swing pivot"
      )
      self._strategy_metrics.rejected_signals += 1
      self._strategy_metrics.risk_validation_failures += 1
      return

    sl_calc = calculate_swing_stop_loss(
        entry_price=signal_price,
        pivot=pivot,
        is_long=is_long,
        buffer_ratio=self.settings.swing_stop_buffer_ratio,
    )
    if not sl_calc.is_valid:
      self.strategy_engine.handle_risk_validation_failure(
          signal.window_id, signal.symbol, sl_calc.rejection_reason or "Invalid SL"
      )
      self._strategy_metrics.rejected_signals += 1
      self._strategy_metrics.risk_validation_failures += 1
      return

    account_info = await self.adapter.get_account_info()
    filters = await self.adapter.get_symbol_filters(signal.symbol)
    brackets_map = await self.adapter.get_leverage_brackets(signal.symbol)
    symbol_brackets = brackets_map.get(signal.symbol)
    if not symbol_brackets:
      self.strategy_engine.handle_risk_validation_failure(
          signal.window_id, signal.symbol, "Missing leverage brackets"
      )
      self._strategy_metrics.rejected_signals += 1
      self._strategy_metrics.risk_validation_failures += 1
      return

    equity = account_info.total_margin_balance
    risk_budget = calculate_risk_budget(equity, self.settings.nominal_risk_ratio)
    stop_dist = calculate_stop_distance(signal_price, sl_calc.stop_price, is_long)
    est_notional = (risk_budget / stop_dist) * signal_price
    bracket = get_bracket_for_notional(symbol_brackets, est_notional)
    if bracket is None:
      self.strategy_engine.handle_risk_validation_failure(
          signal.window_id, signal.symbol, "No leverage bracket matched"
      )
      self._strategy_metrics.rejected_signals += 1
      self._strategy_metrics.risk_validation_failures += 1
      return

    sizing_res = validate_and_size_position(
        SizingInput(
            symbol=signal.symbol,
            equity=equity,
            is_long=is_long,
            entry_price=signal_price,
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
            taker_fee_rate=self.config.execution.taker_fee_rate,
            slippage_rate=self.config.execution.entry_slippage_bps / 10000.0,
        )
    )
    if not sizing_res.is_valid:
      self.strategy_engine.handle_risk_validation_failure(
          signal.window_id,
          signal.symbol,
          sizing_res.rejection_reason or "Risk validation rejected",
      )
      self._strategy_metrics.rejected_signals += 1
      self._strategy_metrics.risk_validation_failures += 1
      return

    try:
      pos = await self.position_manager.open_position(
          signal=signal,
          sizing=sizing_res,
          tick_size=filters.tick_size,
          step_size=filters.step_size,
      )
    except Exception as exc:
      logger.warning("Simulated entry failed for %s: %s", signal.symbol, exc)
      self.strategy_engine.handle_risk_validation_failure(
          signal.window_id, signal.symbol, str(exc)
      )
      self._strategy_metrics.rejected_signals += 1
      return

    win = self.strategy_engine.active_window
    cand = win.get_candidate(signal.symbol) if win else None
    entry_meta = self.adapter.last_entry_meta or {}

    self._active_audit = TradeAuditRecord(
        trade_id=pos.position_id,
        symbol=pos.symbol,
        direction=signal.direction.value,
        candidate_rank=signal.candidate_rank,
        window_id=signal.window_id,
        snapshot_symbols=list(win.candidates.keys()) if win else [],
        trigger_15m_timestamp=signal.trigger_15m_timestamp,
        trigger_15m_ha_open=(
            cand.trigger_15m_candle.ha_open if cand else signal.trigger_price
        ),
        trigger_15m_ha_close=(
            cand.trigger_15m_candle.ha_close if cand else signal.trigger_price
        ),
        candidate_created_at=cand.registered_at if cand else signal.trigger_15m_timestamp,
        monitored_1m_bars_count=(
            max(1, (signal.confirmation_1m_timestamp - signal.trigger_15m_timestamp) // 60000)
        ),
        confirmation_1m_timestamp=signal.confirmation_1m_timestamp,
        tie_break_candidates=tie_break_symbols,
        pivot_price=pivot.price,
        pivot_timestamp=pivot.pivot_timestamp,
        pivot_confirmed_at=pivot.confirmation_timestamp,
        signal_price=signal_price,
        order_submitted_at=entry_meta.get("order_submitted_at", signal.confirmation_1m_timestamp),
        fill_timestamp=entry_meta.get("fill_timestamp", signal.confirmation_1m_timestamp),
        fill_price=pos.entry_price,
        quantity=pos.quantity,
        leverage=pos.leverage,
        isolated_margin=entry_meta.get("isolated_margin", 0.0),
        sl_price=pos.stop_loss_price,
        tp_price=pos.take_profit_price,
        entry_fee=entry_meta.get("entry_fee", 0.0),
        spread_cost=entry_meta.get("spread_cost", 0.0),
        slippage_cost=entry_meta.get("slippage_cost", 0.0),
    )

  async def _handle_simulated_exit(self, exit_sim: ExitSimulationResult) -> None:
    """Closes PositionManager state and completes the TradeAuditRecord."""
    await self.position_manager.handle_exit_fill(
        triggered_client_order_id=exit_sim.triggered_client_order_id,
        triggered_order_id=exit_sim.triggered_order_id,
        exit_price=exit_sim.fill_price,
    )

    if self._active_audit is not None:
      rec = self._active_audit
      rec.exit_timestamp = exit_sim.fill_timestamp_ms
      rec.exit_reason = exit_sim.exit_reason
      rec.exit_trigger_price = exit_sim.trigger_price
      rec.exit_fill_price = exit_sim.fill_price
      rec.ambiguity_resolution_note = exit_sim.ambiguity_note
      rec.gross_pnl = exit_sim.gross_pnl
      rec.exit_fee = exit_sim.exit_fee
      rec.funding_paid = exit_sim.funding_paid
      rec.spread_cost += exit_sim.spread_cost
      rec.slippage_cost += exit_sim.slippage_cost
      rec.net_pnl = (
          exit_sim.gross_pnl
          - rec.entry_fee
          - exit_sim.exit_fee
          - exit_sim.funding_paid
      )
      rec.equity_after_trade = self.ledger.wallet_balance
      self._trades.append(rec)
      self._active_audit = None
