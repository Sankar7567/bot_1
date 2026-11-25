# Backup and Recovery

---

## 1. What to Back Up
- **SQLite State Database:** `data/trading_bot.db` (plus `-wal` / `-shm` if running, or use SQLite's online `.backup` command).
- **Configuration:** `.env` (contains API secrets—**store encrypted and never include in shared archives!**).

## 2. Safe Database Backup (Without Stopping or Corrupting WAL)
```bash
mkdir -p backups
sqlite3 data/trading_bot.db ".backup 'backups/trading_bot_$(date -u +%Y%m%d_%H%M%S).db'"
```
Verify backup integrity:
```bash
sqlite3 backups/trading_bot_*.db "PRAGMA integrity_check;"
```
Expected output: `ok`.

## 3. Restoring a Backup
1. Stop the bot gracefully (`Ctrl+C` / `SIGTERM`).
2. Remove stale WAL/SHM files and restore:
   ```bash
   rm -f data/trading_bot.db-wal data/trading_bot.db-shm
   cp backups/trading_bot_YYYYMMDD_HHMMSS.db data/trading_bot.db
   ```
3. Restart `python -m src.main`—`StartupReconciler` automatically reconciles restored DB state with Binance.
