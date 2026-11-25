# Binance Futures HA Trading Bot — Master Project Specification

**Document Version:** 1.0.0  
**Phase:** P01 — Master Project Brief, Architecture & Foundation  
**Status:** Approved Foundation  
**Author:** Lead Software Architect & Senior Python Engineer  
**Target Platform:** Binance USDⓈ-M Futures (Testnet First)  

---

## 1. Executive Summary & Objective

The objective of this project is to develop a production-grade, mathematically verified, fully automated algorithmic trading system for Binance USDⓈ-M Futures. The bot systematically captures directional momentum in high-volatility cryptocurrency markets while strictly constraining downside risk through deterministic stop-loss calculations, rigorous exchange-side order protection, and conservative position sizing.

The system operates under a strict **Testnet-First** rule: no real-money trading is permitted during development, and all phases must be independently validated before proceeding.

---

## 2. Core Functional Requirements

### 2.1 Market Universe & Scanner
1. **Target Market:** Binance USDⓈ-M Futures (`/fapi/v1/`).
2. **Contract Type:** USDT Perpetual contracts (`contractType == "PERPETUAL"`, `quoteAsset == "USDT"`).
3. **Status Filter:** Only contracts with `status == "TRADING"`.
4. **Ranking Metric:** 24-hour percentage price change (`priceChangePercent`), sorted in descending order.
5. **Universe Size:** Top 20 eligible contracts.
6. **Tie-Breaking Rule:** Deterministic secondary sorting by 24-hour quote volume (`quoteVolume` descending), followed by alphabetical symbol name.
7. **Liquidity Filters:** Configurable minimum 24-hour quote volume (default: 1,000,000 USDT).
8. **Refresh Frequency:** Universe is rescanned and updated every 60 seconds.
9. **Universe Integrity:** Stale or incomplete scan data must never produce an invalid universe; previous valid universe is retained with an alert, or scanning halts safely if data is corrupted.

### 2.2 Heikin-Ashi Multi-Timeframe Strategy (Amendment v1.1)
1. **Timeframes:**
   - **Trigger Timeframe:** 15-minute (15m).
   - **Confirmation Timeframe:** 1-minute (1m).
2. **Candle Completion Semantics:** Only fully closed candles (`is_closed == True`) are processed. Real-time incomplete candles are strictly prohibited from generating signals.
3. **Directional Definitions:**
   - **Bullish:** $HA\_Close > HA\_Open$.
   - **Bearish:** $HA\_Close < HA\_Open$.
   - **Doji / Neutral:** $HA\_Close == HA\_Open$ (within floating point epsilon $10^{-8}$).
4. **15m Trigger Window Creation:**
   - At every newly closed 15m candle (e.g. 5:00 PM, 5:15 PM UTC):
     - Take a snapshot of the current top 20 eligible USDT perpetual gainers.
     - Evaluate the newly closed 15m HA candle for all 20 symbols.
     - Register every symbol with a bullish (LONG) or bearish (SHORT) directional trigger as an active candidate in one shared confirmation window.
     - The candidate universe is frozen for that 15m window even if the scanner refreshes its top-20 ranking.
     - Only one active confirmation window exists globally at any time.
5. **Multi-Candle Concurrent 1m Confirmation:**
   - All registered candidates are monitored concurrently throughout the entire 15m window (e.g. 5:00 PM to 5:15 PM UTC).
   - At each newly closed 1m HA candle for a candidate:
     - If the 1m direction agrees with its 15m bias (Bullish for Long, Bearish for Short), the candidate **qualifies**.
     - If the 1m direction disagrees or is a doji, the candidate remains active and continues to be monitored on subsequent 1m closed candles.
     - A candidate can qualify only once per 15m window.
6. **Global First-Trigger-Wins Arbitration:**
   - The system maintains a global entry arbitration lock.
   - The first candidate to produce a valid 1m directional confirmation wins the global entry slot.
   - Once a winner claims the slot, all other candidate watchers are cancelled immediately.
   - **Simultaneous Confirmations:** If multiple candidates qualify on the exact same 1m candle close timestamp, the winner is determined by their original ranking from the top-20 snapshot taken at the start of the window (with deterministic alphabetical symbol ordering as a tie-breaker).
7. **Risk Validation Handoff:**
   - The winning candidate is passed to the risk management engine for validation (swing stop, stop distance, 3R target, quantity, equity, isolated margin, leverage brackets, exchange filters).
   - **Strict Window Consumption:** If the winning candidate fails risk validation, the trade is rejected, and **no secondary candidate from the same window may be selected**. Exactly one trade attempt per confirmation window.
8. **Window Expiration:**
   - At the close of the next 15m candle, all remaining unconfirmed candidate watchers are cancelled and expired.
   - A new top-20 snapshot is taken, new 15m HA triggers are evaluated, and a fresh confirmation window begins. No signals leak across window boundaries.

### 2.3 Swing Detection Engine
1. **Source Data:** Actual exchange OHLC market prices (never synthetic Heikin-Ashi values).
2. **Pivot Definition (5-Left / 5-Right):**
   - **Confirmed Swing Low:** A candle at index $i$ whose `Low` is strictly lower than the `Low` of the 5 completed candles immediately preceding it ($i-5 \dots i-1$) AND the 5 completed candles immediately following it ($i+1 \dots i+5$).
   - **Confirmed Swing High:** A candle at index $i$ whose `High` is strictly higher than the `High` of the 5 completed candles immediately preceding it ($i-5 \dots i-1$) AND the 5 completed candles immediately following it ($i+1 \dots i+5$).
3. **No Look-Ahead Bias:** A pivot at index $i$ is confirmed and eligible for use only after candle $i+5$ has officially closed.
4. **Stop-Loss Assignment:**
   - **LONG Trades:** Stop-Loss is placed below the latest eligible confirmed 1m swing low, minus a configurable buffer ($SL = SwingLow \times (1 - \text{buffer})$).
   - **SHORT Trades:** Stop-Loss is placed above the latest eligible confirmed 1m swing high, plus a configurable buffer ($SL = SwingHigh \times (1 + \text{buffer})$).
   - If no valid confirmed pivot exists within a configurable historical lookback window (default: 100 bars), the trade is **rejected**.

### 2.4 Risk Budget & Position Sizing
1. **Nominal Risk Budget:**
   $$\text{Risk Budget} = \text{Current Account Equity (USDT)} \times 0.10$$
   *Clarification: 10% is the nominal risk target based on stop distance; realized loss may vary due to slippage, fees, and market gaps.*
2. **Reward-to-Risk Target:**
   - Fixed 3:1 Reward-to-Risk ratio ($3R$).
   - **LONG:** $TP = Entry + 3 \times (Entry - SL)$.
   - **SHORT:** $TP = Entry - 3 \times (SL - Entry)$.
3. **Sizing Mechanics:**
   - Base Quantity: $Q_{base} = \frac{\text{Risk Budget}}{|\text{Entry} - \text{SL}|}$.
   - Adjusted Quantity: Incorporates estimated taker fees for entry and exit, plus estimated slippage buffer.
   - Exchange Step Size: Quantity is rounded strictly **DOWN** to the symbol's `stepSize` precision (`LOT_SIZE` filter).
   - Minimum Constraints: Must meet or exceed `minQty` and `minNotional`. If rounding down causes quantity to violate minimums, or if satisfying minimums requires risking $> 10\%$ equity, the trade is **rejected**. Never increase quantity beyond the risk budget.

### 2.5 Leverage & Margin Management
1. **Margin Type:** Strictly `ISOLATED` margin.
2. **Position Mode:** Strictly `ONE_WAY` mode (no hedge mode ambiguity).
3. **Leverage Selection:** Automatically retrieves Binance symbol leverage brackets (`/fapi/v1/leverageBracket`) and sets the maximum eligible leverage permitted for the intended position notional.
4. **Margin Verification:** Validates that available isolated wallet balance is sufficient to maintain initial margin plus fee reserves before submitting orders.

### 2.6 Global Position Limit & Concurrency
1. **Single Global Position:** Exactly **one (1)** bot-managed position is permitted across the entire exchange account at any given time.
2. **Account-Level Mutex:** An atomic persistent state lock prevents concurrent entries, re-entries during active positions, or trades while unresolved orders exist.

### 2.7 Exchange-Side Protective Orders (SL / TP)
1. **Post-Fill Recalculation:** Upon market entry fill, the system retrieves actual executed quantity and volume-weighted average fill price, recalculating exact SL and TP price levels.
2. **Order Placement:**
   - Stop-Loss: `STOP_MARKET` order with `reduceOnly=true`, triggered by `MARK_PRICE` (or `CONTRACT_PRICE`).
   - Take-Profit: `TAKE_PROFIT_MARKET` (or limit `TAKE_PROFIT`) with `reduceOnly=true`.
3. **Active Verification:** Both protective orders must be verified in `NEW` state on the exchange.
4. **Sibling Cancellation (Simulated OCO):**
   - When SL executes: immediately cancel TP; verify TP cancellation; verify position quantity is 0.
   - When TP executes: immediately cancel SL; verify SL cancellation; verify position quantity is 0.
5. **Emergency Fail-Safe:** If protective orders cannot be placed or verified within a timeout window, emergency position closure is executed at market price, followed by exchange state audit.

### 2.8 State Persistence & Crash Recovery
1. **Persistent Database:** SQLite engine storing state transitions, open orders, active position parameters, and trade histories.
2. **Startup Reconciliation:** On system launch/restart, the bot:
   - Queries exchange positions (`/fapi/v2/positionRisk`).
   - Queries open exchange orders (`/fapi/v1/openOrders`).
   - Reconciles exchange state against local database.
   - Resolves discrepancies (e.g. orphaned protective orders) before resuming scanning.

---

## 6.1 Historically Accurate, Chronological Backtesting and Live-Trading Replay (Amendment 01)

**Priority: CRITICAL** — Extends Section 6 (Cross-System Integration Requirements), Workflow A — Historical Backtesting.

The backtesting engine must behave as a **strict chronological simulation of the live trading system**, reproducing the exact decision-making process, information boundaries, timing, and execution behavior at every historical UTC timestamp — never a retrospective signal finder.

### 6.1.1 Core Backtest Invariants & Parity
1. **Engine Parity (Adapter Pattern):** The simulator MUST reuse the exact shared components (`MarketScanner`, `is_eligible_contract`, `is_eligible_ticker`, `deterministic_ranking_sort_key`, `HeikinAshiSeries`, `MultiSymbolStrategyEngine`, `SwingDetector`, `validate_and_size_position`, `OrderRouter`, `ProtectiveOrderManager`, `PositionManager`). Different behavior between live and backtest is strictly isolated inside a `HistoricalExchangeAdapter` / data adapter and simulated clock.
2. **Historical Market Reconstruction:** Reconstructs contract listing/delisting status (avoiding survivorship bias), active flags, market prices, rolling 24h percentage change, rolling 24h volume, liquidity eligibility, 1m/15m candle availability, and data quality at every timestamp.
3. **Rolling 24-Hour Top-Gainer Ranking:**
   - Must calculate rolling 24h change and rolling 24h quote volume using only data strictly closed/available up to timestamp $T$ (consistent with Binance `/fapi/v1/ticker/24hr`), never silently substituting UTC daily candle gain:
     $$\Delta\%_{24\text{h}}(T) = \frac{\text{Close}(T) - \text{Open}(T - 24\text{h})}{\text{Open}(T - 24\text{h})} \times 100, \qquad V_{24\text{h}}(T) = \sum_{k \in (T - 24\text{h}, T]} \text{QuoteVolume}_k$$
   - Periods with insufficient 24h lookback history must be marked incomplete rather than fabricating rankings.
4. **Scanner & Snapshot Timing:**
   - Refreshes market ranking every **60 seconds** in simulated UTC time.
   - At each completed 15-minute boundary, freezes the latest valid top-20 snapshot for that window; mid-window top-20 entrants are ignored until the next 15m boundary.
5. **Strict Chronological Forward-Walking & Warm-up:**
   - Deterministic UTC event loop: Historical data arrival $\rightarrow$ Validation $\rightarrow$ Scanner refresh $\rightarrow$ Ranking $\rightarrow$ 15m snapshot $\rightarrow$ HA calculation $\rightarrow$ Candidate creation $\rightarrow$ 1m monitoring $\rightarrow$ Signal coordination $\rightarrow$ Swing validation $\rightarrow$ Risk validation $\rightarrow$ Entry simulation $\rightarrow$ Position management $\rightarrow$ SL/TP execution $\rightarrow$ Position closure $\rightarrow$ Equity update.
   - Warm-up history preceding the requested interval initializes 24h rolling tickers, 1m/15m HA state, and 5L/5R swing pivots; trades are counted only inside the requested interval.
   - Zero look-ahead bias: completed candles (`close_time <= current_time_ms`) and confirmed swings (bar $i$ available only after bar $i+5$ closes) only.
6. **Realistic Trade Execution, SL/TP & Financial Accounting:**
   - Distinct tracking of signal price, order submission time, simulated fill time (next executable price after confirmation close/latency), simulated fill price (with conservative spread/slippage/fees), and post-fill SL/TP recalculation.
   - SL/TP checked strictly against actual market OHLC prices (never HA prices). Same-bar SL & TP touch resolves via higher-resolution data if present, else a documented conservative rule (SL triggered first), with resolution recorded in audit logs.
   - Full chronological equity accounting: dynamic 10% risk on current simulated equity, entry/exit fees, funding payments, spread, slippage, isolated margin, leverage brackets, liquidation checks, and global 1-position enforcement.
7. **Data Quality, Metrics, Reproducibility & Audit Trail:**
   - Detects and reports missing/duplicate/out-of-order candles, invalid OHLC, missing volume, stale intervals, and listing boundaries without silent price interpolation.
   - Reports complete metrics across Market Selection, Strategy, Trading Performance, Risk, Costs, and Equity, plus full per-trade auditable event history and configuration/data version metadata.
   - Must pass all 17 mandatory chronological validation tests (Amendment 01 §16).

---

## 3. Ambiguities, Edge Cases & Technical Risks

| # | Risk / Ambiguity | Potential Impact | Mitigation / Architectural Decision |
|---|---|---|---|
| 1 | **Non-Atomic Conditional Orders (Lack of Native Futures OCO)** | If SL and TP are placed as two independent `reduceOnly` orders, a violent spike could trigger both before one can be cancelled, or network latency during sibling cancellation could leave an open order. | 1. Use strict `reduceOnly=true` flags so protective orders cannot open reverse positions.<br>2. Listen to real-time User Data Stream WebSocket for execution reports.<br>3. When one triggers, immediately issue cancel request for the sibling and poll until `CANCELED` status is verified.<br>4. Re-check net position quantity to confirm zero exposure. |
| 2 | **Partial Fills on Market Entry** | Sizing SL/TP for requested quantity when only partial fill occurred will cause invalid protective quantities. | Sizing of protective orders occurs strictly against `executedQty` reported in the fill event, never `origQty`. |
| 3 | **Liquidation Price vs Stop Loss** | With maximum leverage, liquidation price might be closer than the confirmed swing stop-loss price. | In the Risk Engine, calculate liquidation price using Binance maintenance margin brackets. If $SL$ is beyond liquidation price ($SL_{long} \le LiqPrice$ or $SL_{short} \ge LiqPrice$), reject the trade immediately. |
| 4 | **Sub-second 1m Candle Timing & Synchronization** | System clock drift causing delayed reception of 1m candle close event. | Use Binance server time synchronization (`/fapi/v1/time`), track candle close timestamps (`kline.x == True`), and maintain an event-driven queue for closed candles. |
| 5 | **Rate Limit Exhaustion (IP & UID)** | Repeated scanning of 20+ symbols and ticker queries could trigger HTTP 429 / 418 IP ban. | Use WebSocket aggregated streams (e.g., `!ticker@arr`) for scanner updates and multi-kline streams for candles. Enforce client-side rate limit token buckets. |
| 6 | **Process Crash During Trade Lifecycle** | Bot crashes between market entry and protective order submission. | On startup, state recovery detects an unhedged position and immediately attempts to either attach calculated SL/TP or execute an emergency market exit. |

---

## 4. Phase 01 Compliance Checklist

- [x] Full requirements restated and categorized.
- [x] Edge cases and technical risks identified with concrete mitigations.
- [x] Clear boundaries established: No trading logic or live orders in P01.
- [x] Foundation ready for architectural definition.
