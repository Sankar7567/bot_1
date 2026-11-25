# Binance Futures HA Trading Bot — Development Log

## Phase 12: Main Orchestration & Full System Integration
**Date:** 2026-11-25  
**Status:** Completed  
**Lead Engineer:** Lead Software Architect & Senior Python Engineer  

### Work Completed:
1. **User Data Stream & ListenKey Support:**
   - Added `/fapi/v1/listenKey` `POST`/`PUT`/`DELETE` methods to `BinanceFuturesClient` (`src/exchange/client.py`).
   - Built `BinanceUserDataStream` (`src/execution/user_stream.py`) with automated keepalive and `ORDER_TRADE_UPDATE` dispatching.
2. **Main Trading Bot Orchestrator (`src/orchestrator.py`):**
   - Connected all 11 domain layers into an event-driven, dependency-injected `TradingBotOrchestrator`.
   - Enforced One-Way mode check at startup, executed `StartupReconciler` before scanning, restored unexpired confirmation windows, and pre-seeded 15m/1m candle buffers and Heikin-Ashi series.
   - Built debounced 15m and 1m batch flushes so simultaneous multi-symbol confirmations preserve deterministic snapshot rank arbitration.
   - Wired signal handoff: Swing Detection (5L/5R OHLC) $\rightarrow$ Bracket Leverage matching & Friction-adjusted Risk Validation $\rightarrow$ `PositionManager.open_position` $\rightarrow$ User Data Stream / Watchdog exit handling.
3. **CLI Entrypoint (`src/main.py`):**
   - Implemented `--check-config` validation flag, sanitized structured logging setup, and `SIGINT`/`SIGTERM` graceful shutdown handling.
4. **Verification & Testing:**
   - Verified full mocked end-to-end trade lifecycle integration (`tests/integration/test_e2e_lifecycle.py`) and orchestrator unit tests (`tests/unit/test_orchestrator.py`).

## Amendment 01: Chronological Backtesting & Live-Trading Replay Specification (Section 6.1)
**Date:** 2026-11-25  
**Status:** Specification & Architecture Incorporated  
**Lead Engineer:** Lead Software Architect & Senior Python Engineer  

### Work Completed:
1. **Requirements & Parity Design:**
   - Incorporated Amendment 01 (Section 6.1) into `docs/PROJECT_SPEC.md`, `docs/ARCHITECTURE.md`, `docs/API_CONTRACTS.md`, `docs/TEST_PLAN.md`, and `docs/CHANGELOG.md`.
   - Documented exact rolling 24h percentage gain and rolling 24h quote volume reconstruction formulas consistent with Binance `/fapi/v1/ticker/24hr`, avoiding silent daily-candle substitution and incomplete-interval fabrication.
   - Defined the shared-engine adapter architecture (`src/backtesting/` implementing exchange/clock adapters around the existing `MarketScanner`, `HeikinAshiSeries`, `MultiSymbolStrategyEngine`, `SwingDetector`, `RiskValidator`, `OrderRouter`, `ProtectiveOrderManager`, and `PositionManager`).
   - Specified all 17 mandatory chronological validation tests under Phase 13 in `docs/TEST_PLAN.md`.

## Phase 11: Position Manager, SQLite State Persistence & Startup Reconciliation
**Date:** 2026-11-24  
**Status:** Completed  
**Lead Engineer:** Lead Software Architect & Senior Python Engineer  

### Work Completed:
1. **SQLite Database & State Repository:**
   - Implemented `src/state/database.py` with WAL mode (`PRAGMA journal_mode = WAL`), busy timeout (5000ms), foreign keys (`PRAGMA foreign_keys = ON`), and connection pooling.
   - Designed relational schema for `positions`, `orders`, `strategy_windows`, and `audit_log` with indexing on status, symbol, and timestamps.
   - Implemented `src/state/repository.py` providing transactional persistence methods for positions, orders, strategy windows, and audit events.
2. **Position Manager & Global 1-Position Lock:**
   - Implemented `src/positions/manager.py` enforcing the global 1-position invariant via `asyncio.Lock` and persistent database checks (`has_open_position`).
   - Coordinated complete position entry: `PENDING_ENTRY` DB record -> `OrderRouter.execute_market_entry` -> fill reconciliation -> `ProtectiveOrderManager.place_protective_orders` -> `OPEN` state in DB and memory.
   - Built failure rollbacks: entry failures mark position `CLOSED` (`ENTRY_FAILED`), protective order placement failures mark position `CLOSED` (`PROTECTION_FAILED_EMERGENCY`), both releasing locks immediately.
   - Implemented `handle_exit_fill` coordinating sibling order cancellation, PnL calculation, database closure, order status updates (`FILLED` / `CANCELED`), and lock release.
   - Implemented `emergency_close_current_position` for operator override and failsafe market closure.
3. **Startup State Reconciler:**
   - Implemented `src/state/reconciler.py` resolving all 6 cases of the startup reconciliation matrix against live Binance Futures state:
     - Case 1: Clean startup (Exchange 0, DB 0, Orders 0 -> `CLEAN`).
     - Case 2: Matching open position with verified protection -> `POSITION_RECOVERED` (re-hydrates `Position` and `ProtectiveOrderPair`).
     - Case 3: Orphaned orders on exchange with 0 positions -> `ORPHANED_ORDERS_CLEARED` (cancels bot-generated orders).
     - Case 4: Unprotected open position on exchange -> `UNPROTECTED_EMERGENCY_CLOSE` (executes emergency market close).
     - Case 5: Position closed on exchange while offline -> `CLOSED_WHILE_OFFLINE` (updates local DB to `CLOSED`).
     - Case 6: Multiple open positions on exchange -> `ReconciliationError` (fatal safety violation).
4. **Verification & Testing:**
   - Authored 24 comprehensive unit tests across `tests/unit/test_state.py`, `tests/unit/test_position_manager.py`, and `tests/unit/test_reconciler.py`.
   - Executed full test suite: 252 passed, 2 skipped, 0 failed.
   - Achieved 89.12% branch coverage across the entire project (well above 80% threshold).
   - Resolved circular typing import using `from __future__ import annotations` and `if TYPE_CHECKING`.
   - Updated `docs/API_CONTRACTS.md`, `docs/CHANGELOG.md` (v0.10.0), and `docs/DEVELOPMENT_LOG.md`.

## Phase 01: Master Project Brief, Architecture & Foundation
**Date:** 2026-11-23  
**Status:** In Progress / Completed Foundation  
**Lead Engineer:** Lead Software Architect & Senior Python Engineer  

### Work Completed:
1. **Environment Inspection:**
   - Platform: Linux x86_64.
   - Python Version: Python 3.14.4.
   - Git repository initialized on branch `main`.
   - Virtual environment `.venv` established and verified.
2. **Requirements & Scope Analysis:**
   - Completed detailed specification analysis of the 15-phase sequence.
   - Identified edge cases: non-atomic conditional orders, partial fills on market orders, high-leverage liquidation vs stop distance, sub-second candle synchronization, and rate-limit controls.
3. **Architecture & Specification Documentation:**
   - Authored `docs/PROJECT_SPEC.md` capturing all strategy and execution requirements.
   - Authored `docs/ARCHITECTURE.md` detailing the 13 decoupled core modules and data flow.
   - Authored `docs/STRATEGY_RULES.md` specifying exact 15m/1m Heikin-Ashi rules and 5L/5R swing pivot detection.
   - Authored `docs/RISK_MODEL.md` establishing the 10% nominal risk budget, 3R target, fee/slippage sizing formulas, and leverage bracket logic.
   - Authored `docs/API_CONTRACTS.md` defining Binance REST/WS endpoints and internal Python dataclasses.
   - Authored `docs/TEST_PLAN.md` detailing the phase-by-phase testing matrix and commands.
4. **Project Structure & Tooling:**
   - Configured `pyproject.toml` with pytest, pytest-asyncio, and pytest-cov settings enforcing minimum 80% coverage.
   - Configured `.gitignore` with strict rules to prevent secret or database leakage.
   - Created `requirements.txt` and `requirements-dev.txt`.
   - Created `.env.example` with full configuration templates and safety defaults.
   - Initialized module directory structure under `src/` and test directories under `tests/`.

### Strategy Amendment v1.1 Incorporated
**Date:** 2026-11-23  
- Incorporated Strategy Amendment v1.1 across all documentation and contracts.
- Documented 15-minute multi-symbol candidate window, multi-candle monitoring, first-trigger-wins global arbitration, deterministic simultaneous confirmation resolution, and single-attempt window consumption.
- Proceeding to Phase 02: Binance Exchange Connector.

## Phase 02: Binance Exchange Connector
**Date:** 2026-11-23  
**Status:** Completed  
**Lead Engineer:** Lead Software Architect & Senior Python Engineer  

### Work Completed:
1. **Connector Architecture & Implementation:**
   - Implemented `src/exchange/client.py` providing asynchronous REST interaction with Binance USDⓈ-M Futures Testnet.
   - Implemented `src/exchange/signer.py` for HMAC-SHA256 request signing with timestamp synchronization and latency window validation (`recvWindow`).
   - Implemented `src/exchange/rate_limiter.py` monitoring `X-MBX-USED-WEIGHT-*` and `Retry-After` headers with proactive backoff.
   - Implemented `src/exchange/models.py` defining immutable data transfer objects (`SymbolFilter`, `SymbolInfo`, `AccountInfo`, `AssetBalance`, `PositionRisk`, `OpenOrder`, `LeverageBracketTier`, `SymbolLeverageBrackets`).
   - Implemented `src/exchange/exceptions.py` with structured error taxonomy (`BinanceAuthError`, `BinanceRateLimitError`, `BinanceNetworkError`, `BinanceAPIError`, `BinanceFilterError`).
2. **Endpoint Coverage:**
   - Ping & Server Time with clock drift offset calculation (`/fapi/v1/ping`, `/fapi/v1/time`).
   - Exchange Information and Symbol Filter parsing (`/fapi/v1/exchangeInfo`).
   - Account and Balance queries (`/fapi/v2/account`, `/fapi/v2/balance`).
   - Position Risk and Open Orders queries (`/fapi/v2/positionRisk`, `/fapi/v1/openOrders`).
   - Leverage Brackets and Notional Tiers (`/fapi/v1/leverageBracket`).
   - Position Mode inspection and configuration (`/fapi/v1/positionSide/dual`).
   - Margin Type configuration (`/fapi/v1/marginType`) with idempotent -4046 handling.
   - Leverage adjustment (`/fapi/v1/leverage`).
3. **Verification & Testing:**
   - 40 test cases executed (39 passed, 1 skipped due to mock-first CI safety).
   - Achieved 94.32% test branch coverage across the codebase.
   - Verified live Testnet connectivity to `https://testnet.binancefuture.com` via unauthenticated ping and time synchronization.
   - Zero credential leakage: credentials masked in logs and string representations.

## Phase 03: Market Scanner
**Date:** 2026-11-23  
**Status:** Completed  
**Lead Engineer:** Lead Software Architect & Senior Python Engineer  

### Work Completed:
1. **Scanner Architecture & Implementation:**
   - Implemented `src/scanner/scanner.py` with asynchronous concurrent querying of exchange information (`/fapi/v1/exchangeInfo`) and 24h ticker statistics (`/fapi/v1/ticker/24hr`).
   - Implemented `src/scanner/filters.py` enforcing contract eligibility (`status == "TRADING"`, `quoteAsset == "USDT"`, `contractType == "PERPETUAL"`), 24h quote volume thresholds, and valid price metrics.
   - Enforced deterministic tie-breaking logic via `deterministic_ranking_sort_key` (sorted by `priceChangePercent` descending, `quoteVolume` descending as primary tie-breaker, and `symbol` ascending as secondary tie-breaker).
   - Implemented `src/scanner/models.py` defining `ScannedSymbol` and `ScanResult`.
   - Implemented `src/scanner/exceptions.py` handling `EmptyUniverseError`, `StaleDataError`, and `ScannerAPIError`.
   - Built periodic 60-second scanning loop with graceful shutdown.
2. **Safety & Robustness:**
   - Detects stale ticker data if ticker `closeTime` lags synchronized server time by > 2 minutes.
   - Strictly validates payload completeness before ranking to prevent partial or empty market universes.
   - Zero order submission or candle/strategy logic implemented.
3. **Verification & Testing:**
   - Executed full test suite: 54 tests passed, 1 skipped (testnet account query requiring private key).
   - `test_live_testnet_market_scan` successfully queried live Binance USDⓈ-M Futures Testnet, ranked live contracts, and verified contract eligibility.
   - Overall test coverage reached 93.96%.

## Phase 04: Market Data Engine
**Date:** 2026-11-23  
**Status:** Completed  
**Lead Engineer:** Lead Software Architect & Senior Python Engineer  

### Work Completed:
1. **Market Data Architecture & Implementation:**
   - Implemented `src/market_data/models.py` defining immutable `Candle` and `CandleCloseEvent` structures with factory parsers `from_rest_kline` and `from_ws_payload`.
   - Implemented `src/market_data/candle_buffer.py` providing chronological ring buffer storage per `(symbol, interval)`, with duplicate deduplication, out-of-order handling, and gap detection.
   - Enforced strict candle completion requirement: unfinished candles (`is_closed == False`, `k.x == False`) are completely rejected from closed history buffers.
   - Implemented `src/market_data/feed.py` providing `BinanceKlineWebSocket` supporting multi-stream combined subscriptions (`/stream?streams=...`) with exponential backoff auto-reconnect and reconnect hooks.
   - Implemented `src/market_data/engine.py` coordinating historical REST pre-seeding, live WebSocket ingestion, REST snapshot reconciliation, and event dispatching across candidate symbols.
2. **Amendment v1.1 Multi-Symbol Concurrency Support:**
   - Designed `set_monitored_universe(symbols)` to concurrently seed history and stream 1m and 15m candles for up to 20 candidate symbols in parallel.
3. **Verification & Testing:**
   - Executed full test suite: 75 passed, 1 skipped (testnet account query requiring private key).
   - `test_live_testnet_historical_kline_seeding` successfully retrieved live 1m and 15m closed candles from Binance Futures Testnet REST API.
   - Reached 91.49% overall test coverage across 924 statements.

## Phase 05: Heikin-Ashi Engine
**Date:** 2026-11-23  
**Status:** Completed  
**Lead Engineer:** Lead Software Architect & Senior Python Engineer  

### Work Completed:
1. **Mathematical Formulation & Implementation:**
   - Implemented `src/indicators/heikin_ashi.py` according to project formulas:
     - $HA\_Close = (Open + High + Low + Close) / 4$
     - First $HA\_Open = (Open + Close) / 2$
     - Subsequent $HA\_Open = (Prev\_HA\_Open + Prev\_HA\_Close) / 2$
     - $HA\_High = \max(High, HA\_Open, HA\_Close)$
     - $HA\_Low = \min(Low, HA\_Open, HA\_Close)$
   - Implemented `HeikinAshiCandle` with properties `is_bullish`, `is_bearish`, `is_doji`, `body_size`, and wicks.
   - Enforced floating-point epsilon ($10^{-8}$) for exact doji identification.
   - Enforced strict closure requirement: `UnfinishedCandleError` raised if incomplete candle passed.
   - Implemented `calculate_heikin_ashi_series` with chronological sequence verification.
   - Implemented stateful `HeikinAshiSeries` manager for incremental candle ingestion and rolling history.
2. **Verification & Testing:**
   - Tested against independently hand-calculated test fixtures (first candle, subsequent bullish candle, doji candle, extreme wick absorption).
   - Validated both 1m and 15m timeframes.
   - Executed full test suite: 83 passed, 1 skipped (testnet account query requiring private key).
   - Achieved 91.58% total project branch coverage across 1,031 statements.

## Phase 06: Multi-Symbol Strategy Engine (Strategy Amendment v1.1)
**Date:** 2026-11-23  
**Status:** Completed  
**Lead Engineer:** Lead Software Architect & Senior Python Engineer  

### Work Completed:
1. **Strategy Engine Architecture & Implementation:**
   - Implemented `src/strategy/models.py` with `CandidateState`, `ConfirmationWindow`, and `TradeSignal`.
   - Implemented `src/strategy/arbitrator.py` with `GlobalSlotArbitrator` ensuring concurrency-safe, deterministic first-trigger-wins slot acquisition.
   - Implemented `src/strategy/engine.py` coordinating:
     - 15-minute confirmation window creation from top-20 snapshot.
     - Concurrent multi-candle 1m monitoring across all candidate symbols.
     - Misaligned and doji candle tolerance throughout the active 15m window.
     - Single global winning signal emission and immediate cancellation of sibling candidate watchers.
     - Window consumption upon winner risk validation failure (preventing second picks).
     - Strict expiration at next 15m candle close and rejection of late-arriving candle events.
     - State serialization (`serialize_state`) and restoration (`restore_state`) across restarts.
2. **Verification & Testing:**
   - Successfully implemented and passed all 17 mandatory test scenarios specified in Replacement Prompt 06.
   - Executed full test suite: 100 passed, 1 skipped (testnet account query requiring private key).
   - Achieved 90.37% overall test branch coverage across 1,232 statements.

### Next Actions:
- Proceed to Phase 07: Swing Detection Engine upon review and approval.






## Phase 08: Risk Management and Position Sizing
**Date:** 2026-11-23
**Status:** Completed
**Lead Engineer:** Lead Software Architect & Senior Python Engineer

### Work Completed:
1. **Risk Engine Architecture & Implementation:**
   - `src/risk/calculator.py`: Pure stateless sizing functions implementing `RISK_MODEL.md §2.2` exactly.
     - `calculate_friction_adjusted_quantity`: denominator = `ΔP_sl + P_entry × (r_fee + r_slip) + P_sl × r_fee`.
     - `estimate_liquidation_price`: LONG: `P_entry × (1 − 1/L + MMR)`; SHORT: `P_entry × (1 + 1/L − MMR)`.
     - `calculate_tp_price`: 3R target from stop distance.
     - `compute_pnl_at_target`: net PnL with both entry and exit taker fees.
   - `src/risk/brackets.py`: Bracket matching sorts tiers by `notional_cap` ascending, returns first tier where `notional ≤ cap`. Falls back to last tier when notional exceeds all caps.
   - `src/risk/validator.py`: 13-step sequential validation producing an authoritative `SizingResult`:
     1. Equity sanity.
     2. Risk budget calculation.
     3. Stop distance (direction-aware, positive required).
     4. Friction-adjusted quantity.
     5. Step-size truncation (always floor, integer arithmetic to avoid float drift).
     6. minQty filter rejection (never increase quantity).
     7. maxQty filter rejection.
     8. TP price calculation.
     9. Notional and leverage from bracket.
     10. minNotional filter rejection (never increase quantity).
     11. Required isolated margin calculation.
     12. Margin availability check.
     13. Liquidation guard (stop must fire before liquidation; rejected otherwise).
   - `src/risk/models.py`: `BracketInfo`, `SizingInput`, `SizingResult` frozen dataclasses.
   - `src/risk/exceptions.py`: Structured exception taxonomy.
2. **Verification & Testing:**
   - 68 unit tests across 11 test classes, all passing.
   - Primary hand-calculated vector from `RISK_MODEL.md §5` verified to 6 significant figures:
     - `friction_qty = 100.0 / 2.149 ≈ 46.5333`, `final_qty = 46.533` (step=0.001).
   - Liquidation guard verified: LONG liq=95.4 < sl=98 → safe; LONG liq=99.6 > sl=97 → rejected.
   - Executed full test suite: **184 passed, 1 skipped** (testnet account key).
   - Total project branch coverage: **89.66%** across 1,560 statements.

### Next Actions:
- Proceed to Phase 09: Binance Order Execution Engine upon review and approval.


## Phase 09: Binance Order Execution Engine
**Date:** 2026-11-23
**Status:** Completed
**Lead Engineer:** Lead Software Architect & Senior Python Engineer

### Work Completed:
1. **Exchange Client Extension:**
   - Extended `src/exchange/client.py` with signed endpoint methods: `place_order` (`POST /fapi/v1/order`), `query_order` (`GET /fapi/v1/order`), and `cancel_order` (`DELETE /fapi/v1/order`).
2. **Deterministic Order Identification:**
   - Implemented `src/execution/client_id.py` generating deterministic, traceable `clientOrderId` values:
     - Formats: `HA_E_<timestamp>_<hex>`, `HA_SL_<timestamp>_<hex>`, `HA_TP_<timestamp>_<hex>`, `HA_EMG_<timestamp>_<hex>`.
     - Strict 36-character length limit compliance.
     - Uniqueness validation: zero collisions across 1,000 rapid iterations.
     - Regex parser `parse_client_order_id` and verification helper `is_bot_client_order_id`.
3. **Execution Domain Models & Exceptions:**
   - `src/execution/models.py`: Immutable `OrderExecutionRequest`, `OrderFill`, and enums `OrderSide`, `OrderType`, `OrderStatus`.
   - `src/execution/exceptions.py`: `ExecutionError`, `OrderRejectedError`, `FillTimeoutError`, `PartialFillError`, `ProtectiveOrderError`, `CancellationError`.
4. **Order Routing & Resilient Execution:**
   - Implemented `src/execution/order_router.py`:
     - Sets margin type to `ISOLATED` (idempotently handling -4046).
     - Configures symbol leverage to target leverage.
     - Submits `MARKET` entry orders with traceable `clientOrderId`.
     - Reconciles network transport timeouts automatically by querying the exchange with `clientOrderId` before declaring failure.
     - Polls for completion if initial response is `NEW` or `PARTIALLY_FILLED`.
     - Partial fill handling: returns `OrderFill` with executed quantity and average price for downstream protective sizing.
     - Idempotent order cancellation (absorbing -2011 and -2013).
5. **Verification & Testing:**
   - Authored unit test suite in `tests/unit/test_execution.py` (22 tests) and integration suite in `tests/integration/test_execution_integration.py` (2 tests).
   - Executed full project test suite: **207 passed, 2 skipped** (testnet credentials).
   - Achieved **89.03%** total project branch coverage across 1,786 statements.

### Next Actions:
- Proceed to Phase 10: Protective Order Manager upon review and approval.


## Phase 10: Protective Order Manager
**Date:** 2026-11-24
**Status:** Completed
**Lead Engineer:** Lead Software Architect & Senior Python Engineer

### Work Completed:
1. **Directional Tick-Size Price Rounding:**
   - Implemented `round_price_directional` in `src/positions/protective.py` enforcing exact exchange rules:
     - LONG: stop-loss rounded DOWN (floor) to maintain $\ge$ intended distance; take-profit rounded UP (ceil) to ensure $\ge 3R$ gross reward.
     - SHORT: stop-loss rounded UP (ceil); take-profit rounded DOWN (floor).
     - Scaled integer arithmetic prevents decimal precision drift and division by zero across all tick sizes ($0.01, 0.5, 1.0$, etc.).
2. **Post-Fill Recalculation & Order Placement:**
   - Sizing of protective orders strictly matches the actual `executedQty` from the entry fill.
   - Recalculates stop distance and 3:1 TP from the actual average entry price `avg_entry_price`.
   - Submits exchange-side `STOP_MARKET` (`HA_SL_`) and `TAKE_PROFIT_MARKET` (`HA_TP_`) orders with `reduceOnly=true`.
   - Confirms that both orders enter `NEW` state on the exchange via post-placement querying.
3. **Fail-Safe Emergency Position Mitigation:**
   - If either protective order fails placement or validation, any placed conditional order is cancelled and an emergency market close (`HA_EMG_`, `reduceOnly=true`) is immediately executed to prevent unprotected exposure.
4. **Sibling Order Cancellation & Zero Exposure Verification:**
   - Implemented `cancel_sibling_order`:
     - Identifies which order filled (SL vs TP).
     - Immediately cancels the remaining sibling order via REST, absorbing `-2011` / `-2013` if already terminated.
     - Polls `get_positions` on the exchange to verify net position exposure is zero (`not pos.is_open`).
     - Returns `ExitTriggerResult` and deregisters active pair.
5. **Verification & Testing:**
   - Authored unit test suite in `tests/unit/test_protective.py` (21 tests).
   - Executed full project test suite: **228 passed, 2 skipped** (testnet credentials).
   - Achieved **88.91%** total project branch coverage across 1,992 statements.

### Next Actions:
- Proceed to Phase 11: Position Manager and State Recovery upon review and approval.
