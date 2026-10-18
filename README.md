# Binance Futures ML Crypto Research & Live Signal Platform

A modular, production-grade quantitative machine learning research platform and real-time signal engine targeting Binance Futures USDT-Margined perpetual contracts. 

Engineered for local execution on mid-range hardware (Ryzen 5 / RTX 3050 / 16GB RAM) with **strict disk space enforcement (< 20GB limit)** using compressed Parquet storage, rolling-batch multi-symbol ingestion, and automatic raw data pruning.

Detailed architecture specifications, mathematical formulations, and data flow diagrams are available in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Architecture Overview

```
bot/
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
├── tests/
│   └── test_platform.py       # Comprehensive automated test suite (10/10 test cases)
├── docs/
│   └── ARCHITECTURE.md        # Complete quantitative system manual & data flow diagrams
├── signals/
│   ├── latest_signal.json     # Decoupled high-conviction JSON signals
│   └── active_trade_state.json# Global active trade state tracker
├── logs/
│   └── platform_audit.log     # Structured audit trail
├── data/
│   ├── raw/{tf}/{sym}.parquet
│   └── features/{tf}/{sym}.parquet
└── models/
    ├── lightgbm_crypto_model.txt
    └── lightgbm_crypto_model_metadata.json
```

---

## 1. Quick Start & Setup

### Environment Activation
```bash
# Activate existing virtual environment
source .venv/bin/activate

# Or install dependencies into a fresh environment
pip install -r requirements.txt
```

---

## 2. CLI Usage (`cli.py`)

### 1. Rolling-Batch Training (< 20GB Disk Enforcement)
Ingests, builds features for, and trains LightGBM on batches of symbols (e.g. 50 symbols per batch), automatically purging raw Parquet files after each batch is absorbed:
```bash
# Live Binance Futures rolling batch training:
python cli.py rolling-train --batch-size 50 --months 12

# Or rapid synthetic verification:
python cli.py rolling-train --batch-size 25 --synthetic --months 3
```

### 2. Risk Sizing & Compounding Optimizer ($100 Starting Balance)
Tests and compares 2%, 3%, and 5% account risk compounding from a $100 starting balance:
```bash
python cli.py optimize-risk --symbol BTC/USDT:USDT --risks "0.02,0.03,0.05" --capital 100.0
```

### 3. Flexible Backtesting for Any Custom Symbol & Risk
Instantly runs backtest on any symbol (auto-ingests historical data if not cached) with compounding equity:
```bash
python cli.py backtest --symbol SOL/USDT:USDT --risk 0.03 --threshold 0.35
```

### 4. Global Multi-Symbol Universe Scanner
Simulates scanning all 500+ symbols simultaneously across time, executing strictly **ONE trade at a time globally** on the highest-conviction setup:
```bash
python cli.py global-backtest --threshold 0.35 --risk 0.03 --capital 100.0
```

### 5. Decoupled Live Signal Generation & Audit Logging
Polls real-time Binance Futures candles via CCXT, runs model inference, emits structured JSON signals, and logs to audit trail:
```bash
# Single scan:
python cli.py live-scan --threshold 0.35

# Continuous daemon polling:
python cli.py live-scan --daemon --poll-interval 10 --threshold 0.35
```

### 6. System Status & Disk Usage Meter
```bash
python cli.py status
```

---

## 3. Decoupled JSON Signal Output

When a high-conviction trade setup ($P(\text{TP}) \ge \text{threshold}$) is detected and no global trade is currently open, a structured JSON payload is saved atomically to `signals/latest_signal.json`:

```json
{
  "timestamp": "2026-10-18T07:45:00Z",
  "symbol": "SOL/USDT:USDT",
  "direction": "LONG",
  "confidence": 0.84,
  "entry_price": 142.50,
  "stop_loss": 139.20,
  "take_profit": 152.40,
  "risk_pct": 3.0,
  "suggested_position_size": 2.15,
  "status": "SIGNAL_GENERATED"
}
```

All actions, trade closures, and errors are appended to `logs/platform_audit.log`.

---

## 4. Web Dashboard (`app.py`)

Launch the Streamlit research workstation:
```bash
streamlit run app.py
```
Open [http://localhost:8501](http://localhost:8501) in your browser.

### Key Interactive Features:
- **Disk Guardrail Meter:** Live disk usage tracker ensuring compliance with the < 20GB limit.
- **Risk Sizing Optimizer Tab:** Side-by-side comparison of 2%, 3%, and 5% account risk compounding from $100, comparative logarithmic equity curves, and underwater drawdown profiles.
- **Global Universe Scanner Tab:** Universe-wide backtest enforcing global single-trade invariance with cross-symbol trade distribution.
- **Decoupled Live Signals & Audit Tab:** Formatted signal card display, global active trade status, and streaming audit log viewer.
- **Single Symbol Drilldown Tab:** Individual pair backtesting and LightGBM feature importance rankings.

---

## 5. Automated Test Suite

Run the full test suite verifying all invariants:
```bash
python -m unittest tests/test_platform.py
```
All 10 test suites validate:
1. Data pipeline storage tracking and ZSTD Parquet compression
2. Momentum, volume, and volatility indicator formulas
3. Backward `pd.merge_asof` multi-timeframe alignment (zero lookahead bias)
4. Dynamic ATR Triple-Barrier 1:3 Risk-Reward labeling
5. LightGBM Purged Walk-Forward Cross-Validation
6. Compounding equity from $100 and risk optimization (2%, 3%, 5%)
7. Global universe scanner with strict single-trade invariance across symbols
8. Rolling-batch training with automatic raw Parquet purging
9. Decoupled live JSON signal emission and audit logging
10. Typer CLI commands
