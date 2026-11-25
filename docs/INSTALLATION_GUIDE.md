# Installation Guide

This guide covers installing `binance-ha-bot` on a clean **Ubuntu/Debian Linux** machine.

---

## 1. Prerequisites

| Requirement | Minimum / Tested | Notes |
|---|---|---|
| **Operating System** | Ubuntu 22.04+ / Debian 12+ | Linux x86_64/arm64 |
| **Python** | Python **3.11+** (tested on **3.14.4**) | Required by `pyproject.toml` (`requires-python >= 3.11`) |
| **Node.js / npm** | **Not required** | No frontend/Node.js UI exists (`NOT IMPLEMENTED`) |
| **Git** | Git 2.30+ | For cloning and updates |
| **Database** | Built-in Python `sqlite3` (WAL mode) | No external DB server required |
| **Storage / RAM** | 1 GB+ free disk, 512 MB+ RAM | Logs rotate at 10 MB × 5 backups |
| **Network** | Outbound HTTPS (443) & WSS (443) | To `*.binancefuture.com` / `stream.binancefuture.com` |

---

## 2. Step-by-Step Installation

### Step 1: Install System Packages
```bash
sudo apt-get update && sudo apt-get install -y git python3 python3-venv python3-pip sqlite3
```
Verify Python version (must be 3.11 or higher):
```bash
python3 --version
```

### Step 2: Clone the Repository
```bash
git clone https://github.com/Sankar7567/bot_1.git bot_ha
cd bot_ha
```

### Step 3: Create and Activate Python Virtual Environment
```bash
python3 -m venv .venv
source .venv/bin/activate
```
Your prompt should now show `(.venv)`.

### Step 4: Upgrade `pip` and Install Dependencies
For **production/runtime only** (no test packages):
```bash
pip install --upgrade pip
pip install -r requirements.txt
```
For **development and testing** (includes `pytest`, `pytest-asyncio`, `pytest-cov`):
```bash
pip install --upgrade pip
pip install -r requirements-dev.txt
```

> [!NOTE]
> `requirements.txt` installs `httpx>=0.28.1`, `websockets>=13.0`, `pydantic>=2.10.0`, `pydantic-settings>=2.7.0`, and `python-dotenv>=1.0.1`.

### Step 5: Create Environment Configuration
Copy the template file to `.env`:
```bash
cp .env.example .env
```
Edit `.env` to set your Binance Futures **Testnet** API credentials (see [API Key Setup](API_KEY_SETUP.md) and [Configuration Guide](CONFIGURATION_GUIDE.md)).

### Step 6: Database Initialization & Migrations
There is **no separate database migration CLI** (such as Alembic). On startup, `DatabaseManager.initialize()` in [src/state/database.py](../src/state/database.py) automatically:
1. Creates the `data/` parent directory if missing.
2. Opens `data/trading_bot.db` with `PRAGMA journal_mode = WAL;`, `PRAGMA synchronous = NORMAL;`, `PRAGMA busy_timeout = 5000;`, and `PRAGMA foreign_keys = ON;`.
3. Executes `CREATE TABLE IF NOT EXISTS` for `positions`, `orders`, `strategy_windows`, and `audit_log`.

### Step 7: Run Initial Configuration Check
```bash
python -m src.main --check-config
```
Expected output:
```text
Configuration OK (env=testnet): {'app_env': 'testnet', 'binance_api_key': '***REDACTED***', 'binance_api_secret': '***REDACTED***', ...}
```
(If keys are empty, they print as `''`.)

### Step 8: Verify Installation with Offline Tests (if dev dependencies installed)
```bash
pytest --cov=src --cov-fail-under=80 -q
```
Expected result: all offline unit/integration tests pass with >= 80% branch coverage; `@pytest.mark.testnet` tests skip cleanly when credentials are absent.

### Step 9: Start the Application
```bash
python -m src.main
```
Stop gracefully anytime with `Ctrl+C` (`SIGINT`) or `SIGTERM`. See [Quick Start Guide](QUICK_START.md) for your first Testnet session.
