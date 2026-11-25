# Live Trading Manual

> [!CAUTION]
> **Live trading is `DISABLED` by default** (`APP_ENV=testnet`, `BINANCE_REST_BASE_URL=https://testnet.binancefuture.com`). Activating `APP_ENV=production` executes real-money orders on Binance USDⓈ-M Futures with 10% nominal equity risk per trade and up to maximum bracket leverage. **Read and complete every step below before enabling production.**

---

## 1. Pre-Activation Safety Checklist
- [ ] Completed full validation on **Binance Futures Testnet** (`APP_ENV=testnet`) and verified SQLite + exchange reconciliation.
- [ ] Created production API keys with **Enable Reading** + **Enable Futures** ONLY (**Withdrawals DISABLED**, **IP Restriction ENABLED**).
- [ ] Populated `.env` on the production host with `chmod 600 .env` and verified `git ls-files .env` is empty.
- [ ] Verified Binance Futures account has **no conflicting open positions or orphan conditional orders**.
- [ ] Understood the **10% nominal risk budget** (`NOMINAL_RISK_RATIO=0.10`): consecutive Stop-Losses compound drawdowns (~10% equity per loss, plus fees/slippage if extreme gaps occur).

---

## 2. Production Configuration (`.env`)
```env
APP_ENV=production
BINANCE_API_KEY=your_production_api_key_here
BINANCE_API_SECRET=your_production_api_secret_here
BINANCE_REST_BASE_URL=https://fapi.binance.com
BINANCE_WS_BASE_URL=wss://fstream.binance.com/ws
```
Validate before launching:
```bash
python -m src.main --check-config
```
Confirm `env=production`, `binance_rest_base_url='https://fapi.binance.com'`, and `***REDACTED***` for credentials.

---

## 3. Exchange & Startup Safeguards
When `python -m src.main` starts:
1. **One-Way Mode Enforcement:** `TradingBotOrchestrator.startup()` checks `get_position_mode()` and switches to One-Way mode if Hedge mode is active.
2. **Startup Reconciliation (`StartupReconciler`):** Reconciles DB vs. exchange positions and open orders before starting the scanner.
3. **Isolated Margin & Bracket Leverage:** Every entry enforces `ISOLATED` margin and validates that liquidation price is strictly safer than Stop-Loss.

---

## 4. Disabling New Entries, Shutdown & Emergency Procedures
- **Stop New Entries / Shutdown Bot:** Press `Ctrl+C` (`SIGINT`) or send `SIGTERM`. The bot stops WebSocket feeds and cancels monitoring cleanly.
- **Important:** Graceful shutdown **does not close an open position**; your exchange-side `STOP_MARKET` and `TAKE_PROFIT_MARKET` (`reduceOnly=true`) orders remain active on Binance.
- **Emergency Manual Close:** If an unexpected position lacks verified SL/TP orders:
  1. Stop the bot (`SIGINT` / `SIGTERM`).
  2. Log in to Binance Futures Web UI → **Positions** → click **Market Close**.
  3. Open **Open Orders** and cancel any orphaned conditional orders.
  4. Inspect `logs/trading_bot.log` and `data/trading_bot.db` before restarting.
