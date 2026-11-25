# Paper Trading Manual

> [!IMPORTANT]
> **Status: `NOT IMPLEMENTED` (as a standalone live-market virtual-wallet mode).**  
> There is no live-feed virtual-balance paper-trading adapter in `src/`. Instead, the project provides two implemented non-production workflows:
> 1. **Binance Futures Testnet (`APP_ENV=testnet`)** — Live-market Testnet execution using virtual Testnet USDT (`https://testnet.binancefuture.com`).
> 2. **Chronological Historical Replay (`src/backtesting/`)** — Offline simulated wallet, fees, slippage, and funding (`AccountLedger` + `HistoricalExchangeAdapter`).

---

## Option 1: Binance Futures Testnet (Recommended Live Simulation)
- **Cannot Submit Production Orders:** When `APP_ENV=testnet` and `BINANCE_REST_BASE_URL=https://testnet.binancefuture.com`, all REST and WebSocket traffic goes strictly to Binance's Testnet sandbox.
- **Virtual Balance & Resets:** Managed in your Testnet account at `https://testnet.binancefuture.com` (Testnet provides a faucet/reset in the web UI).
- **Persistence & Recovery:** Identical to production—positions, orders, windows, and audit events are saved in `data/trading_bot.db` and reconciled via `StartupReconciler` on restart.
- **Setup:** See [Quick Start Guide](QUICK_START.md) and [API Key Setup](API_KEY_SETUP.md).

## Option 2: Chronological Replay (Offline Virtual Ledger)
- Uses `AccountLedger(initial_balance=...)` and `ExecutionAssumptions` (taker fees, half-spread, entry/stop slippage, 8h funding) against historical candles.
- See [Backtesting Manual](BACKTESTING_MANUAL.md).
