# Risk Management Manual

Implemented in [src/indicators/swing_points.py](../src/indicators/swing_points.py), [src/risk/calculator.py](../src/risk/calculator.py), [src/risk/brackets.py](../src/risk/brackets.py), and [src/risk/validator.py](../src/risk/validator.py).

---

## 1. 5-Left / 5-Right Swing Pivot Stop-Loss
- Computed strictly on **actual market 1m OHLC** candles (never Heikin-Ashi synthetic highs/lows).
- A **Swing Low** at bar $i$ requires $L_i < L_{i-k}$ for $k \in \{1..5\}$ and $L_i < L_{i+k}$ for $k \in \{1..5\}$.
- A **Swing High** at bar $i$ requires $H_i > H_{i-k}$ for $k \in \{1..5\}$ and $H_i > H_{i+k}$ for $k \in \{1..5\}$.
- **Zero Look-Ahead:** A pivot at bar $i$ is not confirmed until bar $i + 5$ has closed.
- **Stop-Loss Buffer:**
  - `LONG`: $\text{SL} = \text{SwingLow} \times (1 - \text{SWING\_STOP\_BUFFER\_RATIO})$ (default `0.001` = 0.1% below pivot)
  - `SHORT`: $\text{SL} = \text{SwingHigh} \times (1 + \text{SWING\_STOP\_BUFFER\_RATIO})$ (0.1% above pivot)
- If no confirmed pivot exists, or $\text{SL}$ is on the wrong side of entry, the trade is rejected.

---

## 2. Position Sizing, Friction & 3:1 Reward-to-Risk
1. **Nominal Risk Budget:** $\text{RiskBudget} = \text{Equity} \times \text{NOMINAL\_RISK\_RATIO}$ (default `0.10` = 10%).
2. **Stop Distance:** $D = |\text{Entry} - \text{SL}|$.
3. **Friction-Adjusted Quantity:** Accounts for `ESTIMATED_TAKER_FEE_RATIO` (0.05%) and `ESTIMATED_SLIPPAGE_RATIO` (0.05%) so total loss including round-trip entry/exit fees & slippage stays within the risk budget.
4. **Exchange Precision:** Quantity is truncated down (`floor`) to `stepSize`; prices are directionally rounded to `tickSize`.
5. **3:1 Take-Profit:**
   - `LONG`: $\text{TP} = \text{Entry} + (\text{REWARD\_RISK\_RATIO} \times D)$
   - `SHORT`: $\text{TP} = \text{Entry} - (\text{REWARD\_RISK\_RATIO} \times D)$

---

## 3. Rejection Reasons
`validate_and_size_position()` rejects a trade (and consumes the 15m window) when:
- No confirmed 5L/5R swing pivot is available.
- Rounded quantity `< minQty` or notional `< minNotional`.
- Required isolated margin `> available_margin`.
- No leverage bracket matches the notional size.
- Estimated **liquidation price is tighter than Stop-Loss** (prevents liquidating before SL triggers).
- `global_max_positions` (`1`) is already occupied.
