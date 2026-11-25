# Changelog

All notable changes to the Binance Futures HA Trading Bot will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.11.0] - 2026-11-25
### Added
- Phase 12: Main Orchestration & Full System Integration.
- `src/exchange/client.py`: Added User Data Stream listenKey REST methods (`create_listen_key`, `keepalive_listen_key`, `close_listen_key`) with `api_key_only` header mode and `PUT` request support.
- `src/execution/user_stream.py`: Added `BinanceUserDataStream` managing listenKey lifecycle, keepalive loop, auto-reconnect, and `ORDER_TRADE_UPDATE` dispatch.
- `src/orchestrator.py`: Implemented `TradingBotOrchestrator` connecting Scanner (60s refresh), MarketData (1m/15m seeding & WS), Heikin-Ashi Series, Multi-Symbol Strategy Engine, Swing Detector (5L/5R OHLC), Risk Validation & Leverage Brackets, Order Router, Protective Order Manager, Position Manager (global lock), and Startup Reconciler. Includes universe symbol protection for in-flight windows/positions, debounced timestamp batching for deterministic rank arbitration, exit fill handling, fallback watchdog loop, and graceful shutdown.
- `src/main.py`: Added CLI entrypoint with `--check-config` validation, structured logging setup, and OS `SIGINT`/`SIGTERM` graceful shutdown handling.
- `tests/unit/test_orchestrator.py` & `tests/integration/test_e2e_lifecycle.py`: Added full unit and mocked end-to-end lifecycle integration tests covering scan $\rightarrow$ trigger $\rightarrow$ rank tie-break $\rightarrow$ swing $\rightarrow$ risk $\rightarrow$ entry $\rightarrow$ protection $\rightarrow$ TP fill $\rightarrow$ sibling cancel $\rightarrow$ DB close $\rightarrow$ window consumption on risk failure.
- `docs/API_CONTRACTS.md`: Added Section 1.16 for Orchestrator, User Data Stream, and CLI.

## [0.10.1] - 2026-11-25
### Added
- **Amendment 01 (Section 6.1): Historically Accurate, Chronological Backtesting and Live-Trading Replay**:
  - Added Section 6.1 to `docs/PROJECT_SPEC.md` defining strict non-lookahead chronological simulation, historical universe reconstruction (listing/delisting survivorship avoidance), rolling 24h ticker formula & completeness guards, 60s scanner + 15m frozen snapshot timing, warm-up rules, shared live-engine parity, realistic latency/spread/slippage/fee/funding execution, conservative SL/TP ambiguity resolution, full equity accounting, and complete audit trail.
  - Added `src/backtesting/` module architecture to `docs/ARCHITECTURE.md`.
  - Added Section 1.15 (`src/backtesting/` interface contracts, rolling 24h formula, execution assumptions, and report schemas) to `docs/API_CONTRACTS.md`.
  - Expanded Phase P13 in `docs/TEST_PLAN.md` with all 17 mandatory chronological validation tests from Amendment 01 §16.

## [0.10.0] - 2026-11-24
### Added
- Phase 11: Position Manager, SQLite State Persistence & Startup State Reconciliation.
- `src/state/database.py`: `DatabaseManager` providing SQLite database initialization with WAL mode (`PRAGMA journal_mode = WAL`), foreign keys (`PRAGMA foreign_keys = ON`), busy timeout 5000ms, and complete schema creation (`positions`, `orders`, `strategy_windows`, `audit_log`).
- `src/state/repository.py`: `StateRepository` with typed dataclasses `PositionRecord` and `OrderRecord`:
  - CRUD operations for positions and single active position query (`get_open_position`).
  - Order tracking, update status, and position linking.
  - Strategy window persistence and active query.
  - Immutable audit logging (`log_audit`).
- `src/positions/manager.py`: `PositionManager`:
  - Global 1-position lock (`asyncio.Lock` and persistent database check) strictly enforcing single active position invariant.
  - Coordinated position opening: `PENDING_ENTRY` DB record -> `OrderRouter` market entry -> actual fill metrics -> `ProtectiveOrderManager` SL/TP placement -> `OPEN` state in DB and memory.
  - Robust failure handling: releases lock and updates DB to `CLOSED` on entry or protective failures.
  - Exit fill processing (`handle_exit_fill`): coordinates atomic sibling cancellation, calculates realized PnL, transitions to `CLOSED`, updates DB orders, and releases lock.
  - Emergency manual close (`emergency_close_current_position`).
- `src/positions/exceptions.py`: `PositionError`, `PositionLockError`, `PositionNotFoundError`.
- `src/state/reconciler.py`: `StartupReconciler`:
  - Validates and resolves the 6 startup reconciliation matrix cases:
    - Case 1: Clean startup (Exchange 0, DB 0, Orders 0 -> `CLEAN`).
    - Case 2: Matching open position with verified protection -> `POSITION_RECOVERED`.
    - Case 3: Orphaned orders on exchange with 0 positions -> `ORPHANED_ORDERS_CLEARED`.
    - Case 4: Unprotected open position on exchange -> `UNPROTECTED_EMERGENCY_CLOSE`.
    - Case 5: Position closed on exchange while offline -> `CLOSED_WHILE_OFFLINE`.
    - Case 6: Multiple open positions on exchange -> `ReconciliationError` (Fatal).
- `tests/unit/test_state.py`: 11 unit tests covering database pragmas, schemas, position CRUD, order CRUD, strategy window persistence, and audit logging.
- `tests/unit/test_position_manager.py`: 7 unit tests covering global 1-position lock, entry lifecycle, entry error rollback, protective error rollback, exit fill handling with PnL, and emergency position closure.
- `tests/unit/test_reconciler.py`: 6 unit tests validating all 6 startup reconciliation matrix scenarios.
- `docs/API_CONTRACTS.md`: Added Section 1.14 documenting Position Manager, SQLite State Persistence, and Startup Reconciler interfaces.

## [0.9.0] - 2026-11-24
### Added
- Phase 10: Protective Order Manager implementation.
- `src/positions/protective.py`: `ProtectiveOrderManager` and directional tick rounding:
  - Directional price rounding (`round_price_directional`): strict floor for LONG SL and SHORT TP, strict ceil for LONG TP and SHORT SL, using integer scaled arithmetic avoiding zero-division and precision drift.
  - Post-entry fill recalculation: recalculates stop distance and 3:1 TP based on actual fill price and sizes protective orders strictly against `executed_qty`.
  - Submits exchange-side conditional orders with `reduceOnly=true` (`STOP_MARKET` and `TAKE_PROFIT_MARKET`).
  - Active verification: validates both orders are in `NEW` state on exchange.
  - Sibling cancellation: atomic cancellation of remaining sibling order when SL or TP fills, with absorption of -2011/-2013 if already terminated.
  - Zero net exposure verification: polls `get_positions` to confirm position closure.
  - Fail-safe emergency market close: cancels active conditional orders and submits market close (`HA_EMG_`) on placement or verification failure.
- `src/positions/models.py`: `ProtectiveOrderPair`, `ExitTriggerResult`, `EmergencyExitResult`, `ExitReason`.
- `tests/unit/test_protective.py`: 21 unit tests covering directional price rounding, order pair model helpers, order placement, entry recalculation, emergency fail-safe close on placement/verification error, sibling cancellation, and position closure verification.
- `docs/API_CONTRACTS.md`: Added Section 1.13 for Protective Order Manager.

## [0.8.0] - 2026-11-23
### Added
- Phase 09: Binance Order Execution Engine implementation.
- `src/execution/client_id.py`: Deterministic clientOrderId generator ensuring $\le 36$ characters, collision resistance, bot prefix tagging (`HA_E`, `HA_SL`, `HA_TP`, `HA_EMG`), regex validation, and parsing.
- `src/execution/models.py`: Immutable data transfer models `OrderExecutionRequest`, `OrderFill`, and enums `OrderSide`, `OrderType`, `OrderStatus`.
- `src/execution/exceptions.py`: Structured execution exception hierarchy `ExecutionError`, `OrderRejectedError`, `FillTimeoutError`, `PartialFillError`, `ProtectiveOrderError`, `CancellationError`.
- `src/execution/order_router.py`: `OrderRouter` orchestrating:
  - Automated `ISOLATED` margin type setting (idempotently absorbing -4046).
  - Target leverage configuration on Binance Futures.
  - Market entry order placement with traceable `clientOrderId`.
  - Automatic network error reconciliation (query by `clientOrderId` prior to failure).
  - Immediate fill detection and polling for pending fills.
  - Partial fill handling: returning fill with actual `executed_qty` and `avg_price`.
  - Idempotent order cancellation (absorbing -2011 and -2013).
  - Live order query interface.
- `src/exchange/client.py`: Added direct REST methods `place_order`, `query_order`, `cancel_order`.
- `tests/unit/test_execution.py`: 22 unit tests covering client ID generation, uniqueness, parsing, model serialization, immediate fill, margin handling, leverage configuration, network timeout reconciliation, fill polling, fill timeout, and idempotent cancellation.
- `tests/integration/test_execution_integration.py`: Offline ID compliance test and optional authenticated Testnet connectivity test.
- `docs/API_CONTRACTS.md`: Added Section 1.12 for Order Execution Engine.

## [0.7.0] - 2026-11-23
### Added
- Phase 08: Risk Management and Position Sizing implementation.
- `src/risk/exceptions.py`: `RiskValidationError`, `InsufficientEquityError`, `InvalidStopDistanceError`, `LiquidationRiskError`.
- `src/risk/models.py`: `BracketInfo` (resolved leverage tier), `SizingInput` (all trade parameters), `SizingResult` (complete validation output).
- `src/risk/calculator.py`: Pure stateless sizing formulas — `calculate_risk_budget`, `calculate_stop_distance`, `calculate_tp_price`, `calculate_friction_adjusted_quantity` (RISK_MODEL.md §2.2 formula), `calculate_required_margin`, `estimate_liquidation_price` (RISK_MODEL.md §4.1 formula), `is_liq_price_safer_than_sl`, `compute_pnl_at_target`.
- `src/risk/brackets.py`: `get_bracket_for_notional`, `get_max_eligible_leverage` — bracket sorting and first-fit matching against sorted notional caps.
- `src/risk/validator.py`: `truncate_to_step_size` (always floor, integer-arithmetic), `round_price_to_tick`, `validate_and_size_position` (13-step sequential validation: equity, risk budget, stop distance, friction quantity, step truncation, minQty, maxQty, TP, notional, minNotional, margin availability, liquidation guard).
- `src/risk/__init__.py`: Public API exports.
- `tests/unit/test_risk.py`: 68 unit tests across 11 test classes, covering all hand-calculated reference vectors from RISK_MODEL.md §5.

## [0.6.0] - 2026-11-23
### Added
- Phase 07: Swing Detection Engine implementation.
- `src/indicators/swing_points.py`: Pure 5-left/5-right swing pivot detector on actual market OHLC data:
  - `find_confirmed_pivots()`: Scans chronological candle sequences for confirmed swing highs and lows using strict inequality (equal highs/lows rejected). Checks chronological ordering before evaluation. Zero look-ahead bias: pivot at bar $i$ confirmed only after bar $i + \text{right\_bars}$ closes.
  - `get_latest_eligible_pivot()`: Searches backwards from the most recent confirmable bar within a configurable lookback window to return the latest eligible pivot.
  - `calculate_swing_stop_loss()`: Computes stop price and distance with a configurable buffer ratio (LONG: $SL = \text{SwingLow} \times (1 - \text{buffer})$; SHORT: $SL = \text{SwingHigh} \times (1 + \text{buffer})$). Returns `StopLossCalculation` with `is_valid=False` on wrong pivot type, non-positive stop, or stop on the wrong side of entry.
  - `SwingDetector`: Stateful per-symbol/interval wrapper with `get_latest_swing_low` and `get_latest_swing_high` convenience methods.
- `src/indicators/__init__.py`: Updated public exports.
- `docs/API_CONTRACTS.md`: Added Section 1.10 documenting the Swing Detection Engine interface and data contracts.
- `tests/unit/test_swing_points.py`: 11 unit tests covering hand-calculated pivot low/high, strict inequality enforcement, equal high/low rejection, zero look-ahead bias verification, lookback limits, long/short stop loss buffer calculations, chronological sequence validation, and stateful `SwingDetector` wrapper.

## [0.5.0] - 2026-11-23
### Added
- Phase 06: Multi-Symbol Strategy Engine implementation (Replacement Prompt 06 / Strategy Amendment v1.1).
- `src/strategy/models.py`: State models `CandidateState`, `ConfirmationWindow`, `TradeSignal`, enums `Direction`, `CandidateStatus`, `WindowStatus`.
- `src/strategy/arbitrator.py`: `GlobalSlotArbitrator` providing thread-safe first-trigger-wins slot claiming with deterministic tie-breaking.
- `src/strategy/engine.py`: `MultiSymbolStrategyEngine` orchestrating 15m trigger window creation, concurrent 1m multi-candidate monitoring, single-attempt window consumption on risk failure, expiration, and state serialization.
- Comprehensive unit test suite covering all 17 mandatory test scenarios in Replacement Prompt 06.

## [0.4.0] - 2026-11-23
### Added
- Phase 05: Heikin-Ashi Calculation Engine implementation.
- `src/indicators/heikin_ashi.py`: Pure mathematical Heikin-Ashi formulas, `HeikinAshiCandle` model, series generator, and stateful `HeikinAshiSeries` manager.
- `src/indicators/exceptions.py`: Domain exceptions `InconsistentDataError` and `UnfinishedCandleError`.
- Comprehensive unit tests matching independently hand-calculated test vectors for initial bar, subsequent bullish/bearish bars, doji bars, and extreme wicks.

## [0.3.0] - 2026-11-23
### Added
- Phase 04: Market Data Engine implementation.
- `src/market_data/models.py`: Immutable `Candle` and `CandleCloseEvent` models with REST and WebSocket parsing.
- `src/market_data/candle_buffer.py`: Chronological ring buffer with strict closed-candle enforcement, duplicate deduplication, out-of-order insertion, and gap detection.
- `src/market_data/feed.py`: `BinanceKlineWebSocket` client with automatic reconnection and combined stream URL generator.
- `src/market_data/engine.py`: Central engine coordinating historical REST seeding, live WebSocket ingestion, REST snapshot reconciliation, staleness detection, and candidate universe multi-symbol management (Amendment v1.1).
- Unit and integration tests for candle ordering, deduplication, unfinished bar rejection, WebSocket dispatch, and live Testnet historical kline seeding.

## [0.2.0] - 2026-11-23
### Added
- Phase 03: Market Scanner implementation.
- `src/scanner/models.py`: Data models `ScannedSymbol` and `ScanResult`.
- `src/scanner/filters.py`: Contract status, USDT perpetual, volume thresholds, and deterministic ranking sort key.
- `src/scanner/scanner.py`: Asynchronous scanner querying 24h ticker statistics, filtering, ranking top 20 gainers, and background periodic runner.
- Unit and integration tests for scanner ranking, tie-breaking, inactive/delivery filtering, liquidity thresholds, stale data rejection, and live Testnet scanning.

## [0.1.1] - 2026-11-23
### Changed
- Strategy Amendment v1.1: Replaced single-shot 1st 1m candle confirmation with a global 15-minute multi-symbol confirmation window.
- Updated `PROJECT_SPEC.md`, `ARCHITECTURE.md`, `STRATEGY_RULES.md`, `API_CONTRACTS.md`, and `TEST_PLAN.md` with:
  - Fixed candidate snapshot ranking per 15m window.
  - Concurrent multi-candle 1m monitoring.
  - Global first-trigger-wins arbitration.
  - Deterministic tie-breaking for simultaneous confirmations on identical 1m timestamps.
  - Window consumption upon winner risk validation failure (no second picks).

## [0.1.0] - 2026-11-23
### Added
- Phase 01: Project Foundation and Architectural Design.
- Core specification documents:
  - `docs/PROJECT_SPEC.md`
  - `docs/ARCHITECTURE.md`
  - `docs/STRATEGY_RULES.md`
  - `docs/RISK_MODEL.md`
  - `docs/API_CONTRACTS.md`
  - `docs/TEST_PLAN.md`
  - `docs/DEVELOPMENT_LOG.md`
  - `docs/CHANGELOG.md`
  - `docs/RUNBOOK.md`
- Project directory structure for all modules under `src/` and `tests/`.
- Configuration system using Pydantic Settings with automatic secret masking.
- Centralized structured logging with credential sanitization and file rotation.
- Dependency definitions (`requirements.txt`, `requirements-dev.txt`, `pyproject.toml`).
- Git repository initialization and `.gitignore` safety rules.
- Environment template `.env.example`.
- Foundational unit test suite and pytest coverage configuration.
