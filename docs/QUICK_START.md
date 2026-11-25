# Quick Start Guide

This guide takes you from a fresh installation to a verified **Binance Futures Testnet** run and an offline **Chronological Backtest**.

> [!NOTE]
> Because a web dashboard and standalone live paper-trading mode are **`NOT IMPLEMENTED`**, non-production testing uses **Binance Futures Testnet** (`APP_ENV=testnet`, the default) or the **Chronological Backtesting Engine** (`src/backtesting/`).

---

## Path A: Launch on Binance Futures Testnet (Recommended First Run)

### 1. Install & Activate Environment
Follow Steps 1–4 of the [Installation Guide](INSTALLATION_GUIDE.md):
```bash
cd bot_ha
source .venv/bin/activate
```

### 2. Configure `.env` for Testnet
```bash
cp .env.example .env
```
Open `.env` and populate your **Testnet** API key and secret from `https://testnet.binancefuture.com` (see [API Key Setup](API_KEY_SETUP.md)):
```env
APP_ENV=testnet
BINANCE_API_KEY=your_binance_testnet_api_key_here
BINANCE_API_SECRET=your_binance_testnet_api_secret_here
BINANCE_REST_BASE_URL=https://testnet.binancefuture.com
BINANCE_WS_BASE_URL=wss://stream.binancefuture.com/ws
```

### 3. Validate Configuration
```bash
python -m src.main --check-config
```
Confirm `env=testnet` and `***REDACTED***` appear for both credential fields.

### 4. Verify Testnet Connectivity (Optional Testnet Preflight)
```bash
pytest tests/integration/test_exchange_integration.py -v
```
This runs `test_public_ping_testnet` and (when credentials are set in `.env` or environment) `test_authenticated_testnet_account_info`.

### 5. Start the Trading Bot
```bash
python -m src.main
```
During startup, the bot automatically:
1. Connects to Binance Futures Testnet and synchronizes server time.
2. Verifies **One-Way** position mode (switches from Hedge mode if needed).
3. Runs `StartupReconciler` to check for any existing open exchange/DB positions.
4. Starts the 60-second `MarketScanner` (Top-20 24h USDT perpetual gainers), 15m/1m WebSocket market data feeds, and User Data Stream.

### 6. Monitor Activity
In a second terminal:
- **Live logs**:
  ```bash
  tail -f logs/trading_bot.log
  ```
- **Inspect positions & orders in SQLite**:
  ```bash
  sqlite3 data/trading_bot.db "SELECT position_id, symbol, side, status, entry_price, realized_pnl FROM positions;"
  ```

### 7. Stop Gracefully
Press `Ctrl+C` (`SIGINT`) in the bot terminal. The bot stops streams, closes the listenKey, and closes the SQLite connection cleanly.

---

## Path B: Run an Offline Chronological Backtest (No API Keys Needed)

Run the built-in 12-test chronological replay suite (covering all 17 Amendment 01 invariants) to see the backtester in action:
```bash
pytest tests/unit/test_backtesting_chronological.py --no-cov -v
```
To run a custom backtest against your own historical candles, see the [Backtesting Manual](BACKTESTING_MANUAL.md).
