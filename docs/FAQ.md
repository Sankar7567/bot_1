# Frequently Asked Questions (FAQ)

**Q: Is there a web dashboard or browser UI?**  
A: No. A web dashboard/frontend is `NOT IMPLEMENTED`. Operate and monitor the bot via `python -m src.main`, `logs/trading_bot.log`, and `sqlite3 data/trading_bot.db`.

**Q: Does this bot have a standalone live paper-trading mode?**  
A: No standalone live virtual-wallet adapter is implemented (`NOT IMPLEMENTED`). Use **Binance Futures Testnet** (`APP_ENV=testnet`, the default) for live-market sandbox trading, or **Chronological Backtesting** (`src/backtesting/`) for offline replay.

**Q: Can I accidentally trade real money right after installing?**  
A: No. `APP_ENV` defaults to `testnet` and `BINANCE_REST_BASE_URL` defaults to `https://testnet.binancefuture.com`. Live trading requires explicitly setting `APP_ENV=production` and production URLs/keys in `.env`.

**Q: Why does a trade get rejected even after a 1m HA confirmation?**  
A: Either no confirmed 5L/5R swing pivot exists on 1m OHLC, available isolated margin is insufficient, notional is below `minNotional`, or the estimated liquidation price is tighter than the Stop-Loss.

**Q: What happens to an open position if I press `Ctrl+C`?**  
A: The bot shuts down gracefully, leaving the exchange position and its exchange-side `STOP_MARKET` and `TAKE_PROFIT_MARKET` orders active on Binance. On restart, `StartupReconciler` recovers the position.
