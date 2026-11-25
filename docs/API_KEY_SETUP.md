# API Key Setup

This manual explains how to obtain, configure, and protect Binance API credentials for **Binance Futures Testnet** and **Binance USDⓈ-M Futures Production**.

---

## 1. Public Data vs. Private Account Access

| Use Case | Private API Keys Required? | Where Data Comes From |
|---|---|---|
| **Offline Chronological Backtesting** | **No** | Local `HistoricalMarketStore` historical candles |
| **Public Ping / Server Time / Public Market Data** | **No** | Public REST endpoints (`/fapi/v1/ping`, `/fapi/v1/time`, `/fapi/v1/ticker/24hr`, `/fapi/v1/klines`) |
| **Binance Futures Testnet Bot Run** | **Yes (Testnet keys)** | `https://testnet.binancefuture.com` |
| **Live Production Trading** | **Yes (Production keys)** | `https://fapi.binance.com` (requires `APP_ENV=production`) |

> [!IMPORTANT]
> **API keys alone do not enable live trading.** By default, `APP_ENV=testnet` and `BINANCE_REST_BASE_URL=https://testnet.binancefuture.com`. Live trading requires explicitly changing `APP_ENV=production` and updating the REST/WS URLs (see [Live Trading Manual](LIVE_TRADING_MANUAL.md)).

---

## 2. Creating API Credentials

### Option A: Binance Futures Testnet (Recommended)
1. Log in to [https://testnet.binancefuture.com](https://testnet.binancefuture.com).
2. Open the **API Key** panel at the bottom of the Futures Testnet UI.
3. Copy the generated **API Key** and **Secret Key**.

### Option B: Binance Production USDⓈ-M Futures
1. Log in to Binance → **Account** → **API Management**.
2. Create a new API key (system-generated HMAC or supported key type).
3. Enable **only**:
   - **Enable Reading** (for account balance, positions, open orders, listenKey)
   - **Enable Futures** (for placing/cancelling orders, setting leverage & isolated margin)
4. **Keep Withdrawals DISABLED.** Never enable withdrawal or universal transfer permissions.
5. **Restrict access by IP** to your server's static IP address.

---

## 3. Where to Put API Keys

1. Copy `.env.example` to `.env` in the repository root (`/home/sankar/bot_ha/.env`):
   ```bash
   cp .env.example .env
   ```
2. Edit `.env` using the exact variable names read by `Settings`:
   ```env
   APP_ENV=testnet
   BINANCE_API_KEY=your_api_key_here
   BINANCE_API_SECRET=your_api_secret_here
   BINANCE_REST_BASE_URL=https://testnet.binancefuture.com
   BINANCE_WS_BASE_URL=wss://stream.binancefuture.com/ws
   ```
3. **Restart Required:** Changes to `.env` require restarting `python -m src.main`.

### Production Example (Requires Explicit Activation)
```env
APP_ENV=production
BINANCE_API_KEY=your_production_api_key_here
BINANCE_API_SECRET=your_production_api_secret_here
BINANCE_REST_BASE_URL=https://fapi.binance.com
BINANCE_WS_BASE_URL=wss://fstream.binance.com/ws
```

---

## 4. Credential Protection & `.gitignore` Verification

`.gitignore` explicitly excludes `.env`, `.env.*` (except `.env.example`), `data/`, `*.db`, and `logs/`. Verify that `.env` is not tracked by Git:
```bash
git status --ignored
git ls-files .env
```
`git ls-files .env` must return **empty output**.

### If Credentials Are Accidentally Exposed
1. **Immediately revoke/delete** the exposed API key in Binance API Management (or Testnet UI).
2. Stop the bot (`Ctrl+C` / `SIGTERM`) and inspect exchange positions/open orders.
3. Generate a new key pair, update `.env`, and verify with `python -m src.main --check-config`.
4. If committed to Git history, rewrite/purge Git history before pushing, or rotate keys immediately (always rotate keys first).
