# Backtesting Manual

Implemented in [src/backtesting/](../src/backtesting/__init__.py) following **Amendment 01 (§6.1)**.

---

## 1. Overview & Non-Lookahead Design
The backtesting engine (`ChronologicalBacktestRunner`) is a **chronological live-trading replay**, not a retrospective signal finder. It wires the exact shared live modules (`MarketScanner`, `MarketDataEngine`, `HeikinAshiSeries`, `MultiSymbolStrategyEngine`, `SwingDetector`, `validate_and_size_position`, `OrderRouter`, `ProtectiveOrderManager`, `PositionManager`) to `SimulatedClock`, `HistoricalMarketStore`, and `HistoricalExchangeAdapter`.

- **Warm-Up Period:** `BacktestConfig.warmup_ms` defaults to `90_000_000` ms (25 hours: 24h rolling window + 1h indicator warm-up). Trades are only counted inside `[start_time_ms, end_time_ms]`.
- **Historical 24h Ticker Reconstruction:** At each 60s scan timestamp $T$, `HistoricalMarketStore.reconstruct_24h_tickers(T)` uses only completed 1m bars with `close_time <= T` and `open_time >= T - 86_400_000`, requiring `min_24h_coverage_ratio >= 0.80` (default) instead of substituting UTC daily candles.
- **Listing/Delisting Awareness:** `ContractMetadata.is_listed_at(T)` prevents survivorship bias and blocks mid-window entrants until the next 15m snapshot.
- **Realistic Execution (`ExecutionAssumptions`):**
  - `signal_to_order_latency_ms = 200` (entry executes on the **next** available bar open after confirmation + latency, never the signal bar close).
  - `half_spread_bps = 1.0`, `entry_slippage_bps = 5.0`, `stop_slippage_bps = 10.0`, `tp_slippage_bps = 0.0`, `taker_fee_rate = 0.0005`, `funding_rate_8h = 0.0001`.
  - **OHLC Ambiguity:** Uses actual market OHLC (never HA OHLC) for SL/TP triggers. If both SL and TP are touched within the same 1m bar (and no finer sub-bars are supplied), `CONSERVATIVE_SL_FIRST` records a Stop-Loss exit and logs the note in `TradeAuditRecord.ambiguity_resolution_note`.

---

## 2. Running a Backtest
There is **no dedicated CLI flag** for backtesting in `src/main.py`. Backtests are executed via Python:
1. **Run the built-in 12-test Amendment 01 verification suite:**
   ```bash
   pytest tests/unit/test_backtesting_chronological.py --no-cov -v
   ```
2. **Programmatic Replay:**
   ```python
   import asyncio
   from src.backtesting import (
       BacktestConfig,
       ChronologicalBacktestRunner,
       ExecutionAssumptions,
       HistoricalMarketStore,
   )

   store = HistoricalMarketStore()
   # 1. Register contracts via store.register_contract(ContractMetadata(...))
   # 2. Ingest 1m and 15m historical Candle lists via store.ingest_candles(symbol, interval, candles)

   config = BacktestConfig(
       start_time_ms=1700092800000,
       end_time_ms=1700524800000,
       initial_balance=1000.0,
       execution=ExecutionAssumptions(),
   )
   report = asyncio.run(ChronologicalBacktestRunner(store=store, config=config).run())
   print(report.performance, report.risk, report.costs, report.equity)
   ```

---

## 3. Report Metrics (`BacktestReport`)
- **Market Selection (`MarketSelectionMetrics`):** `eligible_contracts_count`, `historical_universe_size`, `top_20_appearances`, `candidate_counts_by_symbol`, `candidate_counts_by_direction`.
- **Strategy (`StrategyMetrics`):** `total_signals`, `confirmed_signals`, `rejected_signals`, `expired_candidates`, `risk_validation_failures`, `tie_breaking_events`, `trades_skipped_open_position`.
- **Performance (`PerformanceMetrics`):** `total_trades`, `winning_trades`, `losing_trades`, `win_rate`, `gross_profit`, `gross_loss`, `net_profit`, `net_return_pct`, `average_win`, `average_loss`, `average_rr_achieved`, `profit_factor`, `expectancy`, `average_holding_time_ms`, `max_consecutive_wins`, `max_consecutive_losses`.
- **Risk (`RiskMetrics`):** `max_drawdown_usdt`, `max_drawdown_pct`, `max_account_exposure`, `max_nominal_risk_per_trade`, `largest_win`, `largest_loss`, `liquidation_events`, `max_margin_utilization`.
- **Costs & Equity:** `entry_fees`, `exit_fees`, `total_fees`, `funding_paid`, `estimated_spread_cost`, `estimated_slippage_cost`, `starting_balance`, `ending_balance`, `realized_equity_curve`, `daily_pnl`, `daily_trade_count`.
- **Reproducibility:** Identical `store` + `BacktestConfig` (`random_seed=42`) produces bit-for-bit identical `BacktestReport` outputs.
- **Exporting Reports:** Built-in JSON/CSV file export is `NOT IMPLEMENTED`; serialize `dataclasses.asdict(report)` in Python if you want to save to disk.
