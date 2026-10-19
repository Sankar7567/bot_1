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
│   └── test_platform.py       # Comprehensive automated test suite (31 test cases)
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
    ├── lightgbm_crypto_model_v1.txt           # Preferred v1 weights (69 features)
    ├── lightgbm_crypto_model_v1_metadata.json # v1 Feature schema
    ├── lightgbm_crypto_model_v2.txt           # v2 Symmetric long/short model weights
    └── lightgbm_crypto_model_v2_metadata.json # v2 Feature schema (111 features)
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

#### 24/7 Autonomous Daemon Mode (Recommended for Live Trading)
```bash
python cli.py execute --mode live --daemon --poll-interval 15 --monitor-interval 30 --threshold 0.35 --risk 0.05
```

The daemon runs **infinitely without crashing**, cycling seamlessly through:
`Universal Scanning -> Global Best-Trade Selection -> Order Execution -> 30-Second Active Trade Monitoring -> Closure Sweep & Cleanup -> Infinite Re-Scanning`.

Trading commands default to the existing **v1 checkpoint and feature architecture**. The signal selector compares its class 0 downside score for SHORT with class 2 upside score for LONG and takes the stronger side. v2 remains available as the separately trained symmetric directional model.

Live brackets use a strict **1:3 risk-reward ratio** after price-tick alignment. The stop distance rounds outward to the next valid tick, the target is set to exactly three times that distance, and a symbol is skipped if a positive target cannot be represented.

| Phase | Cadence | Trigger | Autonomous Action |
|---|---|---|---|
| **Phase 1: Discovery & Execution** | Every 15s | No active trade | Scans 500+ symbols, evaluates ML confidence, skips sub-minimum trades (`SKIPPED_MIN_NOTIONAL`), executes best trade with isolated margin and 1:3 RR bracket orders |
| **Phase 2: Active Trade Monitoring** | Every 30s | Active trade open | Dedicated sub-loop sleeps 30 seconds, polls ticker price AND Binance `fetch_positions()` to verify live bracket fills |
| **Phase 3: Completion, Cleanup & Reset** | Instant | Trade closed | Executes Closure Sweep (`cancel_all_orders`), marks state `CLOSED`, logs PnL to audit trail, and **immediately returns to Phase 1** to scan 24/7 |

> [!IMPORTANT]
> **Exchange-First Ground Truth (`exchange.fetch_positions()`):** The daemon treats your actual Binance Futures account as the absolute authority. If local state contains phantom trades (e.g. from tests or previous paper runs), they are automatically purged on startup and on every loop iteration. If 0 positions exist on Binance, the slate is wiped clean, orphan orders are swept, and universal scanning resumes immediately.

Stop anytime with **Ctrl+C** — the daemon intercepts the interrupt, executes a full shutdown orphan sweep across the account, shuts down gracefully, and logs the exit.

### 7. Live Bracket Order Cleanup Verification
Verifies the aggressive bracket order cleanup protocol directly on Binance Futures with a minimum-size test order or existing position reduce-only bracket:
```bash
# Verify cleanup on default or specific symbol
python cli.py test-cleanup --symbol BTC/USDT:USDT
```
Protocol executed:
1. Places a test bracket order setup on Binance.
2. Initiates an aggressive multi-pass closure sweep for regular and Binance conditional/algo orders (`stop=True`), then force-cancels remaining IDs individually.
3. Verifies both open-order endpoints return zero orders; fetch or cancellation verification errors do not count as success.
4. Emits a rich validation table and asserts 100% cleanup pass.

### 8. System Status & Disk Usage Meter
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

All events (orders, closures, skips, exceptions, fallbacks) are appended as JSON Lines to `logs/platform_audit.log`.

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

### Bulletproof Infinite Loop & Error Resilience
- The outer loop is an unbreakable `while True:` wrapped in comprehensive `try...except Exception as e:` blocks.
- If any network timeout, CCXT error, rate limit, or unexpected exception occurs:
  - It catches the error, logs full stack trace to `logs/platform_audit.log`, sleeps 15 seconds, and **continues the loop**.
  - Exceptions never crash the daemon.
- Automatic retry on network dropouts via `retry_ccxt_call` (up to 3 attempts with exponential backoff).
- Binance code `-4168` ("Unable to adjust to isolated-margin mode under the Multi-Assets mode") is handled gracefully with an informational warning, allowing trading to proceed with account default margin mode.

### Sub-Minimum Trade Skipping
When 5% equity risk sizing produces a position below Binance's `minNotional` or `minQty` for a symbol:
- **No crash or error** — the candidate is skipped.
- Logs: `[SKIPPED_MIN_NOTIONAL] Symbol XYZ position size $A below exchange minNotional $B. Evaluating next top setup.`
- The engine evaluates the next highest-ML-confidence candidate in the ranked queue until a valid trade is found.

### Two-Layer Live Exchange Reconciliation & 30-Second Cadence
In LIVE mode, `check_and_process_exit()` uses **two independent layers**:
1. **Local price arithmetic** — compares current ticker price against stored TP/SL levels.
2. **Exchange reconciliation** — calls `fetch_positions([symbol])` to detect fills that Binance executed on bracket orders even before the ticker price crosses the barrier locally.

The monitoring sub-loop checks this every 30 seconds (`--monitor-interval 30`), ensuring prompt bracket order cleanup and state reset.

### Aggressive Bracket Order Cleanup & Verification Loop
When either a Take-Profit or Stop-Loss order triggers and fills on Binance, the opposing bracket order must not linger as an orphan:
1. **Multi-Pass Loop (Up to 5 Passes):** Bulk-cancels regular orders and conditional/algo orders using CCXT's `stop=True` route.
2. **Lingering Order Verification:** Inspects both `fetch_open_orders(symbol)` and `fetch_open_orders(symbol, params={"stop": True})`.
3. **Individual Force-Cancellations:** Uses the matching regular or conditional cancel endpoint for each lingering order ID.
4. **Exchange Propagation Delay:** Pauses 0.5s between passes for Binance matching engine propagation.
5. **Zero-Orphan Invariance:** Repeats until both order endpoints return empty lists. A failed verification keeps the daemon in monitoring so it retries cleanup.

### Account-Wide Startup & Shutdown Sweeps
- **Startup Sweep:** Before commencing discovery or monitoring, queries all open orders across the account. For any symbol without an active position, it triggers `cancel_all_orders_for_symbol()` to eliminate stale orders from prior crashed sessions.
- **Shutdown Sweep:** Intercepts SIGINT/Ctrl+C or clean daemon termination, running a comprehensive account sweep to guarantee zero leftover orders when stopping.

### Binance `fetchOpenOrders` Warning Suppression
`LiveExchangeAdapter` sets `exchange.options["fetchOpenOrders"]["warnWithoutSymbol"] = False` on initialization. This suppresses the CCXT advisory emitted when calling `fetch_open_orders()` without a symbol — we call it intentionally during the global orphan sweep.

---

## 6. Automated Test Suite

```bash
python -m unittest tests/test_platform.py -v
```

The **37-test suite** covers:

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
| 19 | Error Resilience | CCXT network errors caught, logged with traceback, loop continues |
| 20 | Code -4168 Handling | Multi-Assets isolated margin error handled gracefully without raising |
| 21 | 30s Monitoring & Sweep | Dedicated 30s monitoring sub-loop, closure sweep & immediate reset |
| 22 | Exchange Ground Truth | Zero open positions on Binance purges phantom local state files |
| 23 | Exchange Position Sync | Active Binance positions detected and synchronized to state file |
| 24 | Live Exit on Position Zero | Position contracts dropping to 0 triggers exit and closure sweep |
| 25 | Bracket Cleanup Verification | Multi-pass cancellation + verification loop force-cancels orphans |
| 26 | Shutdown Orphan Sweep | Shutdown sweep cleans all orphan orders across account on exit |
| 27 | Live Orphan Test Harness | Test harness protocol confirms 100% zero leftover bracket orders |
| 28 | Versioned Models | v1 and v2 checkpoints and their feature schemas load independently |
| 29 | Trend Features | ADX, EMA slopes, MACD continuation, and trend alignment features |
| 30 | Symmetric Labels | Both long and short winner labels are present |
| 31 | v2 Rolling Training | Rolling trainer produces a v2 model checkpoint |
| 32 | Data Pagination | Five-year ingestion paginates until the exchange returns no more candles |
| 33 | Nuclear Reset | Regular and conditional open orders are cleared and verified |
| 34 | Precise 1:3 Brackets | Fill-anchored long brackets preserve 1:3 RR at exchange precision |
| 35 | Low-Price RR and Direction Mapping | Tick-aligned brackets and v1/v2 selection of the stronger short-side or long-side score |
| 36 | Conditional Order Cleanup | Binance conditional algo orders are canceled and fetch errors fail closed |
| 37 | Bidirectional Backtests | Single-symbol and global backtests open and close both LONG and SHORT positions at strict 1:3 RR |
