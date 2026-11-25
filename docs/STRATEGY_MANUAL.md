# Strategy Manual

Implemented in [src/indicators/heikin_ashi.py](../src/indicators/heikin_ashi.py), [src/strategy/engine.py](../src/strategy/engine.py), and [src/strategy/arbitrator.py](../src/strategy/arbitrator.py).

---

## 1. Heikin-Ashi Calculation
Using **completed** market OHLC candles only:
$$\text{HA\_Close} = \frac{O + H + L + C}{4}$$
$$\text{HA\_Open}_0 = \frac{O_0 + C_0}{2}, \qquad \text{HA\_Open}_t = \frac{\text{HA\_Open}_{t-1} + \text{HA\_Close}_{t-1}}{2}$$
$$\text{HA\_High} = \max(H, \text{HA\_Open}, \text{HA\_Close}), \qquad \text{HA\_Low} = \min(L, \text{HA\_Open}, \text{HA\_Close})$$

- **Bullish:** $\text{HA\_Close} > \text{HA\_Open}$
- **Bearish:** $\text{HA\_Close} < \text{HA\_Open}$
- **Doji (neutral):** $\text{HA\_Close} == \text{HA\_Open}$

---

## 2. Multi-Timeframe Signal Lifecycle

1. **15-Minute Trigger & Frozen Snapshot:**
   - At each completed `15m` candle close, the engine freezes the latest Top-20 scanner snapshot.
   - Symbols entering the Top 20 mid-window are ignored until the next `15m` boundary.
   - Any unconfirmed candidates from the previous window expire.
   - Every symbol in the frozen snapshot with a **Bullish** 15m HA candle becomes a `LONG` candidate; **Bearish** becomes a `SHORT` candidate (15m Doji creates no candidate).
2. **1-Minute Confirmation Monitoring:**
   - Across the next 15 one-minute closes, each candidate watches its `1m` HA candle.
   - **Opposite or Doji `1m` HA candles do NOT invalidate a candidate.** The bot waits for the first aligned `1m` HA candle within the window.
3. **Global Winner & Deterministic Tie-Breaking:**
   - The first candidate globally to print an aligned `1m` HA candle wins the window (`GlobalSlotArbitrator`).
   - If multiple candidates confirm on the **same `1m` close timestamp**, the candidate with the **lowest `candidate_rank`** (highest 24h gainer rank in the frozen Top-20 snapshot) wins; alphabetical `symbol` breaks exact ties.
   - Once a winner is chosen, all other candidates in that window are cancelled.
   - If the winner fails Swing or Risk validation, the entire `15m` window is marked **CONSUMED**—no secondary candidate is picked.
4. **Global 1-Position Lock:**
   - If a bot-managed position is already open, no new entry is taken.

---

## 3. Worked Hypothetical Trade Example

- **10:15:00 UTC:** Scanner snapshot ranks `#1 ABCUSDT (+18.4%)`, `#2 XYZUSDT (+14.2%)`.
- **10:15:00 UTC (15m close):** `ABCUSDT` prints Bullish 15m HA ($\text{HA\_Close} = 102.0 > \text{HA\_Open} = 100.5$) → `LONG` candidate (Rank 1). `XYZUSDT` prints Bullish 15m HA → `LONG` candidate (Rank 2).
- **10:16:00 UTC (1m close #1):** `ABCUSDT` prints Bearish 1m HA → candidate stays `MONITORING` (not invalidated).
- **10:17:00 UTC (1m close #2):** Both `ABCUSDT` and `XYZUSDT` print Bullish 1m HA at `10:17:00`.
- **Arbitration:** Same timestamp → Rank 1 (`ABCUSDT`) wins; `XYZUSDT` is cancelled.
- **Swing & Risk Sizing:** Latest confirmed 5L/5R swing low on `ABCUSDT` 1m OHLC is `98.00`. With `0.1%` buffer: $\text{SL} = 98.00 \times 0.999 = 97.90$. Entry price $= 102.00$, stop distance $= 4.10$. On $1{,}000$ USDT equity, nominal risk $= 100$ USDT → friction-adjusted sizing computes quantity, sets `TP = 102.00 + 3 \times 4.10 = 114.30` (3:1 RR), checks isolated margin and liquidation safety, and submits `MARKET` BUY + `STOP_MARKET` SL + `TAKE_PROFIT_MARKET` TP.
