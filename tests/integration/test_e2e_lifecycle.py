"""End-to-End Lifecycle Integration Tests for Phase 12 — Full System Orchestration.

Validates the complete automated pipeline with mocked Binance exchange endpoints:
1. Startup & Clean Reconciliation.
2. Market Scan (Top-20 Gainers).
3. Historical REST pre-seeding (1m + 15m) & Heikin-Ashi series initialization.
4. 15m Closed Candle -> Global Confirmation Window creation with Top-20 Snapshot.
5. Simultaneous 1m Closed Confirmations -> Deterministic Top-20 Rank Arbitration.
6. 5L/5R OHLC Swing Detection -> Friction-Adjusted Risk Sizing -> Bracket Leverage.
7. Market Entry Execution -> SQLite Persistence -> Exchange SL/TP Placement.
8. Real-Time ORDER_TRADE_UPDATE Take-Profit Fill -> Sibling SL Cancellation ->
   Zero-Exposure Verification -> Position CLOSED & PnL Persisted -> Lock Released.
9. Window Consumption on Risk Failure (no secondary candidate permitted).
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest
from src.config.settings import Settings
from src.exchange.client import BinanceFuturesClient
from src.exchange.models import (
    AccountInfo,
    LeverageBracketTier,
    PositionRisk,
    SymbolFilter,
    SymbolInfo,
    SymbolLeverageBrackets,
    Ticker24hr,
)
from src.market_data.models import Candle, CandleCloseEvent
from src.orchestrator import TradingBotOrchestrator
from src.positions.models import PositionState
from src.state.reconciler import ReconciliationStatus


def _build_1m_klines_with_swing_low(
    symbol: str,
    base_ts: int,
    count: int = 30,
    swing_index: int = 20,
    swing_low_price: float = 98.0,
    base_price: float = 100.0,
) -> list:
  """Builds raw Binance REST kline arrays forming a clear 5L/5R swing low."""
  klines = []
  for i in range(count):
    open_t = base_ts + i * 60000
    close_t = open_t + 59999
    # V-shape centered at swing_index so low[swing_index] is strictly < 5 left and 5 right
    dist = abs(i - swing_index)
    if i == swing_index:
      low = swing_low_price
      high = swing_low_price + 1.0
      o = swing_low_price + 0.5
      c = swing_low_price + 0.6
    elif dist <= 6:
      low = swing_low_price + 0.5 + dist * 0.2
      high = low + 1.2
      o = low + 0.2
      c = low + 0.8
    else:
      low = base_price - 0.5
      high = base_price + 0.8
      o = base_price
      c = base_price + 0.3

    klines.append([
        open_t,
        str(o),
        str(high),
        str(low),
        str(c),
        "100.0",
        close_t,
        "10000.0",
        50,
        "50.0",
        "5000.0",
        "0",
    ])
  return klines


def _build_bearish_15m_klines(base_ts: int, count: int = 10) -> list:
  """Builds raw Binance REST 15m klines ending at base_ts - 1."""
  klines = []
  for i in range(count):
    open_t = base_ts - (count - i) * 900000
    close_t = open_t + 899999
    klines.append([
        open_t,
        "99.0",
        "100.0",
        "98.5",
        "99.5",
        "500.0",
        close_t,
        "50000.0",
        100,
        "250.0",
        "25000.0",
        "0",
    ])
  return klines


class TestFullEndToEndLifecycle:
  """Full integrated lifecycle tests from Market Scan through Exit Fill & DB Closure."""

  @pytest.mark.asyncio
  async def test_complete_long_trade_lifecycle_with_simultaneous_arbitration(
      self, tmp_path: Path
  ):
    base_15m_close = 1_700_000_000_000 + 899_999
    now_ms = base_15m_close + 100

    settings = Settings(
        app_env="testnet",
        db_file_path=tmp_path / "e2e_bot.db",
        nominal_risk_ratio=0.10,
        reward_risk_ratio=3.0,
        swing_confirmation_bars=5,
        swing_stop_buffer_ratio=0.001,
    )

    # Configure mocked BinanceFuturesClient
    client = MagicMock(spec=BinanceFuturesClient)
    client.get_synchronized_time = MagicMock(return_value=now_ms)
    client.synchronize_time = AsyncMock(return_value=0)
    client.close = AsyncMock()

    # Position/Orders at startup: Clean
    client.get_positions = AsyncMock(return_value=[])
    client.get_open_orders = AsyncMock(return_value=[])

    # Universe: BTCUSDT (Rank 1, +10%), ETHUSDT (Rank 2, +8%)
    btc_filters = SymbolFilter(
        symbol="BTCUSDT",
        tick_size=0.01,
        step_size=0.001,
        min_qty=0.001,
        max_qty=1000.0,
        min_notional=5.0,
        price_precision=2,
        quantity_precision=3,
    )
    eth_filters = SymbolFilter(
        symbol="ETHUSDT",
        tick_size=0.01,
        step_size=0.001,
        min_qty=0.001,
        max_qty=1000.0,
        min_notional=5.0,
        price_precision=2,
        quantity_precision=3,
    )
    client.get_exchange_info = AsyncMock(
        return_value={
            "BTCUSDT": SymbolInfo(
                symbol="BTCUSDT",
                status="TRADING",
                base_asset="BTC",
                quote_asset="USDT",
                contract_type="PERPETUAL",
                filters=btc_filters,
            ),
            "ETHUSDT": SymbolInfo(
                symbol="ETHUSDT",
                status="TRADING",
                base_asset="ETH",
                quote_asset="USDT",
                contract_type="PERPETUAL",
                filters=eth_filters,
            ),
        }
    )
    client.get_24hr_tickers = AsyncMock(
        return_value=[
            Ticker24hr(
                symbol="BTCUSDT",
                price_change=10.0,
                price_change_percent=10.0,
                weighted_avg_price=100.0,
                last_price=100.0,
                last_qty=1.0,
                open_price=90.0,
                high_price=102.0,
                low_price=89.0,
                volume=100000.0,
                quote_volume=10_000_000.0,
                open_time=now_ms - 86400000,
                close_time=now_ms,
                first_id=1,
                last_id=100,
                count=100,
            ),
            Ticker24hr(
                symbol="ETHUSDT",
                price_change=8.0,
                price_change_percent=8.0,
                weighted_avg_price=100.0,
                last_price=100.0,
                last_qty=1.0,
                open_price=92.0,
                high_price=101.0,
                low_price=91.0,
                volume=80000.0,
                quote_volume=8_000_000.0,
                open_time=now_ms - 86400000,
                close_time=now_ms,
                first_id=1,
                last_id=100,
                count=100,
            ),
        ]
    )

    # Seed historical klines for 1m (with confirmed 5L/5R swing low @ 98.0) and 15m
    btc_1m_raw = _build_1m_klines_with_swing_low(
        "BTCUSDT", base_ts=base_15m_close - 29 * 60000, count=30, swing_index=20, swing_low_price=98.0
    )
    eth_1m_raw = _build_1m_klines_with_swing_low(
        "ETHUSDT", base_ts=base_15m_close - 29 * 60000, count=30, swing_index=20, swing_low_price=98.0
    )
    seed_15m_raw = _build_bearish_15m_klines(base_ts=base_15m_close - 899_999, count=5)

    async def mock_get_klines(symbol: str, interval: str, limit: int = 100):
      if interval == "1m":
        return btc_1m_raw if symbol == "BTCUSDT" else eth_1m_raw
      return seed_15m_raw

    client.get_klines = AsyncMock(side_effect=mock_get_klines)
    client.get_symbol_filters = AsyncMock(return_value=btc_filters)
    client.get_account_info = AsyncMock(
        return_value=AccountInfo(
            fee_tier=0,
            can_trade=True,
            can_deposit=True,
            can_withdraw=True,
            total_margin_balance=1000.0,
            total_wallet_balance=1000.0,
            total_unrealized_profit=0.0,
            available_balance=1000.0,
        )
    )
    client.get_leverage_brackets = AsyncMock(
        return_value={
            "BTCUSDT": SymbolLeverageBrackets(
                symbol="BTCUSDT",
                brackets=[
                    LeverageBracketTier(
                        bracket=1,
                        initial_leverage=20,
                        notional_cap=50000.0,
                        notional_floor=0.0,
                        maint_margin_ratio=0.005,
                    )
                ],
            )
        }
    )
    client.change_margin_type = AsyncMock(return_value={"code": 200})
    client.change_leverage = AsyncMock(return_value={"leverage": 20})

    # Track placed orders dynamically so query_order & cancel_order behave realistically
    submitted_orders = {}

    async def mock_place_order(params: dict):
      cid = params["newClientOrderId"]
      otype = params["type"]
      qty = float(params["quantity"])
      oid = len(submitted_orders) + 1000
      if otype == "MARKET":
        resp = {
            "orderId": oid,
            "clientOrderId": cid,
            "symbol": params["symbol"],
            "status": "FILLED",
            "side": params["side"],
            "type": "MARKET",
            "origQty": str(qty),
            "executedQty": str(qty),
            "avgPrice": "100.0",
            "cumQuote": str(qty * 100.0),
            "updateTime": now_ms + 60000,
        }
      else:
        resp = {
            "orderId": oid,
            "clientOrderId": cid,
            "symbol": params["symbol"],
            "status": "NEW",
            "side": params["side"],
            "type": otype,
            "origQty": str(qty),
            "stopPrice": str(params["stopPrice"]),
            "reduceOnly": True,
            "updateTime": now_ms + 60000,
        }
      submitted_orders[cid] = resp
      return resp

    async def mock_query_order(symbol: str, client_order_id: str = None, order_id: int = None):
      return submitted_orders.get(client_order_id, {"status": "NEW"})

    async def mock_cancel_order(symbol: str, client_order_id: str = None, order_id: int = None):
      if client_order_id in submitted_orders:
        submitted_orders[client_order_id]["status"] = "CANCELED"
      return {"status": "CANCELED", "clientOrderId": client_order_id}

    client.place_order = AsyncMock(side_effect=mock_place_order)
    client.query_order = AsyncMock(side_effect=mock_query_order)
    client.cancel_order = AsyncMock(side_effect=mock_cancel_order)

    # 1. Start Orchestrator (skipping live network connect/WS loops for deterministic E2E)
    orch = TradingBotOrchestrator(
        settings=settings,
        client=client,
        batch_debounce_seconds=10.0,  # Explicit deterministic flushes
    )
    report = await orch.startup(verify_connectivity=False, start_streams=False)
    assert report.status == ReconciliationStatus.CLEAN
    assert not orch.position_manager.has_open_position()

    # 2. Simulate strongly Bullish 15m Closed Candle for BOTH BTCUSDT and ETHUSDT
    for sym in ("BTCUSDT", "ETHUSDT"):
      c15 = Candle(
          symbol=sym,
          interval="15m",
          open_time=base_15m_close - 899_999,
          close_time=base_15m_close,
          open=100.0,
          high=106.0,
          low=99.8,
          close=105.0,
          volume=1000.0,
          quote_volume=100000.0,
          trades_count=100,
          is_closed=True,
      )
      await orch.on_candle_closed(
          CandleCloseEvent(symbol=sym, interval="15m", candle=c15, emitted_at=base_15m_close)
      )

    # Flush 15m boundary -> creates shared confirmation window
    await orch.flush_15m_close(base_15m_close)
    win = orch.strategy_engine.active_window
    assert win is not None
    assert win.is_active
    assert "BTCUSDT" in win.candidates
    assert "ETHUSDT" in win.candidates

    # 3. Simulate simultaneous Bullish 1m Closed Candles on first bar inside window
    t1m_open = base_15m_close + 1
    t1m_close = t1m_open + 59_999
    client.get_synchronized_time.return_value = t1m_close

    # Feed ETHUSDT (Rank 2) FIRST, then BTCUSDT (Rank 1) SECOND!
    # Deterministic arbitration MUST still choose BTCUSDT because Rank 1 < Rank 2.
    for sym in ("ETHUSDT", "BTCUSDT"):
      c1 = Candle(
          symbol=sym,
          interval="1m",
          open_time=t1m_open,
          close_time=t1m_close,
          open=100.0,
          high=101.5,
          low=99.9,
          close=101.0,
          volume=100.0,
          quote_volume=10000.0,
          trades_count=20,
          is_closed=True,
      )
      orch.market_data.get_buffer(sym, "1m").add_candle(c1)
      await orch.on_candle_closed(
          CandleCloseEvent(symbol=sym, interval="1m", candle=c1, emitted_at=t1m_close)
      )

    # Flush 1m batch -> Arbitrates winner -> Swing -> Risk -> Order Entry -> SL/TP
    winning_signal = await orch.flush_1m_close(t1m_close)
    assert winning_signal is not None
    assert winning_signal.symbol == "BTCUSDT"
    assert winning_signal.candidate_rank == 1

    # Verify global position is now OPEN with SL and TP active
    assert orch.position_manager.has_open_position()
    pos = orch.position_manager.get_current_position()
    assert pos is not None
    assert pos.symbol == "BTCUSDT"
    assert pos.status == PositionState.OPEN
    assert pos.protective_pair is not None
    assert pos.quantity > 0

    tp_cid = pos.protective_pair.tp_client_order_id
    sl_cid = pos.protective_pair.sl_client_order_id
    tp_price = pos.protective_pair.tp_price

    # 4. Simulate Take-Profit Fill event via User Data Stream
    client.get_positions.return_value = [
        PositionRisk(
            symbol="BTCUSDT",
            position_amt=0.0,
            entry_price=0.0,
            mark_price=tp_price,
            unrealized_profit=0.0,
            liquidation_price=0.0,
            leverage=20,
            margin_type="isolated",
            position_side="BOTH",
        )
    ]

    await orch.user_stream.process_event({
        "e": "ORDER_TRADE_UPDATE",
        "o": {
            "s": "BTCUSDT",
            "c": tp_cid,
            "i": pos.protective_pair.tp_order_id,
            "X": "FILLED",
            "ap": str(tp_price),
            "z": str(pos.quantity),
        },
    })

    # Verify SL sibling was cancelled, position closed in DB, and lock released!
    assert submitted_orders[sl_cid]["status"] == "CANCELED"
    assert not orch.position_manager.has_open_position()
    assert orch.position_manager.get_current_position() is None

    db_pos = orch.repository.get_position(pos.position_id)
    assert db_pos is not None
    assert db_pos.status == "CLOSED"
    assert db_pos.exit_reason == "TAKE_PROFIT"
    assert db_pos.realized_pnl > 0

    await orch.shutdown()

  @pytest.mark.asyncio
  async def test_risk_rejection_consumes_window_no_secondary_candidate(
      self, tmp_path: Path
  ):
    base_15m_close = 1_700_000_000_000 + 899_999
    settings = Settings(
        app_env="testnet",
        db_file_path=tmp_path / "e2e_reject.db",
    )

    client = MagicMock(spec=BinanceFuturesClient)
    client.get_synchronized_time = MagicMock(return_value=base_15m_close + 100)
    client.synchronize_time = AsyncMock(return_value=0)
    client.close = AsyncMock()
    client.get_positions = AsyncMock(return_value=[])
    client.get_open_orders = AsyncMock(return_value=[])

    filters = SymbolFilter(
        symbol="BTCUSDT",
        tick_size=0.01,
        step_size=0.001,
        min_qty=0.001,
        max_qty=1000.0,
        min_notional=5.0,
        price_precision=2,
        quantity_precision=3,
    )
    client.get_exchange_info = AsyncMock(
        return_value={
            "BTCUSDT": SymbolInfo("BTCUSDT", "TRADING", "BTC", "USDT", "PERPETUAL", filters),
            "ETHUSDT": SymbolInfo("ETHUSDT", "TRADING", "ETH", "USDT", "PERPETUAL", filters),
        }
    )
    client.get_24hr_tickers = AsyncMock(
        return_value=[
            Ticker24hr("BTCUSDT", 10.0, 10.0, 100.0, 100.0, 1.0, 90.0, 102.0, 89.0, 1e5, 1e7, 0, base_15m_close, 1, 2, 2),
            Ticker24hr("ETHUSDT", 8.0, 8.0, 100.0, 100.0, 1.0, 92.0, 101.0, 91.0, 1e5, 8e6, 0, base_15m_close, 1, 2, 2),
        ]
    )
    # Deliberately return NO swing pivots (only 3 bars, less than 11 needed for 5L/5R)
    client.get_klines = AsyncMock(return_value=[])

    orch = TradingBotOrchestrator(
        settings=settings, client=client, batch_debounce_seconds=10.0
    )
    await orch.startup(verify_connectivity=False, start_streams=False)

    # Trigger 15m bullish candles (asymmetric so initial HA_Close > HA_Open)
    for sym in ("BTCUSDT", "ETHUSDT"):
      c15 = Candle(sym, "15m", base_15m_close - 899999, base_15m_close, 100.0, 106.0, 99.8, 105.0, 100.0, 10000.0, 10, True)
      await orch.on_candle_closed(CandleCloseEvent(sym, "15m", c15, base_15m_close))

    await orch.flush_15m_close(base_15m_close)

    # Bar 1: BTCUSDT (Rank 1) confirms -> fails swing validation -> MUST CONSUME WINDOW!
    t1_open = base_15m_close + 1
    t1_close = t1_open + 59999
    c1_btc = Candle("BTCUSDT", "1m", t1_open, t1_close, 100.0, 103.0, 99.9, 102.5, 10.0, 1000.0, 5, True)
    orch.market_data.get_buffer("BTCUSDT", "1m").add_candle(c1_btc)
    await orch.on_candle_closed(CandleCloseEvent("BTCUSDT", "1m", c1_btc, t1_close))
    sig1 = await orch.flush_1m_close(t1_close)
    assert sig1 is not None
    assert not orch.position_manager.has_open_position()
    assert orch.strategy_engine.active_window.window_consumed

    # Bar 2: ETHUSDT confirms later in same window -> MUST BE IGNORED
    t2_open = t1_close + 1
    t2_close = t2_open + 59999
    c1_eth = Candle("ETHUSDT", "1m", t2_open, t2_close, 100.0, 103.0, 99.9, 102.5, 10.0, 1000.0, 5, True)
    orch.market_data.get_buffer("ETHUSDT", "1m").add_candle(c1_eth)
    await orch.on_candle_closed(CandleCloseEvent("ETHUSDT", "1m", c1_eth, t2_close))
    sig2 = await orch.flush_1m_close(t2_close)
    assert sig2 is None
    assert not orch.position_manager.has_open_position()

    await orch.shutdown()
