# Troubleshooting

---

## 1. Startup & Configuration Failures
- **Symptoms:** `ValidationError` on `python -m src.main --check-config`.
- **Causes:** Invalid value in `.env` (e.g. `GLOBAL_MAX_POSITIONS != 1`, `NOMINAL_RISK_RATIO > 0.25`, `REWARD_RISK_RATIO < 1.0`).
- **Resolution:** Run `python -m src.main --check-config`, fix `.env` per [Configuration Guide](CONFIGURATION_GUIDE.md).

## 2. Missing Dependencies / Python Version
- **Symptoms:** `ModuleNotFoundError` or syntax error.
- **Resolution:** Ensure `.venv` is activated (`source .venv/bin/activate`), `python --version` is 3.11+, and run `pip install -r requirements-dev.txt`.

## 3. Invalid API Keys / Permission Errors (`-2015`, `-2014`)
- **Symptoms:** Auth failure on startup or order submission.
- **Causes:** Wrong key/secret, Testnet key used against Production URL (or vice versa), IP not whitelisted, or Futures permission disabled.
- **Resolution:** Verify `APP_ENV` matches `BINANCE_REST_BASE_URL` and check permissions in [API Key Setup](API_KEY_SETUP.md). **Stop trading until resolved.**

## 4. Rate Limits (`429` / `-1003`) & Network Timeouts
- **Symptoms:** `RateLimitError` or HTTP retry warnings in `logs/trading_bot.log`.
- **Resolution:** `BinanceFuturesClient` (`src/exchange/rate_limiter.py`) handles backoff/retries (`HTTP_MAX_RETRIES=3`). If persistent, verify outbound connectivity to `testnet.binancefuture.com` / `fapi.binance.com`.

## 5. WebSocket Disconnections (`UserDataStream` / `MarketDataFeed`)
- **Symptoms:** `User Data Stream closed` or `MarketDataFeed` reconnect warnings.
- **Resolution:** Both streams auto-reconnect with exponential backoff, and `_position_watchdog_loop` polls exchange positions as a safety fallback.

## 6. No Strategy Signals or Scanner Not Updating
- **Symptoms:** Bot runs, no trades opened.
- **Causes:** No symbol meets `SCANNER_MIN_QUOTE_VOLUME_24H` (`1,000,000` USDT), 15m HA is Doji, or 1m HA did not align within the 15m window.
- **Diagnostic:** `grep -E "Scanner|Window|Signal" logs/trading_bot.log | tail -n 30`.

## 7. Risk Validation Rejection (`Trade REJECTED`)
- **Symptoms:** `No confirmed 5L/5R swing pivot`, `Liquidation price is tighter than stop-loss`, or `minNotional` / `available_margin` rejection.
- **Resolution:** Expected protective behavior; the 15m window is consumed cleanly. Ensure sufficient Testnet/Futures USDT balance.

## 8. Protective Order Failure & Unexpected Account State
- **Symptoms:** `CRITICAL` log `Failed to place protective orders` or `StartupReconciler` `MANUAL_INTERVENTION_REQUIRED`.
- **Resolution:** **Stop the bot immediately (`Ctrl+C`)**, inspect Binance **Positions** and **Open Orders** in the web UI, close any unprotected position manually, and inspect `logs/trading_bot.log` + `data/trading_bot.db`.
