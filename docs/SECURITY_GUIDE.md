# Security Guide

## 1. Secret Management
- **Pydantic `SecretStr`:** `binance_api_key` and `binance_api_secret` in [src/config/settings.py](../src/config/settings.py) are typed as `SecretStr`. Calling `Settings.get_safe_dict()` (used by `--check-config`) replaces non-empty secrets with `***REDACTED***`.
- **Log Redaction:** `RedactingFormatter` in [src/monitoring/logger.py](../src/monitoring/logger.py) strips `api_key`, `secret`, `signature=`, and `X-MBX-APIKEY` patterns from both console and rotating file logs.
- **No Frontend Secrets:** There is no frontend/browser UI in this repository (`NOT IMPLEMENTED`), so no secrets are ever exposed to client-side code.
- **Git Exclusion:** `.gitignore` excludes `.env`, `.env.*`, `data/`, `*.db`, and `logs/`. Never force-add (`git add -f`) `.env`.

## 2. File Permissions on Linux
Restrict `.env` and `data/trading_bot.db` to the bot's OS user:
```bash
chmod 600 .env
chmod 700 data logs
```

## 3. API Key Least Privilege
- Enable **Read** + **Futures** only.
- **Never** enable Withdrawals, Margin, or Universal Transfers.
- Always enable **IP Whitelisting** for production keys.
- Use **Testnet** (`APP_ENV=testnet`) for all development and validation.

## 4. Database & Backup Security
Do not include `.env` in shared support bundles or backups unless encrypted (see [Backup & Recovery](BACKUP_AND_RECOVERY.md)).
