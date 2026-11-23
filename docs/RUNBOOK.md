# Binance Futures HA Trading Bot — Operator & Developer Runbook

**Document Version:** 1.0.0  
**Phase:** P01 — Master Project Brief, Architecture & Foundation  
**Status:** Approved Foundation  

---

## 1. System Requirements & Prerequisites

- **Operating System:** Linux (Ubuntu 22.04+ / Debian 12+) or macOS.
- **Python Runtime:** Python 3.11 or higher (Tested on Python 3.14.4).
- **Network Access:** Unrestricted HTTPS and WebSocket access to `*.binancefuture.com`.
- **Binance Account:** Binance USDⓈ-M Futures Testnet account (accessible at [testnet.binancefuture.com](https://testnet.binancefuture.com)).

---

## 2. Initial Setup & Installation

### 2.1 Clone and Environment Setup
```bash
# Navigate to repository root
cd /home/sankar/bot_ha

# Create dedicated virtual environment
python3 -m venv .venv

# Activate virtual environment
source .venv/bin/activate

# Upgrade pip and install all development dependencies
pip install --upgrade pip
pip install -r requirements-dev.txt
```

### 2.2 Configuration Management
```bash
# Copy template environment file
cp .env.example .env

# Edit .env with your Binance Futures Testnet credentials
# DO NOT COMMIT .env TO GIT!
```

---

## 3. Running Verification & Tests

### 3.1 Standard Test Suite (Unit & Mocked Integration)
```bash
# Run all unit tests with full branch coverage report
pytest tests/unit/ --cov=src --cov-report=term-missing
```

### 3.2 Running Code Quality Checks
```bash
# Verify coverage threshold (fails under 80%)
pytest --cov=src --cov-fail-under=80
```

---

## 4. Emergency Procedures (Manual Exchange Audit)

If the automated system is interrupted or an unexpected state occurs during operation:

1. **Stop the Bot Process:**
   - Issue `SIGINT` (Ctrl+C) or `SIGTERM`.
2. **Access Binance Futures Testnet UI:**
   - Log in at `https://testnet.binancefuture.com`.
   - Inspect the **Positions** tab: Check for any open positions.
   - Inspect the **Open Orders** tab: Check for any pending `STOP_MARKET` or `TAKE_PROFIT_MARKET` orders.
3. **Emergency Manual Close:**
   - If a position exists without active protective orders, close the position immediately via the "Market Close" button in the exchange web UI.
   - Cancel any orphaned conditional orders.
4. **Inspect Local Database & Logs:**
   - Check `logs/trading_bot.log` for the last execution events and error stack traces.
   - Query `data/trading_bot.db` to inspect the recorded position state.
