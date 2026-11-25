# Maintenance Guide

---

## 1. Updating Application Code
1. Stop the running bot gracefully (`Ctrl+C` / `SIGTERM`).
2. Back up `data/trading_bot.db` ([Backup & Recovery](BACKUP_AND_RECOVERY.md)).
3. Pull updates and refresh dependencies:
   ```bash
   git pull origin main
   source .venv/bin/activate
   pip install -r requirements-dev.txt
   ```
4. Validate config & run tests before restarting:
   ```bash
   python -m src.main --check-config
   pytest --cov=src --cov-fail-under=80 -q
   ```
5. Restart: `python -m src.main`.

## 2. Database Migrations
`DatabaseManager.initialize()` in `src/state/database.py` uses idempotent `CREATE TABLE IF NOT EXISTS` statements. No separate migration tool is required.

## 3. Git & Documentation Practices
- Never commit `.env`, `data/*.db`, or `logs/*.log`.
- Update `docs/CHANGELOG.md`, `docs/DEVELOPMENT_LOG.md`, and affected manuals whenever behavior, settings, or CLI flags change.
