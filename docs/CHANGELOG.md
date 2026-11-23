# Changelog

All notable changes to the Binance Futures HA Trading Bot will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
