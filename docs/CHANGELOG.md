# Changelog

All notable changes to the Binance Futures HA Trading Bot will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
