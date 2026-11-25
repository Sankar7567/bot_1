# Market Scanner Manual

Implemented in [src/scanner/scanner.py](../src/scanner/scanner.py) and [src/scanner/filters.py](../src/scanner/filters.py).

---

## 1. Eligible Universe Rules
Every scan fetches `exchangeInfo` and 24-hour tickers (`ticker/24hr`), then filters contracts using [src/scanner/filters.py](../src/scanner/filters.py):
1. **Quote Asset:** Must match `SCANNER_QUOTE_ASSET` (default `USDT`).
2. **Contract Type:** Must match `SCANNER_CONTRACT_TYPE` (default `PERPETUAL`).
3. **Contract Status:** Must be `"TRADING"` (excludes settling, delisted, or pre-trading contracts).
4. **Minimum Liquidity:** 24-hour `quoteVolume` must be `>= SCANNER_MIN_QUOTE_VOLUME_24H` (default `1,000,000.0` USDT).
5. **Valid Last Price & Gain:** Non-zero valid price and ticker statistics.

---

## 2. Ranking & Refresh Timing
- **Metric:** Rolling 24-hour percentage price change (`priceChangePercent`), sorted descending.
- **Tie-Breaker:** Alphabetical by `symbol` ascending if two contracts have identical 24h gain.
- **Top-N Selection:** Top `SCANNER_TOP_N` (default `20`) contracts.
- **Refresh Interval:** Every `SCANNER_REFRESH_INTERVAL_SEC` seconds (default `60`).
- **In-Flight Universe Protection:** `TradingBotOrchestrator.sync_universe()` keeps WebSocket streams active for any symbol that currently has an open position or active confirmation window even if it temporarily drops out of the Top 20.
- **Failure Handling:** Raises `EmptyUniverseError` or `StaleDataError` if no eligible symbols or tickers are available; the orchestrator logs a warning and retries on the next 60s cycle without fabricating rankings.
