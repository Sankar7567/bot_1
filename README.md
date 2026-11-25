# Binance Futures Heikin-Ashi Trading Bot

> **Backend-only Python CLI trading engine for Binance USDⓈ-M Perpetual Futures.**  
> No web UI. No REST server. No frontend. All interaction is via the terminal and SQLite.

---

## What This Application Does

The Binance HA Bot is an automated algorithmic trading system that:

1. **Scans** all USDⓈ-M USDT perpetual contracts on Binance every 60 seconds and ranks them by rolling 24-hour percentage gain.
2. **Selects** the top-20 eligible contracts (after volume and liquidity filtering) at each 15-minute trigger window.
3. **Analyses** completed 15-minute Heikin-Ashi candles to establish a directional bias (Bullish / Bearish).
4. **Confirms** that bias on the first completed 1-minute Heikin-Ashi candle in the same direction.
5. **Validates** a confirmed swing stop-loss using real OHLC pivots (5 bars left, 5 bars right, zero look-ahead).
6. **Sizes** each position to risk exactly 10 % of current futures account equity, enforcing 3 : 1 reward-to-risk.
7. **Executes** a market entry with native exchange-side Stop-Loss and Take-Profit orders (`reduceOnly=true`).
8. **Manages** exactly one open position at a time (isolated margin, highest eligible bracket leverage).
9. **Recovers** from crashes by reconciling persistent SQLite state against live exchange state on startup.
10. **Backtests** chronologically: replaying historical market data in forward-walking order with no look-ahead bias (Amendment 01).

---

## Major Features

| Feature | Status |
|---|---|
| Market scanner — top-20 24h gainers | ✅ Implemented |
| Multi-timeframe Heikin-Ashi strategy | ✅ Implemented |
| Confirmed swing stop-loss (5L/5R pivots) | ✅ Implemented |
| 10% equity risk sizing + 3:1 R:R | ✅ Implemented |
| Isolated margin execution on Binance | ✅ Implemented |
| Exchange-side SL/TP protective orders | ✅ Implemented |
| SQLite crash recovery | ✅ Implemented |
| Chronological backtesting engine | ✅ Implemented |
| Binance Testnet support | ✅ Implemented |
| Structured logging with secret redaction | ✅ Implemented |
| Web dashboard / frontend | ❌ NOT IMPLEMENTED |
| Standalone paper-trading virtual wallet | ❌ NOT IMPLEMENTED |
| REST / SSE API server | ❌ NOT IMPLEMENTED |
| Database migration CLI | ❌ NOT IMPLEMENTED |

---

## Architecture Overview

```
src/
├── config/         Settings (pydantic-settings, .env, validators)
├── exchange/       Binance REST + WebSocket connectors
├── scanner/        Top-20 24h gainers scanner (60-second refresh)
├── market_data/    Kline feeds, ring buffers
├── indicators/     Pure Heikin-Ashi + 5L/5R swing calculations
├── strategy/       15m trigger + 1m confirmation state machine
├── risk/           10% equity sizing, leverage brackets, step sizes
├── execution/      Order submission, fills, user data stream
├── positions/      Position manager + SL/TP protective lifecycle
├── state/          Persistent SQLite WAL store + crash recovery
├── backtesting/    Chronological non-look-ahead replay engine
├── monitoring/     Structured logging + secret redaction
├── orchestrator.py Event-driven TradingBotOrchestrator
└── main.py         CLI entrypoint (--check-config, SIGINT/SIGTERM)
```

```
Data flow:
Binance WebSocket ──► MarketDataEngine ──► StrategyEngine ──► RiskManager
                                                                    │
                                                              ExecutionEngine
                                                                    │
                                                           ProtectiveOrderManager
                                                                    │
                                                              StateManager (SQLite)
```

---

## Technology Stack

| Component | Technology |
|---|---|
| Language | Python 3.11+ (tested on 3.14.4) |
| Async runtime | `asyncio` |
| HTTP client | `httpx` (async) |
| WebSocket | `websockets` |
| Configuration | `pydantic-settings` v2, `.env` |
| Database | SQLite 3 (WAL mode, via `aiosqlite`) |
| Logging | Python `logging`, `RotatingFileHandler` |
| Testing | `pytest`, `pytest-asyncio`, `pytest-cov` |
| Exchange API | Binance USDⓈ-M Futures REST + WS |

---

## Supported Operating Modes

| Mode | Description | Default? |
|---|---|---|
| **Testnet** (`APP_ENV=testnet`) | Connects to Binance Futures Testnet. All orders are simulated on the exchange — no real money. | ✅ Yes |
| **Production** (`APP_ENV=production`) | Connects to live Binance Futures. Real orders, real money. Requires explicit configuration. | ❌ No |
| **Backtest** | Chronological forward-walking replay of historical market data. | Run via code / test suite |
| **Web dashboard** | — | ❌ NOT IMPLEMENTED |
| **Paper trading (virtual wallet)** | — | ❌ NOT IMPLEMENTED |

> **⚠️ SECURITY WARNING:** Live trading (`APP_ENV=production`) is **disabled by default**.  
> Enabling it requires explicitly setting production API endpoints and credentials in `.env`.  
> Never commit `.env` to version control. Never share API keys with withdrawal permissions.

---

## System Requirements

| Requirement | Minimum | Recommended |
|---|---|---|
| OS | Ubuntu 20.04 LTS | Ubuntu 22.04 LTS |
| Python | 3.11 | 3.12+ |
| RAM | 512 MB | 1 GB |
| Disk | 1 GB | 5 GB (for backtest data) |
| Network | Stable internet | Low-latency VPS |
| Node.js | Not required | Not required |
| Database | SQLite (bundled) | SQLite (bundled) |

---

## Quick Installation

```bash
# 1. Clone the repository
git clone https://github.com/Sankar7567/bot_1.git bot_ha
cd bot_ha

# 2. Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install all dependencies (including dev/test)
pip install -r requirements-dev.txt

# 4. Create your environment file
cp .env.example .env
# Edit .env — set BINANCE_API_KEY and BINANCE_API_SECRET for Testnet

# 5. Validate configuration
python -m src.main --check-config

# 6. Run the full offline test suite
pytest --cov=src --cov-fail-under=80
```

For a complete step-by-step installation guide, see **[docs/INSTALLATION_GUIDE.md](docs/INSTALLATION_GUIDE.md)**.

---

## Quick Start (Testnet Paper Trading)

```bash
# Start the bot in Testnet mode (default — no live money)
source .venv/bin/activate
python -m src.main
```

The bot will:
1. Connect to Binance Futures Testnet.
2. Scan the market and select top-20 gainers.
3. Monitor for Heikin-Ashi signals.
4. Submit and manage positions on the Testnet exchange.
5. Log all activity to `logs/trading_bot.log`.
6. Persist state to `data/trading_bot.db`.

For a guided first-run walkthrough, see **[docs/QUICK_START.md](docs/QUICK_START.md)**.

---

## Documentation

All documentation is in the [`docs/`](docs/) directory. Start with **[docs/README.md](docs/README.md)** for the full index.

### Core Guides

| Document | Description |
|---|---|
| [USER_MANUAL.md](docs/USER_MANUAL.md) | Operating modes, daily workflows, interaction patterns |
| [INSTALLATION_GUIDE.md](docs/INSTALLATION_GUIDE.md) | Complete fresh-install guide for Ubuntu/Linux |
| [QUICK_START.md](docs/QUICK_START.md) | Testnet quick-start and first backtest |
| [CONFIGURATION_GUIDE.md](docs/CONFIGURATION_GUIDE.md) | All 22 `Settings` fields, env vars, validators |
| [API_KEY_SETUP.md](docs/API_KEY_SETUP.md) | Binance Testnet + production API key creation |
| [SECURITY_GUIDE.md](docs/SECURITY_GUIDE.md) | Secret handling, file permissions, least privilege |

### Feature Manuals

| Document | Description |
|---|---|
| [MARKET_SCANNER_MANUAL.md](docs/MARKET_SCANNER_MANUAL.md) | Universe filters, top-20 ranking, refresh timing |
| [STRATEGY_MANUAL.md](docs/STRATEGY_MANUAL.md) | Heikin-Ashi math, signal lifecycle, worked examples |
| [RISK_MANAGEMENT_MANUAL.md](docs/RISK_MANAGEMENT_MANUAL.md) | 5L/5R pivots, equity sizing math, rejection reasons |
| [BACKTESTING_MANUAL.md](docs/BACKTESTING_MANUAL.md) | Chronological replay engine, `BacktestReport` metrics |
| [PAPER_TRADING_MANUAL.md](docs/PAPER_TRADING_MANUAL.md) | Testnet and replay alternatives (virtual wallet NOT IMPLEMENTED) |
| [LIVE_TRADING_MANUAL.md](docs/LIVE_TRADING_MANUAL.md) | Pre-activation checklist, production config, emergency procedures |
| [DASHBOARD_MANUAL.md](docs/DASHBOARD_MANUAL.md) | CLI/SQLite equivalents (web UI NOT IMPLEMENTED) |
| [TERMINAL_MANUAL.md](docs/TERMINAL_MANUAL.md) | All verified CLI commands and monitoring recipes |

### Operations & Development

| Document | Description |
|---|---|
| [TRADE_HISTORY_AND_ANALYTICS.md](docs/TRADE_HISTORY_AND_ANALYTICS.md) | SQLite schema, useful queries, `BacktestReport` reference |
| [MONITORING_AND_LOGS.md](docs/MONITORING_AND_LOGS.md) | Log file, rotation (10 MB × 5), log format, monitoring commands |
| [TESTING_GUIDE.md](docs/TESTING_GUIDE.md) | All test suite commands, baseline results (280 passed, 86.71% coverage) |
| [TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | 8 categories with symptoms / causes / diagnostics / resolution |
| [BACKUP_AND_RECOVERY.md](docs/BACKUP_AND_RECOVERY.md) | SQLite WAL backup and restore procedures |
| [MAINTENANCE_GUIDE.md](docs/MAINTENANCE_GUIDE.md) | Code updates, DB practices, Git workflow |
| [DEVELOPER_GUIDE.md](docs/DEVELOPER_GUIDE.md) | Architecture diagram, module table, extension guide |
| [FAQ.md](docs/FAQ.md) | Frequently asked questions |

### Technical Specifications

| Document | Description |
|---|---|
| [PROJECT_SPEC.md](docs/PROJECT_SPEC.md) | Master project specification |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | Detailed component architecture |
| [STRATEGY_RULES.md](docs/STRATEGY_RULES.md) | Complete strategy rule set |
| [RISK_MODEL.md](docs/RISK_MODEL.md) | Risk model specification |
| [API_CONTRACTS.md](docs/API_CONTRACTS.md) | Binance API contracts |
| [TEST_PLAN.md](docs/TEST_PLAN.md) | Test plan and coverage targets |
| [RUNBOOK.md](docs/RUNBOOK.md) | Operational runbook |
| [CHANGELOG.md](docs/CHANGELOG.md) | Version history |

---

## Known Limitations

| Limitation | Detail |
|---|---|
| No web UI | The application is terminal-only. There is no dashboard, no browser interface, and no HTTP server. |
| No standalone paper-trading | There is no virtual-wallet paper trading mode. Use Binance Futures Testnet instead (`APP_ENV=testnet`). |
| No CLI backtest subcommand | Backtesting is invoked via the Python API or via the test suite. There is no `--backtest` CLI flag. |
| No database migration CLI | The SQLite schema is applied automatically via `DatabaseManager.initialize()` on every startup (idempotent). |
| No Alembic / migration history | Schema changes must be applied manually or by dropping and reinitializing the database. |
| LOG_ROTATION / LOG_RETENTION_DAYS | These keys appear in `.env.example` for documentation purposes only. `Settings` does not read them (uses `extra="ignore"`). |
| DATABASE_URL | Appears in `.env.example` for reference only. `Settings` uses `DB_FILE_PATH`. |

---

## Project Directory Structure

```
bot_ha/
├── README.md                    ← You are here
├── pyproject.toml               ← Project config, test settings, coverage thresholds
├── requirements.txt             ← Production runtime dependencies
├── requirements-dev.txt         ← Development and testing dependencies
├── .env.example                 ← Environment variable template (copy to .env)
├── .gitignore                   ← Excludes .env, data/, logs/, *.db
│
├── docs/                        ← All documentation
│   ├── README.md                ← Documentation hub and index
│   ├── USER_MANUAL.md
│   ├── INSTALLATION_GUIDE.md
│   ├── QUICK_START.md
│   ├── CONFIGURATION_GUIDE.md
│   ├── API_KEY_SETUP.md
│   ├── SECURITY_GUIDE.md
│   ├── MARKET_SCANNER_MANUAL.md
│   ├── STRATEGY_MANUAL.md
│   ├── RISK_MANAGEMENT_MANUAL.md
│   ├── BACKTESTING_MANUAL.md
│   ├── PAPER_TRADING_MANUAL.md
│   ├── LIVE_TRADING_MANUAL.md
│   ├── DASHBOARD_MANUAL.md
│   ├── TERMINAL_MANUAL.md
│   ├── TRADE_HISTORY_AND_ANALYTICS.md
│   ├── MONITORING_AND_LOGS.md
│   ├── TESTING_GUIDE.md
│   ├── TROUBLESHOOTING.md
│   ├── BACKUP_AND_RECOVERY.md
│   ├── MAINTENANCE_GUIDE.md
│   ├── DEVELOPER_GUIDE.md
│   ├── FAQ.md
│   ├── FULL_REFERENCE.md        ← Combined mega-reference (all docs in one file)
│   ├── PROJECT_SPEC.md
│   ├── ARCHITECTURE.md
│   ├── STRATEGY_RULES.md
│   ├── RISK_MODEL.md
│   ├── API_CONTRACTS.md
│   ├── TEST_PLAN.md
│   ├── DEVELOPMENT_LOG.md
│   ├── RUNBOOK.md
│   └── CHANGELOG.md
│
├── src/                         ← Application source code
│   ├── config/                  ← Settings, validators
│   ├── exchange/                ← Binance REST + WebSocket
│   ├── scanner/                 ← Market scanner
│   ├── market_data/             ← Kline feeds, ring buffers
│   ├── indicators/              ← Heikin-Ashi + swing detection
│   ├── strategy/                ← State machine (15m + 1m)
│   ├── risk/                    ← Sizing, leverage, step sizes
│   ├── execution/               ← Order submission + fills
│   ├── positions/               ← Position manager + SL/TP
│   ├── state/                   ← SQLite WAL store
│   ├── backtesting/             ← Chronological replay engine
│   ├── monitoring/              ← Logging + secret redaction
│   ├── orchestrator.py          ← TradingBotOrchestrator
│   └── main.py                  ← CLI entrypoint
│
├── tests/
│   ├── unit/                    ← Unit tests (280 passing)
│   └── integration/             ← Integration + testnet tests
│
├── data/                        ← SQLite database (auto-created, git-ignored)
└── logs/                        ← Log files (auto-created, git-ignored)
```

---

## Implementation Status

**v1.0.0 — All 15 development phases complete.**

| Phase | Description | Status |
|---|---|---|
| P01 | Master project brief, architecture, foundation | ✅ Done |
| P02 | Binance exchange connector | ✅ Done |
| P03 | Market scanner | ✅ Done |
| P04 | Market data engine | ✅ Done |
| P05 | Heikin-Ashi engine | ✅ Done |
| P06 | Multi-timeframe strategy engine | ✅ Done |
| P07 | Swing detection engine | ✅ Done |
| P08 | Risk management and position sizing | ✅ Done |
| P09 | Binance order execution engine | ✅ Done |
| P10 | Protective order manager | ✅ Done |
| P11 | Position manager and state recovery | ✅ Done |
| P12 | Main orchestration and full integration | ✅ Done |
| P13 | Chronological backtesting (Amendment 01) | ✅ Done |
| P14 | Binance Testnet validation and fault injection | ✅ Done |
| P15 | Independent audit and deployment readiness | ✅ Done |
| A02 | Complete documentation system (22 manuals) | ✅ Done |

---

## Running Tests

```bash
source .venv/bin/activate

# Full offline suite with branch coverage report
pytest --cov=src --cov-report=term-missing --cov-fail-under=80

# Fast unit tests only
pytest tests/unit/ -v

# Integration tests (no network required)
pytest tests/integration/ -v -m "not testnet"

# Live Testnet tests (requires Testnet API keys in .env)
pytest -m testnet -v
```

Baseline: **280 passed, 3 skipped (testnet-gated), 86.71 % branch coverage**.

---

## Security Warnings

> **⚠️ NEVER commit your `.env` file to version control.**  
> `.gitignore` excludes `.env`, `data/`, `logs/`, and `*.db` by default. Verify this before every push.

> **⚠️ NEVER grant Withdrawal permissions to any API key used by this bot.**  
> Use Futures trading permissions only (read + trade). Enable IP allowlisting in production.

> **⚠️ Test thoroughly on Testnet before enabling production mode.**  
> At minimum: run the full test suite, validate config, run 24+ hours on Testnet, and review all log output.

> **⚠️ This software is provided for educational purposes.**  
> Algorithmic trading involves substantial financial risk. Past performance does not guarantee future results.

---

## License

See repository for license details.
