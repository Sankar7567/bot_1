# Binance Futures HA Trading Bot — Strategy Rules & Execution Mechanics

**Document Version:** 1.0.0  
**Phase:** P01 — Master Project Brief, Architecture & Foundation  
**Status:** Approved Foundation  

---

## 1. Strategy Overview (Amendment v1.1)

The strategy combines multi-timeframe directional trend identification using synthetic Heikin-Ashi candles with market-structure stop-loss placement derived from actual price swing pivots. It trades high-momentum instruments selected dynamically from the top 24-hour gainers on Binance USDⓈ-M Futures.

Under **Strategy Amendment v1.1**, the system operates a **global multi-symbol confirmation window** spanning the duration of each 15-minute candle. At each newly closed 15m candle, all qualifying candidates from the top-20 snapshot are registered and monitored concurrently across all subsequent 1m closed candles. The first candidate to confirm globally claims the single execution slot.

```mermaid
flowchart TD
    A["Close of 15m Candle (T_15)"] --> B["Snapshot Top 20 24h USDT Perpetuals"]
    B --> C["Evaluate 15m Closed HA for all 20 Symbols"]
    C --> D["Register Candidates in Shared Confirmation Window [T_15, T_15+15m]"]
    
    D --> E["Concurrently Monitor Candidates on every 1m Closed HA Candle"]
    E -->|Candidate 1m HA Matches 15m Direction| F["Candidate Qualifies"]
    E -->|Candidate 1m HA Disagrees or Doji| G["Continue Monitoring in Window"]
    
    F --> H{"Global Execution Slot Claimed?"}
    H -->|Already Claimed| I["Ignore Subsequent Confirmation"]
    H -->|First Global Confirmation| J["Claim Global Slot & Cancel Other Watchers"]
    
    J --> K{"Simultaneous Confirmations on same 1m?"}
    K -->|Yes| L["Select Winner via Original Top-20 Rank (Tie-Breaker: Symbol ASC)"]
    K -->|No| M["Winner Selected"]
    
    L --> N["Find Latest Confirmed 1m Swing Pivot (5L/5R Actual OHLC)"]
    M --> N
    
    N -->|Found| O["Handoff to Risk Validation Engine"]
    N -->|Not Found| P["Reject Trade (Window Consumed)"]
    
    O -->|Passes Risk & Leverage & Filters| Q["Acquire Position Lock & Execute Market Order"]
    O -->|Fails Risk / Margin / Limits| R["Reject Trade (Window Consumed, No 2nd Pick)"]
    
    E -->|15m Window Boundary Reached| S["Expire Unconfirmed Candidates & Start New Window"]
```

---

## 2. Market Universe Selection

1. **Exchange Segment:** Binance USDⓈ-M Futures (`/fapi/v1/exchangeInfo`).
2. **Contract Eligibility Criteria:**
   - `contractType == "PERPETUAL"`
   - `quoteAsset == "USDT"`
   - `status == "TRADING"`
3. **Ranking Metric:**
   - 24-hour percentage price change (`priceChangePercent`) from `/fapi/v1/ticker/24hr`.
   - Sorted in descending order (highest percentage gainer first).
4. **Universe Size:** Top 20 eligible symbols.
5. **Deterministic Tie-Breaking:**
   - In case two symbols share identical `priceChangePercent`:
     1. Primary tie-breaker: 24-hour quote volume (`quoteVolume` descending).
     2. Secondary tie-breaker: Lexicographical symbol name ascending (`BTCUSDT` before `ETHUSDT`).
6. **Liquidity Filters:**
   - Exclude symbols with 24h quote volume $< 1,000,000\text{ USDT}$ (configurable via `SCANNER_MIN_QUOTE_VOLUME_24H`).
7. **Refresh Interval:**
   - Executed automatically every 60 seconds.
8. **In-Flight Symbol Protection:**
   - A symbol that qualified at the 15m trigger remains eligible for its 1m confirmation window even if it drops out of the top 20 during a scanner refresh within that window.
   - However, no *new* 15m triggers may be established for symbols outside the active top 20.

---

## 3. Heikin-Ashi Calculation Specification

All directional decisions rely strictly on completed candles (`is_closed == True`).

### 3.1 Formulas
Let actual candle $k_t$ have prices $(Open_t, High_t, Low_t, Close_t)$. The synthetic Heikin-Ashi candle $HA_t = (HA\_Open_t, HA\_High_t, HA\_Low_t, HA\_Close_t)$ is computed as follows:

1. **HA Close:**
   $$HA\_Close_t = \frac{Open_t + High_t + Low_t + Close_t}{4}$$

2. **HA Open:**
   - For initial bar ($t=0$ or first available history):
     $$HA\_Open_0 = \frac{Open_0 + Close_0}{2}$$
   - For all subsequent bars ($t > 0$):
     $$HA\_Open_t = \frac{HA\_Open_{t-1} + HA\_Close_{t-1}}{2}$$

3. **HA High:**
   $$HA\_High_t = \max(High_t, HA\_Open_t, HA\_Close_t)$$

4. **HA Low:**
   $$HA\_Low_t = \min(Low_t, HA\_Open_t, HA\_Close_t)$$

### 3.2 Directional Classification
With floating-point epsilon $\epsilon = 10^{-8}$:
- **Bullish:** $HA\_Close_t - HA\_Open_t > \epsilon$
- **Bearish:** $HA\_Open_t - HA\_Close_t > \epsilon$
- **Doji / Neutral:** $|HA\_Close_t - HA\_Open_t| \le \epsilon$

---

## 4. Multi-Timeframe Trigger & Confirmation Workflow (Amendment v1.1)

### 4.1 15-Minute Directional Trigger Window Creation
- Evaluated when a 15m candle closes (e.g. 5:00 PM, 5:15 PM UTC):
  1. Take a snapshot of the current top 20 eligible USDT perpetual gainers.
  2. For every symbol, evaluate its newly closed 15m HA candle.
  3. Register all symbols with directional bias:
     - **Bullish 15m HA ($HA\_Close > HA\_Open$):** Candidate for `LONG`.
     - **Bearish 15m HA ($HA\_Close < HA\_Open$):** Candidate for `SHORT`.
     - **Doji ($|HA\_Close - HA\_Open| \le 10^{-8}$):** Ignored.
  4. Create one shared global confirmation window spanning from $T_{15}$ to $T_{15} + 15\text{ minutes}$.
  5. The candidate list and their snapshot rankings remain frozen for this window.

### 4.2 Concurrent Multi-Candle 1m Confirmation
- All registered candidates are monitored concurrently for every newly closed 1m candle during the active 15m window.
- **Candidate Alignment:**
  - A candidate qualifies when its newly closed 1m HA candle matches its registered 15m bias (Bullish for Long, Bearish for Short).
  - If a 1m candle is opposite or doji, the candidate is **NOT** discarded; monitoring continues on subsequent 1m candles.
  - A candidate can qualify only once per 15m window.

### 4.3 Global First-Trigger-Wins Arbitration
- Exactly **one** candidate across all monitored symbols can claim the global entry slot during the window.
- The first candidate to produce a valid 1m directional confirmation claims the slot.
- Once claimed:
  1. All other candidate monitoring tasks are safely cancelled.
  2. Exactly one trade signal is emitted.
  3. Subsequent confirmations within the same window are ignored.

### 4.4 Deterministic Simultaneous Confirmation Tie-Breaking
- If multiple candidates confirm on the **exact same 1m candle close timestamp**:
  1. The candidate with the highest ranking from the original top-20 snapshot taken at the beginning of the window wins.
  2. In case of identical ranking, secondary tie-breaker is lexicographical symbol name ascending (e.g. `BTCUSDT` before `ETHUSDT`).
  3. Asynchronous execution order never determines the winner.

### 4.5 Post-Confirmation Risk Validation & Single Window Attempt
- The winning candidate is submitted to the Risk Management Engine for complete validation.
- If the winning candidate fails risk validation (e.g. stop distance too small/large, insufficient margin, invalid swing, liquidation price clash):
  - The trade is rejected and logged.
  - **No second candidate from the same window may be chosen.**
  - The window is considered consumed.

### 4.6 Window Expiration
- At the close of the next 15m candle:
  - All unconfirmed candidates and active watchers from the expired window are cancelled.
  - Late-arriving events for the expired window are rejected.
  - A fresh top-20 snapshot is taken, and a new confirmation window begins.

---

## 5. Swing Detection Specification (Stop-Loss Reference)

### 5.1 Price Source
Swing detection uses **actual market OHLC data**, never synthetic Heikin-Ashi values.

### 5.2 5-Left / 5-Right Pivot Rules
A candle at index $i$ is a confirmed pivot if and only if:
- **Swing Low (Pivot Low):**
  $$Low_i < Low_j \quad \forall j \in \{i-5, i-4, i-3, i-2, i-1\} \quad \text{AND} \quad Low_i < Low_k \quad \forall k \in \{i+1, i+2, i+3, i+4, i+5\}$$
- **Swing High (Pivot High):**
  $$High_i > High_j \quad \forall j \in \{i-5, i-4, i-3, i-2, i-1\} \quad \text{AND} \quad High_i > High_k \quad \forall k \in \{i+1, i+2, i+3, i+4, i+5\}$$

### 5.3 Confirmation & Timing (No Look-Ahead)
- The pivot at index $i$ is confirmed **only after candle $i+5$ has closed**.
- Earlier unconfirmed pivots cannot be used.
- Equal highs or lows do not satisfy the strict inequality and are rejected.

### 5.4 Stop-Loss Distance & Buffer
Let $SwingPrice$ be the price of the latest eligible confirmed 1m swing pivot within a lookback window of 100 bars:
- **LONG Trade:**
  $$SL = SwingLow \times (1 - \text{buffer})$$
  *(where buffer is configurable, default: 0.1% / 0.001)*
  - Requirement: $SL < EntryPrice$. If $SL \ge EntryPrice$, reject the trade.
- **SHORT Trade:**
  $$SL = SwingHigh \times (1 + \text{buffer})$$
  - Requirement: $SL > EntryPrice$. If $SL \le EntryPrice$, reject the trade.
- If no valid confirmed pivot exists in the lookback window, the trade is **rejected**.

---

## 6. Reward-to-Risk Ratio & Take-Profit Calculation

The strategy enforces a fixed **3:1 gross reward-to-risk ratio (3R)**:

1. **Stop Distance ($\Delta_{stop}$):**
   - For LONG: $\Delta_{stop} = Entry - SL$
   - For SHORT: $\Delta_{stop} = SL - Entry$

2. **Take-Profit Level ($TP$):**
   - For LONG: $TP = Entry + (3 \times \Delta_{stop})$
   - For SHORT: $TP = Entry - (3 \times \Delta_{stop})$

3. **Validation:**
   - Both $SL$ and $TP$ prices must conform to exchange `PRICE_FILTER` tick size (`tickSize`).
   - Rounding of $SL$:
     - For LONG: round **DOWN** to nearest tick size (maintains $\ge$ intended distance).
     - For SHORT: round **UP** to nearest tick size.
   - Rounding of $TP$:
     - For LONG: round **UP** to nearest tick size.
     - For SHORT: round **DOWN** to nearest tick size.
