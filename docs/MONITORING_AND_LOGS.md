# Monitoring and Logs

Implemented in [src/monitoring/logger.py](../src/monitoring/logger.py).

---

## 1. Log Locations & Rotation
- **Default Log File:** `logs/trading_bot.log` (configured via `LOG_FILE` and `LOG_DIR` in `.env`).
- **Console Output:** Enabled by default (`StreamHandler` to `stderr`/`stdout`).
- **Rotation:** `RotatingFileHandler` rotates at **`10 MB`** (`max_bytes=10 * 1024 * 1024`) and keeps **`5` backup files** (`trading_bot.log.1` through `.5`).
- **Timestamps:** Formatted in UTC: `YYYY-MM-DD HH:MM:SS UTC [LEVEL] [logger:func:line] message`.

## 2. Log Levels
Controlled by `LOG_LEVEL` in `.env` (default `INFO`): `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`.

## 3. Automatic Secret Redaction
`RedactingFormatter` automatically masks `api_key`, `secret`, `signature=`, and `X-MBX-APIKEY` values with `***REDACTED***` before writing to console or disk.

## 4. Live Log Monitoring Commands
```bash
# Follow all logs in real time
tail -f logs/trading_bot.log

# Filter errors and warnings
grep -E "\[(WARNING|ERROR|CRITICAL)\]" logs/trading_bot.log | tail -n 50

# Watch trade entries, exits, and risk rejections
grep -E "Position|CLOSED|REJECTED|Watchdog" logs/trading_bot.log | tail -n 30
```
