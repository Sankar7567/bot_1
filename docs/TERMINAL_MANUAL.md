# Terminal Command Reference

All verified terminal commands for `binance-ha-bot`. Always activate the virtual environment first (`source .venv/bin/activate`).

---

## 1. Application CLI (`src/main.py`)

| Command | Purpose |
|---|---|
| `python -m src.main --check-config` | Validates `.env` / environment via `Settings`, prints sanitized config (`***REDACTED***` for secrets), and exits `0`. |
| `python -m src.main` | Starts `TradingBotOrchestrator` (connectivity check, time sync, one-way mode, startup reconciliation, 60s scanner, 15m/1m WS, User Data Stream). |
| `Ctrl+C` (`SIGINT`) / `kill -TERM <pid>` | Requests graceful shutdown (`orchestrator.request_shutdown()`), stops streams, closes listenKey & SQLite DB. |

> [!NOTE]
> `src/main.py` has **only** `--check-config` (`--help` prints usage). Dedicated CLI subcommands for backtesting, paper trading, standalone scanner, or DB migrations are `NOT IMPLEMENTED`.

---

## 2. Database & Diagnostics Commands
- **Initialize SQLite schema manually (normally automatic on startup):**
  ```bash
  python -c "from src.state.database import DatabaseManager; db = DatabaseManager(); db.initialize(); db.close()"
  ```
- **Inspect positions, orders, and audit log:** See [Trade History & Analytics](TRADE_HISTORY_AND_ANALYTICS.md).
- **Follow logs:**
  ```bash
  tail -f logs/trading_bot.log
  ```

---

## 3. Testing & Backtest Commands
- **Full offline test suite + branch coverage (`>= 80%`):**
  ```bash
  pytest --cov=src --cov-fail-under=80 -q
  ```
- **Chronological Backtesting validation suite (all 17 Amendment 01 tests):**
  ```bash
  pytest tests/unit/test_backtesting_chronological.py --no-cov -v
  ```
- **Live Binance Testnet tests (requires `BINANCE_API_KEY` & `BINANCE_API_SECRET`):**
  ```bash
  pytest -m testnet -v
  ```
- **Bytecode compilation check:**
  ```bash
  python -m compileall -q src tests
  ```
