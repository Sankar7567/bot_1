# Binance Futures HA Trading Bot — API Contracts & Internal Interfaces

**Document Version:** 1.0.0  
**Phase:** P01 — Master Project Brief, Architecture & Foundation  
**Status:** Approved Foundation  

---

## 1. Binance USDⓈ-M Futures External Endpoints

All requests target the Binance USDⓈ-M Futures Testnet:
- **REST Base URL:** `https://testnet.binancefuture.com`
- **WebSocket Base URL:** `wss://stream.binancefuture.com/ws`

### 1.1 Market Data Endpoints (Public)
| Method | Endpoint | Purpose | Weight |
|---|---|---|---|
| `GET` | `/fapi/v1/time` | Synchronize local timestamp with Binance exchange clock. | 1 |
| `GET` | `/fapi/v1/exchangeInfo` | Query trading rules, step size, minQty, minNotional, tickSize. | 1 |
| `GET` | `/fapi/v1/ticker/24hr` | Fetch 24h rolling price change percentage for all contracts. | 40 |
| `GET` | `/fapi/v1/klines` | Fetch historical OHLCV klines for 1m and 15m initialization. | 5-10 |

### 1.2 Account & Position Endpoints (HMAC SHA-256 Signed)
| Method | Endpoint | Purpose | Weight |
|---|---|---|---|
| `GET` | `/fapi/v2/account` | Retrieve account equity, isolated balances, and initial margin. | 5 |
| `GET` | `/fapi/v2/balance` | Retrieve wallet asset balances (USDT). | 5 |
| `GET` | `/fapi/v2/positionRisk` | Query real-time position details, leverage, and liquidation prices. | 5 |
| `GET` | `/fapi/v1/leverageBracket` | Fetch notional brackets and maximum leverage per symbol. | 1 |
| `POST` | `/fapi/v1/positionSide/dual` | Ensure account is in ONE-WAY position mode (`dualSidePosition=false`). | 1 |
| `POST` | `/fapi/v1/marginType` | Configure symbol margin type to `ISOLATED`. | 1 |
| `POST` | `/fapi/v1/leverage` | Set symbol leverage to the bracket maximum. | 1 |

### 1.3 Order Execution Endpoints (HMAC SHA-256 Signed)
| Method | Endpoint | Purpose | Weight |
|---|---|---|---|
| `POST` | `/fapi/v1/order` | Submit market entry or conditional protective orders (`reduceOnly`). | 1 |
| `GET` | `/fapi/v1/order` | Query status of an order using `clientOrderId` or `orderId`. | 1 |
| `DELETE`| `/fapi/v1/order` | Cancel active protective order (`clientOrderId` or `orderId`). | 1 |
| `GET` | `/fapi/v1/openOrders` | Query all currently open orders (for crash recovery reconciliation). | 1 or 40 |

### 1.9 Multi-Symbol Strategy Engine Interface (`src/strategy/`)

The `MultiSymbolStrategyEngine` coordinates multi-symbol confirmation windows and first-trigger-wins slot arbitration (Strategy Amendment v1.1):
- `on_15m_candle_closed(symbols_snapshot: List[ScannedSymbol], closed_15m_ha: Dict[str, HeikinAshiCandle], timestamp_ms: int) -> Optional[ConfirmationWindow]`: Creates and registers a new 15m confirmation window.
- `on_1m_candle_closed(event: CandleCloseEvent, ha_1m: HeikinAshiCandle) -> Optional[TradeSignal]`: Evaluates 1m candle for candidate; arbitrates global entry slot if aligned.
- `process_simultaneous_confirmations(candidates: List[CandidateState]) -> Optional[TradeSignal]`: Deterministically arbitrates multiple same-timestamp confirmations using snapshot ranking.
- `handle_risk_validation_failure(window_id: str, symbol: str, reason: str) -> None`: Consumes the confirmation window when the winner fails risk validation (prevents secondary picks).
- `expire_active_window(current_time_ms: int) -> None`: Explicitly marks active window and unconfirmed candidates as expired.
- `serialize_state() -> dict` and `restore_state(state_dict: dict) -> None`: Preserves and restores confirmation window state across restarts.

Data contracts in `src/strategy/models.py`:
- `Direction`: `LONG`, `SHORT`.
- `CandidateStatus`: `MONITORING`, `QUALIFIED`, `CANCELLED`, `EXPIRED`.
- `WindowStatus`: `ACTIVE`, `SLOT_CLAIMED`, `EXPIRED`.
- `CandidateState`: `symbol`, `direction`, `initial_rank`, `registered_at`, `trigger_15m_candle`, `status`, `confirmation_candle`, `confirmed_at`.
- `ConfirmationWindow`: `window_id`, `start_timestamp`, `end_timestamp`, `candidates`, `status`, `is_slot_claimed`, `winning_symbol`, `winning_signal`, `window_consumed`.
- `TradeSignal`: `signal_id`, `window_id`, `symbol`, `direction`, `trigger_15m_timestamp`, `confirmation_1m_timestamp`, `trigger_price`, `confirmation_price`, `candidate_rank`, `created_at`, `expires_at`.

### 1.10 Swing Detection Engine Interface (`src/indicators/swing_points.py`)

The `src/indicators/swing_points.py` module detects confirmed swing high and low pivot points on actual market OHLC data and calculates stop-loss levels with buffer offsets:
- `find_confirmed_pivots(candles: List[Candle], left_bars: int = 5, right_bars: int = 5) -> List[SwingPivot]`: Scans a chronological candle sequence for 5L/5R pivots satisfying strict inequality ($Low_i < Low_{j,k}$ or $High_i > High_{j,k}$). Confirmed at bar $i + \text{right\_bars}$.
- `get_latest_eligible_pivot(candles: List[Candle], pivot_type: PivotType, left_bars: int = 5, right_bars: int = 5, lookback_bars: int = 100) -> Optional[SwingPivot]`: Searches backwards for the most recent confirmed swing pivot of the specified type within the lookback window.
- `calculate_swing_stop_loss(entry_price: float, pivot: SwingPivot, is_long: bool, buffer_ratio: float = 0.001) -> StopLossCalculation`: Computes stop loss price and distance with configurable buffer (LONG: $SL = \text{Low} \times (1 - \text{buffer})$, SHORT: $SL = \text{High} \times (1 + \text{buffer})$) and validates stop distance.
- `SwingDetector`: Stateful per-symbol wrapper providing `get_latest_swing_low` and `get_latest_swing_high`.

Data contracts in `src/indicators/swing_points.py`:
- `PivotType`: `SWING_LOW`, `SWING_HIGH`.
- `SwingPivot`: `symbol`, `interval`, `pivot_type`, `bar_index`, `pivot_timestamp`, `confirmation_timestamp`, `price`, `left_bars`, `right_bars`.
- `StopLossCalculation`: `pivot`, `entry_price`, `buffer_ratio`, `stop_price`, `stop_distance`, `is_valid`, `rejection_reason`.

### 1.11 Risk Management Engine Interface (`src/risk/`)

The `src/risk/` package performs end-to-end position sizing and trade validation:

**`src/risk/calculator.py`** — Pure stateless formulas:
- `calculate_risk_budget(equity, risk_ratio=0.10) -> float`: Returns `equity × risk_ratio`.
- `calculate_stop_distance(entry_price, sl_price, is_long) -> float`: Returns `|entry − sl|`; raises if ≤ 0.
- `calculate_tp_price(entry_price, sl_price, is_long, rr_ratio=3.0) -> float`: LONG: `entry + rr × dist`; SHORT: `entry − rr × dist`.
- `calculate_friction_adjusted_quantity(risk_budget, stop_distance, entry_price, sl_price, taker_fee_rate=0.0005, slippage_rate=0.0005) -> float`: $Q = \frac{R}{\Delta P_{sl} + P_e(r_f + r_s) + P_{sl} \cdot r_f}$.
- `calculate_required_margin(quantity, entry_price, leverage) -> float`: `(qty × entry) / leverage`.
- `estimate_liquidation_price(entry_price, leverage, is_long, maint_margin_ratio) -> float`: LONG: $P_e(1 - 1/L + MMR)$; SHORT: $P_e(1 + 1/L - MMR)$.
- `is_liq_price_safer_than_sl(liq_price, sl_price, is_long) -> bool`: True if stop fires before liquidation.
- `compute_pnl_at_target(quantity, entry_price, target_price, is_long, taker_fee_rate) -> float`: Net PnL with two-sided fees.

**`src/risk/brackets.py`** — Leverage bracket matching:
- `get_bracket_for_notional(symbol_brackets, notional) -> Optional[BracketInfo]`: First-fit from sorted tiers.
- `get_max_eligible_leverage(symbol_brackets, notional) -> int`: Convenience wrapper returning `BracketInfo.max_leverage`.

**`src/risk/validator.py`** — Full validation pipeline:
- `truncate_to_step_size(quantity, step_size) -> float`: Floor-only truncation with integer arithmetic.
- `round_price_to_tick(price, tick_size) -> float`: Round-half-up to nearest tick.
- `validate_and_size_position(sizing_input: SizingInput) -> SizingResult`: 13-step sequential validation.

Data contracts in `src/risk/models.py`:
- `BracketInfo`: `bracket_number`, `max_leverage`, `notional_cap`, `notional_floor`, `maint_margin_ratio`.
- `SizingInput`: `symbol`, `equity`, `is_long`, `entry_price`, `sl_price`, `step_size`, `min_qty`, `max_qty`, `min_notional`, `tick_size`, `available_margin`, `bracket`, `risk_ratio`, `rr_ratio`, `taker_fee_rate`, `slippage_rate`.
- `SizingResult`: `is_valid`, `rejection_reason`, `symbol`, `is_long`, `equity`, `risk_budget`, `stop_distance`, `raw_quantity`, `friction_adjusted_qty`, `final_quantity`, `entry_price`, `sl_price`, `tp_price`, `leverage`, `required_margin`, `estimated_liq_price`, `maint_margin_ratio`.

### 1.12 Order Execution Engine Interface (`src/execution/`)

The `src/execution/` package coordinates order preparation, market order entry, network error recovery, fill tracking, and order querying:

**`src/execution/client_id.py`** — Deterministic clientOrderId generator:
- `generate_client_order_id(prefix: OrderPrefix | str, timestamp_ms: Optional[int] = None, hex_len: int = 6) -> str`: Generates collision-resistant identifier $\le 36$ characters.
- `is_bot_client_order_id(client_order_id: str) -> bool`: Regex verification of bot-originating order IDs.
- `parse_client_order_id(client_order_id: str) -> Optional[dict]`: Deconstructs client order ID into prefix, timestamp, and random hex components.

**`src/execution/order_router.py`** — OrderRouter:
- `execute_market_entry(symbol, side, quantity, leverage, client_order_id=None, fill_timeout_seconds=5.0, poll_interval_seconds=0.2) -> OrderFill`:
  1. Sets margin type to `ISOLATED` (absorbs -4046 idempotently).
  2. Sets symbol leverage to target leverage.
  3. Submits `MARKET` order with traceable `clientOrderId`.
  4. Automatically reconciles network timeouts by querying exchange with `clientOrderId` prior to declaring failure.
  5. Polls for fill completion if market order does not fill immediately.
  6. Handles partial fills by returning executed quantity and average fill price.
- `query_order_fill(symbol, client_order_id=None, order_id=None) -> OrderFill`: Queries live order status from Binance Futures.
- `cancel_order(symbol, client_order_id=None, order_id=None) -> Dict[str, Any]`: Cancels an open order idempotently (absorbs -2011 unknown order and -2013 not found).

Data contracts in `src/execution/models.py`:
- `OrderSide`: `BUY`, `SELL`.
- `OrderType`: `MARKET`, `LIMIT`, `STOP_MARKET`, `TAKE_PROFIT_MARKET`.
- `OrderStatus`: `NEW`, `PARTIALLY_FILLED`, `FILLED`, `CANCELED`, `REJECTED`, `EXPIRED`.
- `OrderExecutionRequest`: `symbol`, `side`, `order_type`, `quantity`, `client_order_id`, `reduce_only`, `stop_price`, `price`, `time_in_force`.
- `OrderFill`: `symbol`, `client_order_id`, `order_id`, `status`, `side`, `order_type`, `orig_qty`, `executed_qty`, `avg_price`, `cum_quote`, `commission`, `commission_asset`, `transact_time`, `raw_response`. Helpers: `is_fully_filled`, `is_partially_filled`.

### 1.13 Protective Order Manager Interface (`src/positions/`)

The `src/positions/` package manages exchange-side protective orders (`STOP_MARKET` and `TAKE_PROFIT_MARKET`), directional tick rounding, post-entry SL/TP recalculation, atomic sibling order cancellation, and fail-safe emergency market close procedures:

**`src/positions/protective.py`** — ProtectiveOrderManager:
- `round_price_directional(price: float, tick_size: float, round_up: bool) -> float`: Rounds price to tick size either strictly UP (ceil) or strictly DOWN (floor) using integer scaled arithmetic.
- `place_protective_orders(symbol, entry_side, executed_qty, avg_entry_price, stop_loss_price, tick_size, step_size, take_profit_price=None, rr_ratio=3.0, working_type='CONTRACT_PRICE', verify_on_exchange=True) -> ProtectiveOrderPair`:
  1. Recalculates SL and 3:1 TP using actual entry fill price.
  2. Applies directional rounding: LONG (SL floor, TP ceil); SHORT (SL ceil, TP floor).
  3. Sizes both conditional orders strictly against `executed_qty` (`reduceOnly=true`).
  4. Places `STOP_MARKET` with `HA_SL_` clientOrderId and `TAKE_PROFIT_MARKET` with `HA_TP_` clientOrderId.
  5. Verifies both orders are in `NEW` state on exchange.
  6. On placement/verification failure, executes emergency mitigation: cancels any placed order and executes emergency market close.
- `cancel_sibling_order(triggered_client_order_id=None, triggered_order_id=None, active_pair=None, symbol=None, verify_position_closed=True) -> ExitTriggerResult`:
  - When SL fills, cancels TP order.
  - When TP fills, cancels SL order.
  - Verifies position is closed on exchange (zero net exposure).
- `verify_position_closed(symbol, timeout_seconds=5.0, poll_interval_seconds=0.2) -> bool`: Queries exchange position to verify `not pos.is_open`.
- `execute_emergency_market_close(symbol, position_side, quantity) -> EmergencyExitResult`: Submits reduceOnly `MARKET` order with `HA_EMG_` clientOrderId to immediately eliminate exposure.

Data contracts in `src/positions/models.py`:
- `ExitReason`: `STOP_LOSS`, `TAKE_PROFIT`, `EMERGENCY`, `MANUAL`.
- `ProtectiveOrderPair`: `symbol`, `entry_side`, `exit_side`, `quantity`, `avg_entry_price`, `sl_order_id`, `sl_client_order_id`, `sl_price`, `tp_order_id`, `tp_client_order_id`, `tp_price`, `created_at`, `is_active`. Helpers: `is_sl_order`, `is_tp_order`, `get_sibling_order_details`.
- `ExitTriggerResult`: `symbol`, `triggered_client_order_id`, `triggered_type`, `exit_reason`, `cancelled_sibling_client_order_id`, `sibling_cancellation_success`, `position_closed_verified`, `timestamp_ms`.
- `EmergencyExitResult`: `symbol`, `emergency_client_order_id`, `quantity`, `is_closed`, `error_message`, `timestamp_ms`.

### 1.8 Heikin-Ashi Indicator Interface (`src/indicators/`)

The `src/indicators/heikin_ashi.py` module provides pure mathematical Heikin-Ashi transformations:
- `calculate_heikin_ashi_candle(candle: Candle, prev_ha: Optional[HeikinAshiCandle]) -> HeikinAshiCandle`: Transforms single closed candle using documented formulas.
- `calculate_heikin_ashi_series(candles: List[Candle]) -> List[HeikinAshiCandle]`: Computes chronological HA sequence across a series.
- `HeikinAshiSeries`: Stateful manager per `(symbol, interval)` maintaining rolling calculated HA bars with idempotent updates and capacity eviction.

Data contracts in `src/indicators/heikin_ashi.py`:
- `HeikinAshiCandle`: `symbol`, `interval`, `open_time`, `close_time`, `ha_open`, `ha_high`, `ha_low`, `ha_close`, `is_closed`. Helpers: `is_bullish`, `is_bearish`, `is_doji`, `body_size`, `upper_wick`, `lower_wick`.

### 1.7 Market Data Engine Interface (`src/market_data/`)

The `MarketDataEngine` coordinates 1m and 15m candle streaming and history across monitored candidate symbols:
- `seed_symbol_history(symbol: str, intervals: Tuple[str, ...], limits: Dict[str, int]) -> None`: Fetches historical closed klines via REST and populates chronological buffers.
- `reconcile_rest_snapshot(symbol: str, interval: str, limit: int = 50) -> int`: Fetches latest REST klines and backfills missing closed bars idempotently.
- `process_kline_payload(payload: dict) -> Optional[CandleCloseEvent]`: Ingests WebSocket kline events; strictly filters out unfinished bars (`k.x == false`), adds closed candles to buffers, and dispatches `CandleCloseEvent`.
- `subscribe_candle_closed(listener: Callable[[CandleCloseEvent], Coroutine]) -> None`: Subscribes async callbacks to closed-candle events.
- `get_candles(symbol: str, interval: str, n: Optional[int] = None) -> List[Candle]`: Retrieves chronological closed candle slice.
- `get_latest_closed_candle(symbol: str, interval: str) -> Optional[Candle]`: Retrieves latest completed bar.
- `is_data_stale(symbol: str, interval: str, max_staleness_ms: Optional[int] = None) -> bool`: Verifies data recency against exchange clock.
- `set_monitored_universe(symbols: List[str], intervals: Tuple[str, ...]) -> None`: Pre-seeds history and establishes WebSocket subscriptions for up to 20 candidate symbols concurrently (Amendment v1.1).

Data contracts in `src/market_data/models.py`:
- `Candle`: `symbol`, `interval`, `open_time`, `close_time`, `open`, `high`, `low`, `close`, `volume`, `quote_volume`, `trades_count`, `is_closed`. Helpers: `is_bullish`, `is_bearish`, `duration_ms`.
- `CandleCloseEvent`: `symbol`, `interval`, `candle`, `emitted_at`.

### 1.6 Market Scanner Interface (`src/scanner/`)

The `MarketScanner` provides:
- `scan() -> ScanResult`: Queries `/fapi/v1/exchangeInfo` and `/fapi/v1/ticker/24hr`, applies contract and volume filters, deterministically ranks gainers, and selects the top N symbols.
- `last_scan -> Optional[ScanResult]`: Accesses the most recent scan snapshot.
- `start_periodic_scan(interval_sec: int = 60) -> None`: Initiates an asynchronous background scanning loop.
- `stop_periodic_scan() -> None`: Gracefully cancels the background scanning task.

Data contracts in `src/scanner/models.py`:
- `ScannedSymbol`: `rank`, `symbol`, `price_change_percent`, `quote_volume`, `last_price`, `base_asset`, `quote_asset`, `contract_type`, `scan_timestamp`.
- `ScanResult`: `scan_timestamp`, `symbols`, `total_evaluated_count`, `total_eligible_count`, `scan_duration_ms`. Helper methods: `get_symbol_names()`, `get_rank(symbol)`, `is_in_universe(symbol)`.

The `BinanceFuturesClient` provides the following verified methods:
- `ping() -> bool`: Basic connectivity test (`GET /fapi/v1/ping`).
- `get_server_time() -> ServerTimeResponse`: Retrieves exchange time and computes clock drift offset (`GET /fapi/v1/time`).
- `get_exchange_info() -> Dict[str, SymbolInfo]`: Retrieves active contracts and parses trading filters (`GET /fapi/v1/exchangeInfo`).
- `get_symbol_filters(symbol: str) -> SymbolFilter`: Retrieves parsed tick size, step size, minQty, and minNotional.
- `get_account_info() -> AccountInfo`: Signed query of margin equity, wallet balance, and trade permissions (`GET /fapi/v2/account`).
- `get_balances() -> List[AssetBalance]`: Signed query of asset-level balances (`GET /fapi/v2/balance`).
- `get_positions(symbol: Optional[str] = None) -> List[PositionRisk]`: Real-time position risk, leverage, and liquidation prices (`GET /fapi/v2/positionRisk`).
- `get_open_orders(symbol: Optional[str] = None) -> List[OpenOrder]`: Active open order inspection (`GET /fapi/v1/openOrders`).
- `get_leverage_brackets(symbol: Optional[str] = None) -> Dict[str, SymbolLeverageBrackets]`: Leverage bracket and notional tier limits (`GET /fapi/v1/leverageBracket`).
- `get_position_mode() -> Dict[str, Any]`: Checks if position mode is One-Way (`GET /fapi/v1/positionSide/dual`).
- `set_position_mode(dual_side: bool = False) -> Dict[str, Any]`: Configures account to One-Way mode (`POST /fapi/v1/positionSide/dual`).
- `change_margin_type(symbol: str, margin_type: str = "ISOLATED") -> Dict[str, Any]`: Sets isolated margin mode (`POST /fapi/v1/marginType`).
- `change_leverage(symbol: str, leverage: int) -> Dict[str, Any]`: Sets maximum eligible leverage (`POST /fapi/v1/leverage`).

---

## 2. Internal Python Typed Data Structures

All internal contracts are implemented as immutable dataclasses or Pydantic models with strict runtime type validation.

```python
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Direction(str, Enum):
  LONG = "LONG"
  SHORT = "SHORT"


class PositionStatus(str, Enum):
  IDLE = "IDLE"
  PENDING_ENTRY = "PENDING_ENTRY"
  ACTIVE = "ACTIVE"
  PENDING_EXIT = "PENDING_EXIT"
  CLOSED = "CLOSED"
  EMERGENCY = "EMERGENCY"


class PivotType(str, Enum):
  SWING_LOW = "SWING_LOW"
  SWING_HIGH = "SWING_HIGH"


@dataclass(frozen=True)
class Candle:
  """Standard OHLCV Candle."""

  timestamp: int  # Open time in ms UTC
  open: float
  high: float
  low: float
  close: float
  volume: float
  close_time: int  # Close time in ms UTC
  is_closed: bool


@dataclass(frozen=True)
class HeikinAshiCandle:
  """Synthetic Heikin-Ashi Candle."""

  timestamp: int
  ha_open: float
  ha_high: float
  ha_low: float
  ha_close: float
  is_closed: bool

  @property
  def is_bullish(self) -> bool:
    return self.ha_close > self.ha_open

  @property
  def is_bearish(self) -> bool:
    return self.ha_close < self.ha_open

  @property
  def is_doji(self) -> bool:
    return abs(self.ha_close - self.ha_open) <= 1e-8


@dataclass(frozen=True)
class SwingPivot:
  """Confirmed 5-left / 5-right Swing Pivot."""

  bar_index: int
  pivot_timestamp: int
  confirmation_timestamp: int  # Time when bar_index + 5 closed
  price: float
  pivot_type: PivotType


@dataclass(frozen=True)
class CandidateState:
  """State of a candidate symbol within an active confirmation window."""

  symbol: str
  direction: Direction
  initial_rank: int
  registered_at: int
  confirmed: bool = False
  confirmation_timestamp: Optional[int] = None


@dataclass(frozen=True)
class ConfirmationWindow:
  """15-minute global confirmation window tracking candidates."""

  window_id: str
  start_timestamp: int  # 15m candle close / window open
  end_timestamp: int  # Next 15m candle close / window expiration
  candidates: dict[str, CandidateState]
  is_slot_claimed: bool = False
  winning_symbol: Optional[str] = None
  is_expired: bool = False


@dataclass(frozen=True)
class TradeSignal:
  """Directional signal generated by strategy."""

  signal_id: str
  symbol: str
  direction: Direction
  trigger_15m_timestamp: int
  confirmation_1m_timestamp: int
  pivot: SwingPivot
  estimated_entry: float
  stop_loss_price: float
  take_profit_price: float
  created_at: int
  expires_at: int


@dataclass(frozen=True)
class SizingResult:
  """Risk management sizing evaluation."""

  is_valid: bool
  rejection_reason: Optional[str]
  nominal_risk_budget: float
  calculated_quantity: float
  rounded_quantity: float
  estimated_entry_price: float
  stop_loss_price: float
  take_profit_price: float
  leverage: int
  required_isolated_margin: float
  estimated_liquidation_price: float
```

---

## 3. Order ID Generation Specification

To ensure end-to-end idempotency, all orders submitted by the bot generate a deterministic, collision-resistant `clientOrderId`:

$$\text{clientOrderId} = \text{PREFIX} + \text{TIMESTAMP\_MS} + \text{RANDOM\_HEX}$$
- Format example: `HA_E_1711234567890_a1f9` (Entry)
- Format example: `HA_SL_1711234567890_a1f9` (Stop-Loss)
- Format example: `HA_TP_1711234567890_a1f9` (Take-Profit)

Max length supported by Binance: 36 characters. The prefix ensures instant identification of bot-managed orders during recovery.
