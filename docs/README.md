# Binance HA Trading Platform — Documentation Hub

Welcome to the documentation for the **Binance USDⓈ-M Futures Heikin-Ashi Trading Bot** (`binance-ha-bot`).

> [!IMPORTANT]
> This project is a **Python 3.11+ asynchronous CLI/backend trading engine** (`python -m src.main`) with a **Python chronological backtesting library** (`src/backtesting/`). A web dashboard, HTTP/SSE API server, and standalone live paper-trading virtual wallet are **`NOT IMPLEMENTED`**—use **Binance Futures Testnet** (`APP_ENV=testnet`, default) or **Chronological Backtesting** (`ChronologicalBacktestRunner`) for non-production testing.

---

## Documentation Directory

### Getting Started & Setup
- [User Manual](USER_MANUAL.md) — End-to-end operator overview and daily workflow.
- [Installation Guide](INSTALLATION_GUIDE.md) — Fresh Ubuntu/Linux installation from scratch.
- [Quick Start Guide](QUICK_START.md) — From clean install to `--check-config`, Testnet verification, and chronological backtest.
- [Configuration Guide](CONFIGURATION_GUIDE.md) — Every `Settings` field, `.env` variable, default, and validator range.
- [API Key Setup](API_KEY_SETUP.md) — Binance Testnet & Futures API keys, permissions, IP restrictions, and `.env` placement.
- [Security Guide](SECURITY_GUIDE.md) — Credential protection, `SecretStr`, log redaction, `.gitignore`, and exposure response.

### Trading, Strategy & Risk
- [Market Scanner Manual](MARKET_SCANNER_MANUAL.md) — Top-20 24h gainers, liquidity filters, and 60s refresh behavior.
- [Strategy Manual](STRATEGY_MANUAL.md) — 15m Heikin-Ashi trigger, frozen top-20 snapshot, 1m confirmation, and tie-breaking.
- [Risk Management Manual](RISK_MANAGEMENT_MANUAL.md) — 5L/5R swing pivots, 0.1% stop buffer, 10% risk budget, 3:1 RR, leverage brackets, and 1-position lock.
- [Backtesting Manual](BACKTESTING_MANUAL.md) — Chronological non-lookahead replay (`ChronologicalBacktestRunner`), assumptions, metrics, and limitations.
- [Paper Trading Manual](PAPER_TRADING_MANUAL.md) — Status (`NOT IMPLEMENTED` as standalone live mode) and how to use Testnet or Chronological Replay instead.
- [Live Trading Manual](LIVE_TRADING_MANUAL.md) — Safety-first production activation checklist, isolated margin, one-way mode, and emergency stop.

### Interfaces, Operations & Maintenance
- [Terminal Manual](TERMINAL_MANUAL.md) — Exact verified CLI, `pytest`, SQLite, and Python backtest commands.
- [Dashboard Manual](DASHBOARD_MANUAL.md) — Status (`NOT IMPLEMENTED`) and CLI/SQLite/log monitoring equivalents.
- [Trade History & Analytics](TRADE_HISTORY_AND_ANALYTICS.md) — Querying SQLite (`positions`, `orders`, `strategy_windows`, `audit_log`) and `BacktestReport`.
- [Monitoring & Logs](MONITORING_AND_LOGS.md) — Rotating logs (`logs/trading_bot.log`), `RedactingFormatter`, and log levels.
- [Testing Guide](TESTING_GUIDE.md) — Unit, integration, fault-injection, chronological backtest, and `@pytest.mark.testnet` suites.
- [Troubleshooting](TROUBLESHOOTING.md) — Diagnostics and fixes for startup, API, WebSocket, strategy, risk, and database issues.
- [Backup & Recovery](BACKUP_AND_RECOVERY.md) — Secret-free SQLite WAL & config backups and restoration.
- [Maintenance Guide](MAINTENANCE_GUIDE.md) — Dependency updates, code updates, Git workflow, and safe restarts.
- [Developer Guide](DEVELOPER_GUIDE.md) — Architecture, module responsibilities, Mermaid data flow, schemas, and extension points.
- [FAQ](FAQ.md) — Frequently asked questions.

### Engineering & Specification References
- [Project Spec](PROJECT_SPEC.md) | [Architecture](ARCHITECTURE.md) | [Strategy Rules](STRATEGY_RULES.md) | [Risk Model](RISK_MODEL.md)
- [API Contracts](API_CONTRACTS.md) | [Test Plan](TEST_PLAN.md) | [Runbook](RUNBOOK.md) | [Changelog](CHANGELOG.md) | [Development Log](DEVELOPMENT_LOG.md)
