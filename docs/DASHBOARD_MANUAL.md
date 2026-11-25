# Dashboard Manual

> [!IMPORTANT]
> **Status: `NOT IMPLEMENTED`.**  
> This repository does **not** include a web dashboard, frontend UI (React/Next.js/Vue), or HTTP/SSE API server. Do not expect a browser interface on `localhost`.

---

## Implemented Alternatives for Every Monitoring Task

| Intended Dashboard View | Implemented Equivalent | Where Documented |
|---|---|---|
| **Overview (Mode, Health, Equity, P&L)** | `python -m src.main --check-config`, `tail -f logs/trading_bot.log`, SQLite queries | [Terminal Manual](TERMINAL_MANUAL.md), [Monitoring & Logs](MONITORING_AND_LOGS.md) |
| **Market Scanner (Top-20, 24h %, Volume)** | Scanner log lines in `logs/trading_bot.log` | [Market Scanner Manual](MARKET_SCANNER_MANUAL.md) |
| **Strategy Monitor (15m Window, 1m Confirm)** | `strategy_windows` SQLite table + `strategy.engine` logs | [Strategy Manual](STRATEGY_MANUAL.md), [Trade History & Analytics](TRADE_HISTORY_AND_ANALYTICS.md) |
| **Trading Terminal (Open Position, SL/TP)** | `positions` & `orders` SQLite tables + Binance Testnet/Futures UI | [Trade History & Analytics](TRADE_HISTORY_AND_ANALYTICS.md), [Live Trading Manual](LIVE_TRADING_MANUAL.md) |
| **Backtesting UI** | `ChronologicalBacktestRunner` Python API + `BacktestReport` | [Backtesting Manual](BACKTESTING_MANUAL.md) |
| **Paper Trading UI** | Binance Futures Testnet UI (`testnet.binancefuture.com`) | [Paper Trading Manual](PAPER_TRADING_MANUAL.md) |
| **Settings UI** | `.env` file + `Settings` in `src/config/settings.py` | [Configuration Guide](CONFIGURATION_GUIDE.md) |
