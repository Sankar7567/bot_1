# Binance Futures HA Trading Bot — Master Test Plan & Quality Assurance

**Document Version:** 1.0.0  
**Phase:** P01 — Master Project Brief, Architecture & Foundation  
**Status:** Approved Foundation  

---

## 1. Quality Assurance Philosophy & Testing Standards

To protect capital and guarantee algorithmic correctness, the system adheres to strict engineering rules:
1. **Zero False Claims:** Never claim a test passed without executing it and capturing stdout/stderr.
2. **Deterministic Foundations:** Unit and strategy tests must rely on fixed, mathematically hand-verified test vectors, never live non-deterministic network data.
3. **Strict Separation:** Tests hitting the real Binance Testnet must be marked with `@pytest.mark.testnet` and isolated from the default test suite.
4. **Coverage Enforced:** Minimum 80% branch coverage across core trading modules (`src/`).
5. **No Regressions:** Every phase must run all preceding test suites before acceptance.

---

## 2. Test Classification Matrix

| Tier | Directory | Scope | Network Access | Frequency |
|---|---|---|---|---|
| **Unit Tests** | `tests/unit/` | Pure math, formatting, indicators, config, validation, error mapping. | Forbidden (100% Mocked) | Every commit / PR |
| **Integration Tests** | `tests/integration/` | Database transactions, client-server handshake, mock exchange engine. | Localhost / Mocks | Every phase completion |
| **End-to-End Tests** | `tests/e2e/` | Full signal-to-exit simulated lifecycle with state persistence. | Simulated Exchange | Major milestones |
| **Testnet Tests** | `tests/integration/testnet/` | Real Binance Testnet connectivity, balance query, order placement. | Binance Testnet | Phases P02, P09, P10, P14 |

---

## 3. Phase-by-Phase Verification Roadmap

### P01 — Foundation & Configuration (Current)
- [x] Environment variable loading from `.env` and defaults.
- [x] Pydantic configuration validation (valid ranges, types).
- [x] API key / secret redaction and masking in logs.
- [x] Structured logger initialization and file emission.
- [x] Pytest and coverage execution pipeline.

### P02 — Binance Exchange Connector
- HMAC-SHA256 request signature verification against known Binance vectors.
- Timestamp synchronization and drift adjustment.
- HTTP 429 and 503 retry policies with exponential backoff.
- Error taxonomy mapping (Binance error codes to Python exceptions).
- Real Testnet connectivity ping and balance retrieval.

### P03 — Market Scanner
- 24h percentage gain descending sort.
- Deterministic tie-breaking (quote volume, symbol).
- Exclusion of non-perpetual, inactive, or low-liquidity contracts.
- Rate-limit adherence across 60-second polling cycles.

### P04 — Market Data Engine
- Chronological ordering and gap detection.
- Discarding unfinished candles (`kline.x == False`).
- WebSocket disconnect and automated reconnection.
- REST snapshot reconciliation against live candle buffer.

### P05 — Heikin-Ashi Engine
- Hand-calculated multi-candle sequence verification.
- Initial bar $HA\_Open$ calculation: $(Open_0 + Close_0)/2$.
- Bullish, bearish, and doji classification.
- Absence of look-ahead bias across sliding windows.

### P06 — Multi-Timeframe Strategy Engine
- 15m trigger generation on closed candle.
- 1st 1m confirmation matching directional bias.
- Immediate trigger discard on opposite or doji 1m candle.
- Single-shot confirmation enforcement (ignoring subsequent 1m candles).
- Trigger expiration at the 15m candle boundary.

### P07 — Swing Detection Engine
- 5-left / 5-right pivot low / high verification on actual OHLC data.
- Confirmation delay: pivot at bar $i$ confirmed exactly at bar $i+5$ close.
- Rejection of equal highs / lows.
- Buffer calculation for stop loss.

### P08 — Risk Management & Position Sizing
- Verification against standard test vector ($1000\text{ USDT}, 10\%, Entry=100, SL=98$).
- Cost adjustment: inclusion of 0.05% taker fees and 0.05% slippage.
- Strictly rounding down to `stepSize`.
- Minimum quantity and minimum notional rejection.
- Max leverage bracket lookup and isolated margin validation.
- Liquidation price safeguard ($P_{sl}$ vs $P_{liq}$).

### P09 — Order Execution Engine
- Unique `clientOrderId` generation and format check.
- Market order submission and fill tracking.
- Partial fill handling: basing protection on `executedQty`.
- API timeout reconciliation (query before retry).

### P10 — Protective Order Manager
- Post-fill SL (`STOP_MARKET`) and TP (`TAKE_PROFIT_MARKET`) placement with `reduceOnly=true`.
- Verification of both orders in `NEW` state on exchange.
- Exit event listening and atomic sibling order cancellation.
- Fail-safe emergency market exit if protection fails.

### P11 — Position Manager & State Recovery
- Global single-position lock enforcement across threads/tasks.
- SQLite state persistence and transition validation.
- Startup reconciliation: matching local database with exchange positions/orders.

### P12 — Main Orchestration & Full Integration
- Complete lifecycle simulation: Scan $\rightarrow$ Trigger $\rightarrow$ Confirm $\rightarrow$ Size $\rightarrow$ Entry $\rightarrow$ Protection $\rightarrow$ Exit $\rightarrow$ Reconciliation.
- Concurrency and graceful shutdown handling (`SIGINT`/`SIGTERM`).

### P13 — Paper Trading & Strategy Simulation
- Historical replay without exchange order submission.
- Metric calculation: Win Rate, Profit Factor, Max Drawdown, Sharpe Ratio, Expectancy.

### P14 — Binance Testnet Operational Validation
- End-to-end execution against live Binance Testnet.
- Fault injection: simulated disconnects, timeouts, cancellations.

### P15 — Independent Audit & Deployment Readiness
- Complete code audit, security review, linting, and final sign-off.

---

## 4. Test Execution Commands

```bash
# Activate virtual environment
source .venv/bin/activate

# Run all unit tests with branch coverage
pytest tests/unit/ --cov=src --cov-report=term-missing

# Run integration tests
pytest tests/integration/

# Run complete test suite (excluding live testnet)
pytest -m "not testnet"

# Run live Testnet tests (requires valid testnet credentials in .env)
pytest -m "testnet"
```
