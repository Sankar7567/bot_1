# User Manual

This handbook explains how an operator runs and monitors the **Binance USDⓈ-M Futures Heikin-Ashi Trading Bot** (`binance-ha-bot`).

---

## 1. What the Application Does
`binance-ha-bot` is an asynchronous Python trading engine that:
1. Scans Binance USDⓈ-M USDT perpetual contracts every 60 seconds and ranks the **Top 20** by 24-hour percentage gain (filtered by liquidity).
2. Evaluates completed **15-minute Heikin-Ashi** candles at each 15m boundary, freezing the Top-20 snapshot.
3. Monitors eligible candidates across subsequent **1-minute Heikin-Ashi** closes until the first aligned confirmation wins globally (rank tie-broken).
4. Validates a **5-left / 5-right OHLC swing pivot** stop-loss, sizes the position for a **10% nominal equity risk budget** and **3:1 reward-to-risk**, and enforces a **global 1-position lock** with **ISOLATED** margin.
5. Places an exchange `MARKET` entry and native `STOP_MARKET` + `TAKE_PROFIT_MARKET` (`reduceOnly=true`) protective orders, cancelling the sibling order when one fills.
6. Persists all state to SQLite (`data/trading_bot.db`) and reconciles exchange vs. DB state on startup.

---

## 2. Operating Modes

| Mode | Status | How to Use |
|---|---|---|
| **Chronological Backtesting** | `IMPLEMENTED` | Python API `ChronologicalBacktestRunner` ([Backtesting Manual](BACKTESTING_MANUAL.md)) |
| **Binance Futures Testnet** | `REQUIRES CONFIGURATION` | Default (`APP_ENV=testnet`), `python -m src.main` with Testnet keys |
| **Live Binance Production** | `DISABLED` (by default) | Explicit `APP_ENV=production` + production keys ([Live Trading Manual](LIVE_TRADING_MANUAL.md)) |
| **Web Dashboard / UI** | `NOT IMPLEMENTED` | Use CLI, SQLite queries, and logs ([Dashboard Manual](DASHBOARD_MANUAL.md)) |
| **Standalone Live Paper Mode** | `NOT IMPLEMENTED` | Use Testnet or Chronological Replay ([Paper Trading Manual](PAPER_TRADING_MANUAL.md)) |

---

## 3. Daily Operator Workflow
1. **Preflight Check:** `python -m src.main --check-config`
2. **Launch Engine:** `python -m src.main`
3. **Monitor Logs:** `tail -f logs/trading_bot.log` ([Monitoring & Logs](MONITORING_AND_LOGS.md))
4. **Inspect Positions & Orders:** Query `data/trading_bot.db` ([Trade History & Analytics](TRADE_HISTORY_AND_ANALYTICS.md))
5. **Graceful Shutdown:** Press `Ctrl+C` (`SIGINT`) or send `SIGTERM`. Note: open exchange positions and their exchange-side SL/TP orders remain active on Binance unless closed manually or via emergency close.
