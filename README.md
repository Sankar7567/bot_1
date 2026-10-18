# Binance Futures ML Crypto Research & Live Signal Platform

A modular, production-grade quantitative machine learning research platform and real-time signal engine targeting Binance Futures USDT-Margined perpetual contracts.

Engineered for local execution on mid-range hardware (Ryzen 5 / RTX 3050 / 16GB RAM) with **strict disk space enforcement (< 20GB limit)** using compressed Parquet storage, rolling-batch multi-symbol ingestion, and automatic raw data pruning.

Detailed architecture specifications, mathematical formulations, and data flow diagrams are available in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Architecture Overview

```
bot/
├── execution.py               # Dual-mode engine: PAPER / LIVE, 24/7 daemon, live exchange reconciliation
├── rolling_train_pipeline.py  # Rolling-batch multi-symbol ingestion with automated raw Parquet pruning
├── scanner.py                 # Global universe scanner enforcing strict 1-trade-at-a-time across 500+ symbols
├── live_signals.py            # Real-time polling via CCXT, LightGBM inference, JSON signals & audit logger
├── backtester.py              # Compounding equity from $100, dynamic ATR risk sizing (2%, 3%, 5%), and friction
├── model.py                   # Triple-Barrier labeling (1:3 RR) & Walk-Forward CV LightGBM engine
├── features.py                # Multi-timeframe trend, momentum, volume & volatility features
├── data_pipeline.py           # Historical OHLCV ingestion via CCXT, Parquet ZSTD, disk pruning (< 20GB)
├── cli.py                     # Typer CLI with full pipeline commands
├── app.py                     # Streamlit quantitative web dashboard
├── requirements.txt           # Core system dependencies
├── .env.example               # Template for Binance API credentials (copy to .env, never commit)
├── tests/
│   └── test_platform.py       # Comprehensive automated test suite (18 test cases)
├── docs/
│   └── ARCHITECTURE.md        # Complete quantitative system manual & data flow diagrams
├── signals/
│   ├── latest_signal.json     # Decoupled high-conviction JSON signals
│   ├── paper_wallet.json      # Paper trading simulator wallet state
│   └── active_trade_state.json# Global active trade state tracker (OPEN / CLOSED)
├── logs/
│   └── platform_audit.log     # Structured audit trail (JSON Lines)
├── data/
│   ├── raw/{tf}/{sym}.parquet
│   └── features/{tf}/{sym}.parquet
└── models/
    ├── lightgbm_crypto_model.txt
    └── lightgbm_crypto_model_metadata.json
```

---

## 1. Quick Start & Setup

### Environment & Credentials
```bash
# Activate virtual environment
source .venv/bin/activate

# Or install dependencies into a fresh environment
pip install -r requirements.txt

# Copy the credentials template and fill in your Binance API keys
cp .env.example .env
# Edit .env:
#   BINANCE_API_KEY=your_actual_key
#   BINANCE_API_SECRET=your_actual_secret
```

> **Security:** `.env` is in `.gitignore` and is never committed. Only `.env.example` (with placeholder values) is tracked.

If keys are missing or invalid, the engine automatically falls back to **PAPER mode** and logs:
```
[WARN] Valid live credentials not found. Falling back to PAPER mode.
```

---

## 2. CLI Usage (`cli.py`)

### 1. Rolling-Batch Training (< 20GB Disk Enforcement)
Ingests, builds features for, and trains LightGBM on batches of symbols, automatically purging raw Parquet files after each batch is absorbed:
```bash
python cli.py rolling-train --batch-size 50 --months 12

# Rapid synthetic verification:
python cli.py rolling-train --batch-size 25 --synthetic --months 3
```

### 2. Risk Sizing & Compounding Optimizer ($100 Starting Balance)
```bash
python cli.py optimize-risk --symbol BTC/USDT:USDT --risks "0.02,0.03,0.05" --capital 100.0
```

### 3. Backtesting
```bash
python cli.py backtest --symbol SOL/USDT:USDT --risk 0.03 --threshold 0.35
```

### 4. Global Multi-Symbol Universe Scanner
```bash
python cli.py global-backtest --threshold 0.35 --risk 0.03 --capital 100.0
```

### 5. Live Signal Scanner
```bash
# Single scan cycle:
python cli.py live-scan --threshold 0.35

# Continuous daemon polling:
python cli.py live-scan --daemon --poll-interval 10 --threshold 0.35
```

### 6. Dual-Mode Execution Engine

#### Single Scan Cycle (Paper or Live)
```bash
python cli.py execute --mode paper --threshold 0.35 --risk 0.05
python cli.py execute --mode live  --threshold 0.35 --risk 0.05
```

Displays a **Rich progress bar** during universe scanning and a **Candidate Leaderboard Table** ranking all evaluated setups by ML confidence, with status labels (`SELECTED_AND_EXECUTED`, `SKIPPED_MIN_NOTIONAL`, `VALID_SETUP_QUEUE`, `BELOW_THRESHOLD`).

#### 24/7 Daemon Mode (Recommended for Live Trading)
```bash
python cli.py execute --mode live --daemon --poll-interval 15 --threshold 0.35 --risk 0.05
```

The daemon runs **continuously** without exiting. It operates in two alternating phases:

| Phase | Trigger | Action |
|---|---|---|
| **SCANNING** | No active trade | Full ML universe scan across 500+ symbols; executes highest-conviction valid trade |
| **MONITORING** | Active trade detected | Polls TP/SL every `--poll-interval` seconds; checks **both** local price arithmetic and live Binance `fetch_positions()` |

Once a trade closes (TP/SL hit on price **or** confirmed gone from exchange), the daemon:
1. Cancels all remaining bracket orders (Closure Sweep)
2. Updates `signals/active_trade_state.json` → `CLOSED`
3. Logs the closure to `logs/platform_audit.log`
4. **Immediately** resumes universe scanning for the next trade

Stop with **Ctrl+C** — the daemon shuts down gracefully and logs the event.

### 7. System Status & Disk Usage Meter
```bash
python cli.py status
```

---

## 3. Decoupled JSON Signal Output

When a high-conviction trade setup ($P(\text{TP}) \ge \text{threshold}$) is detected and validated against exchange minimum sizes, a structured JSON payload is atomically written to `signals/latest_signal.json`:

```json
{
  "timestamp": "2026-10-18T07:45:00Z",
  "symbol": "SOL/USDT:USDT",
  "direction": "LONG",
  "confidence": 0.84,
  "entry_price": 142.50,
  "stop_loss": 139.20,
  "take_profit": 152.40,
  "risk_pct": 5.0,
  "dollar_risk": 5.00,
  "leverage": 20,
  "suggested_position_size": 2.15,
  "mode": "LIVE",
  "status": "SIGNAL_GENERATED"
}
```

All events (orders, closures, skips, fallbacks) are appended as JSON Lines to `logs/platform_audit.log`.

---

## 4. Web Dashboard (`app.py`)

```bash
streamlit run app.py
```
Open [http://localhost:8501](http://localhost:8501).

### Key Interactive Features:
- **Disk Guardrail Meter:** Live disk usage tracker enforcing the < 20GB limit.
- **Risk Sizing Optimizer Tab:** Side-by-side comparison of 2%, 3%, and 5% compounding equity curves from $100.
- **Global Universe Scanner Tab:** Universe-wide backtest with global equity curve and cross-symbol trade distribution.
- **Execution Tab:** Mode selection (PAPER/LIVE), real-time `st.progress` scanning bar, candidate ranking leaderboard table, active trade status.
- **Decoupled Live Signals & Audit Tab:** Signal card, active trade status, streaming audit log.
- **Single Symbol Drilldown Tab:** Feature analysis and LightGBM feature importance bar chart.

---

## 5. Key Design Decisions

### Sub-Minimum Trade Skipping
When 5% equity risk sizing produces a position below Binance's `minNotional` or `minQty` for a symbol:
- **No crash or error** — the candidate is silently skipped.
- Logs: `[SKIPPED_MIN_NOTIONAL] Symbol XYZ position size $A below exchange minNotional $B. Evaluating next top setup.`
- The engine evaluates the next highest-ML-confidence candidate in the ranked queue until a valid trade is found.

### Live Exchange Reconciliation
In LIVE mode, `check_and_process_exit()` uses **two independent layers**:
1. **Local price arithmetic** — compares current ticker price against stored TP/SL levels.
2. **Exchange reconciliation** — calls `fetch_positions([symbol])` to detect fills that Binance executed on bracket orders even before the ticker price crosses the barrier locally.

This ensures the active-trade lock is released promptly and no position stays "stuck" in the system after the exchange has already closed it.

### Binance `fetchOpenOrders` Warning Suppression
`LiveExchangeAdapter` sets `exchange.options["fetchOpenOrders"]["warnWithoutSymbol"] = False` on initialization. This suppresses the CCXT advisory emitted when calling `fetch_open_orders()` without a symbol — we call it intentionally during the global orphan sweep.

---

## 6. Automated Test Suite

```bash
python -m unittest tests/test_platform.py -v
```

All **18 tests** pass and validate:

| # | Test | What It Verifies |
|---|---|---|
| 1 | Data Pipeline | Storage tracking, ZSTD Parquet compression |
| 2 | Feature Engineering | Momentum, volume, volatility indicator formulas |
| 3 | HTF Alignment | Backward `pd.merge_asof` with zero lookahead bias |
| 4 | Triple-Barrier Labeling | 1:3 RR dynamic ATR barriers |
| 5 | LightGBM CV | Purged Walk-Forward Cross-Validation |
| 6 | Risk Optimizer | Compounding from $100 at 2%, 3%, 5% risk |
| 7 | Universe Scanner | Strict single-trade invariance across symbols |
| 8 | Rolling-Batch Training | Incremental LightGBM updates + raw Parquet purging |
| 9 | Live Signals | JSON signal structure + audit logging |
| 10 | CLI Commands | Typer CLI commands execute successfully |
| 11 | Paper Execution | PaperWallet lifecycle + orphan cleanup |
| 12 | Risk Sizing | Exact 5% risk against account equity |
| 13 | Feature Importance | Booster interface without `NotFittedError` |
| 14 | Credential Fallback | Graceful PAPER fallback on missing/dummy keys |
| 15 | Sub-Minimum Skipping | `SKIPPED_MIN_NOTIONAL` + next candidate selected |
| 16 | ML Probability Ranking | Strict descending confidence sort + progress callback |
| 17 | Daemon Loop | SCANNING/MONITORING phase transitions + closure resume |
| 18 | Warning Suppression | `fetchOpenOrders.warnWithoutSymbol = False` set on adapter |
