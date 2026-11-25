# Trade History and Analytics

All live and Testnet trade history is persisted in SQLite (`data/trading_bot.db` by default). Backtest analytics are returned in `BacktestReport`.

---

## 1. SQLite Tables (`src/state/database.py`)

| Table | Key Columns |
|---|---|
| `positions` | `position_id`, `symbol`, `side`, `status` (`PENDING_ENTRY`/`OPEN`/`PENDING_EXIT`/`CLOSED`), `quantity`, `entry_price`, `exit_price`, `leverage`, `stop_loss_price`, `take_profit_price`, `realized_pnl`, `exit_reason`, `sl_client_order_id`, `tp_client_order_id`, `created_at`, `closed_at` |
| `orders` | `order_id`, `client_order_id`, `exchange_order_id`, `symbol`, `position_id`, `order_type`, `side`, `status`, `stop_price`, `orig_qty`, `executed_qty`, `avg_price`, `reduce_only` |
| `strategy_windows` | `window_id`, `start_timestamp`, `end_timestamp`, `status`, `winning_symbol`, `window_consumed`, `payload` |
| `audit_log` | `log_id`, `event_type`, `entity_type`, `entity_id`, `details`, `created_at` |

---

## 2. Useful SQLite Queries
```bash
# Recent closed & open positions
sqlite3 -header -column data/trading_bot.db \
  "SELECT position_id, symbol, side, status, quantity, entry_price, exit_price, realized_pnl, exit_reason FROM positions ORDER BY created_at DESC LIMIT 20;"

# Orders for a specific position
sqlite3 -header -column data/trading_bot.db \
  "SELECT client_order_id, order_type, side, status, stop_price, executed_qty, avg_price FROM orders ORDER BY created_at DESC LIMIT 20;"

# Recent audit events
sqlite3 -header -column data/trading_bot.db \
  "SELECT datetime(created_at/1000, 'unixepoch') AS utc_time, event_type, entity_id, details FROM audit_log ORDER BY log_id DESC LIMIT 25;"
```

---

## 3. Backtest Analytics (`BacktestReport`)
`ChronologicalBacktestRunner.run()` returns a `BacktestReport` containing `performance`, `risk`, `costs`, `equity`, `market_selection`, `strategy`, `data_quality`, and per-trade `TradeAuditRecord` entries. See [Backtesting Manual](BACKTESTING_MANUAL.md).
