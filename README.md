# Binance Futures Heikin-Ashi Trading Bot

A robust, mathematically verified, automated algorithmic trading system for Binance USDⓈ-M Futures (Testnet first), developed in Python 3.14+ with asynchronous execution.

---

## Key Strategy & Architecture Characteristics

1. **Market Selection:** Continuously scans USDⓈ-M USDT perpetuals, ranking by 24h percentage price change to identify the top 20 gainers, refreshed every 60 seconds with liquidity filtering.
2. **Multi-Timeframe Heikin-Ashi:** Uses completed 15m Heikin-Ashi candles for directional trend bias (Bullish/Bearish) and confirms via the first subsequent completed 1m Heikin-Ashi candle.
3. **Confirmed Swing Stop-Loss:** Identifies market-structure pivots using actual market OHLC data (5 bars left, 5 bars right with strictly higher lows / lower highs) with zero look-ahead bias.
4. **Asymmetric Risk-to-Reward:** Strictly enforces a 3:1 gross reward-to-risk ratio ($3R$) on all trades.
5. **Rigorous Capital Protection:** Risks a nominal 10% of current Futures account equity per trade, adjusted for taker fees and slippage, with truncation to exchange step sizes and bracket leverage constraints.
6. **Isolated Margin & One Position:** Operates strictly on isolated margin with maximum eligible bracket leverage and permits exactly **one (1)** bot-managed position globally.
7. **Exchange-Side Protective Orders:** Submits native exchange-side Stop-Loss and Take-Profit orders (`reduceOnly=true`) with automated sibling order cancellation upon fill.
8. **Crash Resilience:** Employs persistent SQLite state management with startup reconciliation against Binance exchange state.

---

## Project Structure

```
├── docs/               # Architecture, Specifications, Risk Models, and Runbooks
│   ├── PROJECT_SPEC.md
│   ├── ARCHITECTURE.md
│   ├── STRATEGY_RULES.md
│   ├── RISK_MODEL.md
│   ├── API_CONTRACTS.md
│   ├── TEST_PLAN.md
│   ├── DEVELOPMENT_LOG.md
│   ├── CHANGELOG.md
│   └── RUNBOOK.md
├── src/                # Modular Source Code
│   ├── config/         # Configuration and Environment Settings
│   ├── exchange/       # Binance REST & WebSocket Connectors
│   ├── scanner/        # Top 20 24h Gainers Market Scanner
│   ├── market_data/    # Kline Feed, Order Books, Ring Buffers
│   ├── indicators/     # Pure Heikin-Ashi & 5L/5R Swing Calculations
│   ├── strategy/       # 15m Trigger & 1m Confirmation State Machine
│   ├── risk/           # 10% Equity Sizing, Leverage Brackets, Step Size
│   ├── execution/      # Order Submission, Client Order IDs, Fills, User Stream
│   ├── positions/      # Position Manager & Protective SL/TP Lifecycle
│   ├── state/          # Persistent SQLite Store & Crash Recovery
│   ├── backtesting/    # Chronological Non-Lookahead Replay Engine (Amendment 01)
│   ├── monitoring/     # Structured Logging & Secret Masking
│   ├── orchestrator.py # Event-Driven TradingBotOrchestrator
│   └── main.py         # CLI Entrypoint (--check-config, SIGINT/SIGTERM)
├── tests/              # Test Suites
│   ├── unit/           # Unit tests (Mocked, Pure Logic, Chronological Backtest)
│   └── integration/    # Component, E2E Lifecycle, Fault Injection, & Testnet Tests
├── pyproject.toml      # Project configuration & test settings
├── requirements.txt    # Production runtime dependencies
└── requirements-dev.txt# Development & testing dependencies
```

---

## Development Roadmap (15 Phases — All Complete)

- **P01 — Master Project Brief, Architecture & Foundation** [DONE]
- **P02 — Binance Exchange Connector** [DONE]
- **P03 — Market Scanner** [DONE]
- **P04 — Market Data Engine** [DONE]
- **P05 — Heikin-Ashi Engine** [DONE]
- **P06 — Multi-Timeframe Strategy Engine** [DONE]
- **P07 — Swing Detection Engine** [DONE]
- **P08 — Risk Management and Position Sizing** [DONE]
- **P09 — Binance Order Execution Engine** [DONE]
- **P10 — Protective Order Manager** [DONE]
- **P11 — Position Manager and State Recovery** [DONE]
- **P12 — Main Orchestration and Full Integration** [DONE]
- **P13 — Chronological Backtesting & Live Replay (Amendment 01)** [DONE]
- **P14 — Binance Testnet Validation & Fault Injection** [DONE]
- **P15 — Independent Audit and Deployment Readiness** [DONE]

---

## Getting Started

```bash
# Set up virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements-dev.txt

# Validate configuration (defaults to Binance Testnet)
python -m src.main --check-config

# Run full offline test suite with branch coverage (>= 80%)
pytest --cov=src --cov-fail-under=80

# Run live Binance Testnet tests (requires BINANCE_API_KEY & BINANCE_API_SECRET)
pytest -m testnet
```
