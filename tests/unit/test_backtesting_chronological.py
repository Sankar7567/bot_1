"""Mandatory Validation Tests for Phase 13 — Chronological Backtesting (Amendment 01 §16).

Verifies all 17 mandatory non-lookahead, rolling 24h, timing, execution, accounting,
data quality, and determinism invariants.
"""

from dataclasses import replace
from typing import List, Optional
import pytest
from src.backtesting.clock import SimulatedClock
from src.backtesting.data_adapter import HistoricalMarketStore
from src.backtesting.exec_adapter import HistoricalExchangeAdapter
from src.backtesting.ledger import AccountLedger
from src.backtesting.models import (
    BacktestConfig,
    ContractMetadata,
    ExecutionAssumptions,
)
from src.backtesting.runner import ChronologicalBacktestRunner
from src.config.settings import Settings
from src.exchange.models import (
    LeverageBracketTier,
    SymbolFilter,
    SymbolLeverageBrackets,
)
from src.indicators.swing_points import SwingDetector
from src.market_data.models import Candle
from src.scanner.scanner import MarketScanner


def _make_contract(
    symbol: str,
    listed_at_ms: int = 0,
    delisted_at_ms: Optional[int] = None,
) -> ContractMetadata:
  return ContractMetadata(
      symbol=symbol,
      status="TRADING",
      base_asset=symbol.replace("USDT", ""),
      quote_asset="USDT",
      contract_type="PERPETUAL",
      filters=SymbolFilter(
          symbol=symbol,
          tick_size=0.01,
          step_size=0.001,
          min_qty=0.001,
          max_qty=10000.0,
          min_notional=5.0,
          price_precision=2,
          quantity_precision=3,
      ),
      brackets=SymbolLeverageBrackets(
          symbol=symbol,
          brackets=[
              LeverageBracketTier(
                  bracket=1,
                  initial_leverage=3,  # Low leverage so liq_price stays well outside SL
                  notional_cap=100_000.0,
                  notional_floor=0.0,
                  maint_margin_ratio=0.005,
              )
          ],
      ),
      listed_at_ms=listed_at_ms,
      delisted_at_ms=delisted_at_ms,
  )


def _generate_1m_series(
    symbol: str,
    start_open_ms: int,
    count: int,
    prices: Optional[List[float]] = None,
    base_price: float = 100.0,
    quote_vol_per_bar: float = 10_000.0,
) -> List[Candle]:
  candles = []
  for i in range(count):
    op = start_open_ms + i * 60_000
    cl = op + 59_999
    px = prices[i] if prices is not None else base_price
    candles.append(
        Candle(
            symbol=symbol,
            interval="1m",
            open_time=op,
            close_time=cl,
            open=px,
            high=px * 1.002,
            low=px * 0.998,
            close=px,
            volume=quote_vol_per_bar / px,
            quote_volume=quote_vol_per_bar,
            trades_count=10,
            is_closed=True,
        )
    )
  return candles


def _aggregate_15m(candles_1m: List[Candle], symbol: str) -> List[Candle]:
  """Aggregates aligned 15-bar chunks of 1m candles into 15m OHLC candles."""
  res = []
  for i in range(0, len(candles_1m) - 14, 15):
    chunk = candles_1m[i : i + 15]
    res.append(
        Candle(
            symbol=symbol,
            interval="15m",
            open_time=chunk[0].open_time,
            close_time=chunk[-1].close_time,
            open=chunk[0].open,
            high=max(c.high for c in chunk),
            low=min(c.low for c in chunk),
            close=chunk[-1].close,
            volume=sum(c.volume for c in chunk),
            quote_volume=sum(c.quote_volume for c in chunk),
            trades_count=sum(c.trades_count for c in chunk),
            is_closed=True,
        )
    )
  return res


class TestChronologicalRankingAndUniverse:
  """Tests 1–5: Historical market reconstruction, rolling 24h ranking, and timing."""

  @pytest.mark.asyncio
  async def test_1_and_2_day3_or_post_signal_surge_does_not_leak_into_day1_ranking(
      self,
  ):
    # Aligned to 00:00:00 UTC Day 0
    # 3 days of 1m bars = 4320 bars total
    # COIN_A: starts at 100, linearly ramps to 105 over Day 1 (+5% rolling-24h on Day 1 close),
    #          then holds at 105 for Day 2 and Day 3 (0% rolling-24h gain from Day2 onwards)
    # COIN_B: flat at 100 for Days 0+1+2 (0% rolling-24h gain on Day 1 close),
    #          then abruptly jumps to 300 at the start of Day 3 (+200% rolling-24h on Day 3 close)
    # Day 0 start (also warm-up start):
    day0 = 1_700_006_400_000
    # Build price arrays  — bar index i corresponds to bar opening at day0 + i*60_000
    # Day 1 ends at bar index 1440 (open_time = day0 + 1440*60_000, close_time = day0 + 1440*60_000 + 59_999)
    # We evaluate Day-1 ranking at the close_time of bar 1439 (last bar of Day 1):
    #   day1_eval_ts = day0 + 1439 * 60_000 + 59_999
    # Rolling 24h window at that ts = [day0 + (1439-1440)*60_000 .. day1_eval_ts]
    #   but bar 0 starts at day0 so the base price is prices_a[0]=100.0
    prices_a = [100.0 + (i * 5.0 / 1440.0) for i in range(1440)]  # 100 -> 105 during Day 1
    prices_a += [105.0] * 2880  # flat for Days 2+3
    prices_b = [100.0] * 2881   # flat at 100 through bar 2880 (24h lookback base on Day 3)
    prices_b += [300.0] * 1439  # +200% surge on Day 3

    store = HistoricalMarketStore()
    store.register_contract(_make_contract("COINAUSDT"))
    store.register_contract(_make_contract("COINBUSDT"))
    store.ingest_candles("COINAUSDT", "1m", _generate_1m_series("COINAUSDT", day0, 4320, prices_a))
    store.ingest_candles("COINBUSDT", "1m", _generate_1m_series("COINBUSDT", day0, 4320, prices_b))

    # Evaluate at end of Day 1: close_time of bar index 1439
    day1_eval_ts = day0 + 1439 * 60_000 + 59_999
    clock = SimulatedClock(day1_eval_ts)
    adapter = HistoricalExchangeAdapter(clock, store, AccountLedger())
    scanner = MarketScanner(client=adapter, top_n=1)

    scan_day1 = await scanner.scan()
    assert scan_day1.symbols[0].symbol == "COINAUSDT", (
        f"Expected COINAUSDT to rank #1 on Day1 (rolling +5%), got {scan_day1.symbols[0].symbol}"
    )

    # Evaluate at end of Day 3: close_time of bar index 4319 (after COIN_B surged to 300)
    day3_eval_ts = day0 + 4319 * 60_000 + 59_999
    clock.advance_to(day3_eval_ts)
    scan_day3 = await scanner.scan()
    assert scan_day3.symbols[0].symbol == "COINBUSDT", (
        f"Expected COINBUSDT to rank #1 on Day3 (rolling +200%), got {scan_day3.symbols[0].symbol}"
    )

  @pytest.mark.asyncio
  async def test_4_rolling_24h_differs_from_utc_daily_gain(self):
    # Layout (all times UTC, bar index = minutes since day0):
    #   Day 0 00:00 .. Day 0 12:00  → 720 bars  (warmup prefix)
    #   Day 0 12:00 .. Day 1 12:00  → 1440 bars  (the rolling-24h evaluation window)
    #   Evaluation timestamp = close_time of bar 2159 = day0 + 2159*60_000 + 59_999
    #
    # COIN_X journey (bar indices 0..2159, prices_x):
    #   bars 0..719  (00:00–12:00 Day0):  flat at 120            (warmup)
    #   bars 720..1439 (12:00 Day0–00:00 Day1): drops 120 → 90   (Δ = -30 over 720 bars)
    #   bars 1440..2159 (00:00–12:00 Day1): rises 90 → 100       (Δ = +10 over 720 bars)
    #
    #   Rolling-24h base = price at bar 720 open = 120.0
    #   Rolling-24h end  = price at bar 2159 close = 100.0
    #   Rolling-24h gain = (100 - 120) / 120 * 100 = -16.67%  (NEGATIVE)
    #   UTC-daily gain   = (100 - 90) / 90 * 100 = +11.11%    (POSITIVE)
    #
    # COIN_Y journey (bar indices 0..2159, prices_y):
    #   bars 0..719  (warmup):  flat at 100.0
    #   bars 720..1439:          flat at 100.0
    #   bars 1440..2159:         linearly ramps 100 → 105  (Δ = +5 over 720 bars)
    #
    #   Rolling-24h base = price at bar 720 open = 100.0
    #   Rolling-24h end  = price at bar 2159 close ≈ 105.0
    #   Rolling-24h gain ≈ +5.0%  ✓
    #   UTC-daily gain   = (105 - 100) / 100 * 100 = +5.0%  (same, but we care about rolling)

    utc_00_day0 = 1_700_006_400_000
    total_bars = 2160  # 36 h

    prices_x = []
    prices_y = []
    for i in range(total_bars):
      if i < 720:                # warmup: flat
        prices_x.append(120.0)
        prices_y.append(100.0)
      elif i < 1440:             # drop zone for X, flat for Y
        prices_x.append(120.0 - (i - 720) * (30.0 / 720.0))  # 120 → 90
        prices_y.append(100.0)
      else:                      # recovery for X, ramp for Y
        prices_x.append(90.0 + (i - 1440) * (10.0 / 720.0))   # 90 → 100
        prices_y.append(100.0 + (i - 1440) * (5.0 / 720.0))   # 100 → 105

    store = HistoricalMarketStore()
    store.register_contract(_make_contract("COINXUSDT"))
    store.register_contract(_make_contract("COINYUSDT"))
    store.ingest_candles("COINXUSDT", "1m", _generate_1m_series("COINXUSDT", utc_00_day0, total_bars, prices_x))
    store.ingest_candles("COINYUSDT", "1m", _generate_1m_series("COINYUSDT", utc_00_day0, total_bars, prices_y))

    # Evaluate at close_time of the last bar (bar 2159)
    eval_ts = utc_00_day0 + (total_bars - 1) * 60_000 + 59_999
    clock = SimulatedClock(eval_ts)
    adapter = HistoricalExchangeAdapter(clock, store, AccountLedger())
    scanner = MarketScanner(client=adapter)

    res = await scanner.scan()
    # COIN_Y must rank #1 (rolling +5%), COIN_X must rank #2 (rolling -16.7%)
    assert res.symbols[0].symbol == "COINYUSDT", (
        f"Expected COINYUSDT first, got {res.symbols[0].symbol}"
    )
    assert res.symbols[0].price_change_percent == pytest.approx(5.0, abs=0.5)
    assert res.symbols[1].symbol == "COINXUSDT"
    assert res.symbols[1].price_change_percent < 0.0  # Rolling 24h is negative despite positive UTC day

  @pytest.mark.asyncio
  async def test_5_scanner_updates_with_timestamp_and_excludes_delisted(self):
    t0 = 1_700_006_400_000
    delist_ts = t0 + 1500 * 60_000
    store = HistoricalMarketStore()
    store.register_contract(_make_contract("STAYUSDT"))
    store.register_contract(_make_contract("DELISTUSDT", delisted_at_ms=delist_ts))
    store.ingest_candles("STAYUSDT", "1m", _generate_1m_series("STAYUSDT", t0, 1600, base_price=100.0))
    store.ingest_candles("DELISTUSDT", "1m", _generate_1m_series("DELISTUSDT", t0, 1600, base_price=100.0))

    clock = SimulatedClock(delist_ts - 1)
    adapter = HistoricalExchangeAdapter(clock, store, AccountLedger())
    scanner = MarketScanner(client=adapter)

    r1 = await scanner.scan()
    assert {s.symbol for s in r1.symbols} == {"STAYUSDT", "DELISTUSDT"}

    # Advance past delisting time
    clock.advance_to(delist_ts + 60_000)
    r2 = await scanner.scan()
    assert [s.symbol for s in r2.symbols] == ["STAYUSDT"]


class TestStrategyReplayAndExecution:
  """Tests 3, 6–15, 17: Mid-window freeze, HA monitoring, tie-break, pivot delay, execution, SL/TP, equity, lock, determinism."""

  def _build_full_replay_dataset(
      self,
      *,
      opposite_mid_bars: bool = False,
      simultaneous_confirm: bool = False,
      ambiguous_sl_tp_bar: bool = False,
      second_window_overlap: bool = False,
  ):
    # 25 hours of history (1440 warmup + 60 bars eval)
    t0 = 1_700_006_400_000  # aligned to 15m boundary
    total_bars = 1500  # 1440 warmup (24h) + 60m eval
    eval_start = t0 + 1440 * 60_000

    # Build 1m prices for TOP1USDT (Rank 1: +15% over 24h) and TOP2USDT (Rank 2: +10% over 24h)
    prices_1 = [100.0] * total_bars
    prices_2 = [100.0] * total_bars

    # Ramp during warmup so 24h gain is +15% for TOP1 and +10% for TOP2
    for i in range(1440):
      prices_1[i] = 86.9565 + (13.0435 * i / 1439.0)  # -> 100.0 (+15%)
      prices_2[i] = 90.9090 + (9.0910 * i / 1439.0)   # -> 100.0 (+10%)

    # Form confirmed 5L/5R swing low at bar index 1430 (confirmed at 1435, well before eval_start 1440)
    for k in range(1425, 1436):
      dist = abs(k - 1430)
      prices_1[k] = 95.0 + dist * 0.5
      prices_2[k] = 95.0 + dist * 0.5

    # Eval window 1: bars 1440..1454 (15m candle closes at eval_start + 15*60_000 - 1)
    # Wait: eval_start is open_time of bar 1440; 15m trigger closes at bar 1439 close_time?
    # Let's make bar 1440..1454 the 15m trigger bar closing at eval_start + 15m - 1,
    # and bars 1455..1469 the confirmation window!
    for i in range(1440, 1455):
      # Strongly rising 15m bar so 15m HA is bullish
      prices_1[i] = 100.0 + (i - 1440) * 0.4
      prices_2[i] = 100.0 + (i - 1440) * 0.4

    candles_1 = _generate_1m_series("TOP1USDT", t0, total_bars, prices_1, quote_vol_per_bar=50_000.0)
    candles_2 = _generate_1m_series("TOP2USDT", t0, total_bars, prices_2, quote_vol_per_bar=30_000.0)

    def _set_bar(arr: List[Candle], idx: int, o: float, h: float, l: float, c: float):
      orig = arr[idx]
      arr[idx] = Candle(
          symbol=orig.symbol,
          interval="1m",
          open_time=orig.open_time,
          close_time=orig.close_time,
          open=o,
          high=h,
          low=l,
          close=c,
          volume=orig.volume,
          quote_volume=orig.quote_volume,
          trades_count=orig.trades_count,
          is_closed=True,
      )

    # Ensure swing low at 1430 has strict low=94.0
    _set_bar(candles_1, 1430, 95.0, 96.0, 94.0, 95.5)
    _set_bar(candles_2, 1430, 95.0, 96.0, 94.0, 95.5)

    # Confirmation bar behavior in window [1455..1469]
    if opposite_mid_bars:
      # Bars 1455..1458 are bearish (opposite), then bar 1459 is strongly bullish!
      for b in range(1455, 1459):
        _set_bar(candles_1, b, 105.0, 105.2, 103.5, 103.6)
      _set_bar(candles_1, 1459, 103.6, 112.0, 103.5, 111.0)
      fill_bar_idx = 1460
    elif simultaneous_confirm:
      # Both TOP1 and TOP2 confirm bullish at exact bar 1455
      _set_bar(candles_1, 1455, 105.0, 110.0, 104.8, 109.5)
      _set_bar(candles_2, 1455, 105.0, 110.0, 104.8, 109.5)
      fill_bar_idx = 1456
    else:
      _set_bar(candles_1, 1455, 105.0, 110.0, 104.8, 109.5)
      fill_bar_idx = 1456

    # Entry fills at fill_bar_idx.open (~100-105), SL ~93.9, TP ~138+
    # Simulate exit after entry
    if ambiguous_sl_tp_bar:
      # Single bar touches BOTH SL (<= 90) and TP (>= 160)
      _set_bar(candles_1, fill_bar_idx + 2, 105.0, 170.0, 80.0, 120.0)
    elif second_window_overlap:
      # Position stays open across the next 15m window (bars 1470..1484) and only hits TP at bar 1490
      for b in range(fill_bar_idx, 1490):
        _set_bar(candles_1, b, 105.0, 110.0, 102.0, 106.0)
        _set_bar(candles_2, b, 105.0, 110.0, 102.0, 106.0)
      _set_bar(candles_1, 1490, 110.0, 180.0, 109.0, 175.0)
    else:
      # Hit TP cleanly 3 bars after entry
      _set_bar(candles_1, fill_bar_idx, 106.0, 108.0, 105.0, 107.0)
      _set_bar(candles_1, fill_bar_idx + 1, 107.0, 115.0, 106.5, 114.0)
      _set_bar(candles_1, fill_bar_idx + 2, 114.0, 180.0, 113.0, 175.0)

    store = HistoricalMarketStore()
    store.register_contract(_make_contract("TOP1USDT"))
    store.register_contract(_make_contract("TOP2USDT"))
    store.ingest_candles("TOP1USDT", "1m", candles_1)
    store.ingest_candles("TOP2USDT", "1m", candles_2)
    store.ingest_candles("TOP1USDT", "15m", _aggregate_15m(candles_1, "TOP1USDT"))
    store.ingest_candles("TOP2USDT", "15m", _aggregate_15m(candles_2, "TOP2USDT"))

    cfg = BacktestConfig(
        start_time_ms=eval_start,
        end_time_ms=t0 + total_bars * 60_000,
        initial_balance=1000.0,
        execution=ExecutionAssumptions(
            signal_to_order_latency_ms=200,
            half_spread_bps=1.0,
            entry_slippage_bps=5.0,
            stop_slippage_bps=10.0,
            taker_fee_rate=0.0005,
        ),
    )
    return store, cfg

  @pytest.mark.asyncio
  async def test_6_and_7_opposite_1m_candles_do_not_invalidate_candidate(self):
    store, cfg = self._build_full_replay_dataset(opposite_mid_bars=True)
    runner = ChronologicalBacktestRunner(store=store, config=cfg)
    report = await runner.run()

    assert report.performance.total_trades == 1
    trade = report.trades[0]
    assert trade.symbol == "TOP1USDT"
    assert trade.monitored_1m_bars_count >= 5  # Confirmed on 5th bar despite 4 bearish bars

  @pytest.mark.asyncio
  async def test_8_simultaneous_confirmation_uses_original_snapshot_rank(self):
    store, cfg = self._build_full_replay_dataset(simultaneous_confirm=True)
    runner = ChronologicalBacktestRunner(store=store, config=cfg)
    report = await runner.run()

    assert report.strategy.tie_breaking_events >= 1
    assert report.performance.total_trades == 1
    assert report.trades[0].symbol == "TOP1USDT"
    assert report.trades[0].candidate_rank == 1
    assert "TOP1USDT" in report.trades[0].tie_break_candidates
    assert "TOP2USDT" in report.trades[0].tie_break_candidates

  @pytest.mark.asyncio
  async def test_9_and_10_pivot_unavailable_before_5_right_bars(self):
    t0 = 1_700_000_000_000
    # 11 bars: indices 0..10. Pivot low at index 5 (requires bars 6..10 to confirm).
    prices = [100.0] * 11
    for i in range(11):
      prices[i] = 95.0 + abs(i - 5) * 0.5
    candles = _generate_1m_series("TESTUSDT", t0, 11, prices)

    det = SwingDetector("TESTUSDT", "1m", left_bars=5, right_bars=5)
    # With only bars 0..9 (only 4 right-side bars), pivot at index 5 MUST NOT be available!
    assert det.get_latest_swing_low(candles[:10]) is None
    # Once bar 10 (5th right-side bar) completes, pivot becomes available!
    piv = det.get_latest_swing_low(candles)
    assert piv is not None
    assert piv.bar_index == 5
    assert piv.confirmation_timestamp == candles[10].close_time

  @pytest.mark.asyncio
  async def test_11_entry_executes_after_signal_and_12_14_equity_updates(self):
    store, cfg = self._build_full_replay_dataset()
    # End before the 2nd 15m window so no unclosed 2nd position deducts entry fees after trade 1 exits
    cfg = replace(cfg, end_time_ms=cfg.start_time_ms + 25 * 60_000)
    runner = ChronologicalBacktestRunner(store=store, config=cfg)
    report = await runner.run()

    assert report.performance.total_trades == 1
    t = report.trades[0]
    # Entry MUST be strictly after signal confirmation timestamp (next bar + latency)
    assert t.order_submitted_at > t.confirmation_1m_timestamp
    assert t.fill_timestamp > t.confirmation_1m_timestamp
    assert t.exit_reason == "TAKE_PROFIT"
    assert report.equity.ending_balance > report.equity.starting_balance
    assert t.equity_after_trade == pytest.approx(report.equity.ending_balance)
    assert len(report.equity.realized_equity_curve) == 1

  @pytest.mark.asyncio
  async def test_13_ambiguous_sl_tp_bar_uses_conservative_sl_first_rule(self):
    store, cfg = self._build_full_replay_dataset(ambiguous_sl_tp_bar=True)
    runner = ChronologicalBacktestRunner(store=store, config=cfg)
    report = await runner.run()

    assert report.performance.total_trades == 1
    t = report.trades[0]
    assert t.exit_reason == "STOP_LOSS"
    assert "CONSERVATIVE_SL_FIRST" in t.ambiguity_resolution_note
    assert t.net_pnl < 0

  @pytest.mark.asyncio
  async def test_15_single_open_position_prevents_overlapping_entry(self):
    store, cfg = self._build_full_replay_dataset(second_window_overlap=True)
    runner = ChronologicalBacktestRunner(store=store, config=cfg)
    report = await runner.run()

    # Only 1 trade executed despite 2nd 15m window occurring while position was open
    assert report.performance.total_trades == 1

  @pytest.mark.asyncio
  async def test_3_mid_window_top20_entrant_ignored_until_next_window(self):
    # COIN2 is listed at mid_window_list_ts (20 minutes into the eval period).
    # The first 15m trigger window closes at eval_start + 15m - 1.
    # At that moment, COIN2 is NOT yet listed, so it must NOT be in the frozen snapshot
    # and must NOT create any candidates during the first confirmation window.
    # We end the backtest BEFORE the second 15m trigger so we can assert globally
    # that COIN2 never created any candidates.
    t0 = 1_700_006_400_000
    total_bars = 1465  # Enough for warmup (1440) + first 15m trigger + ~10m of confirmation
    eval_start = t0 + 1440 * 60_000
    # COIN2 listed 20 minutes into the eval period — after the first 15m trigger fires
    mid_window_list_ts = eval_start + 20 * 60_000

    prices_1 = [100.0 + (i * 0.01) for i in range(total_bars)]
    prices_2 = [100.0 + (i * 0.05) for i in range(total_bars)]

    store = HistoricalMarketStore()
    store.register_contract(_make_contract("COIN1USDT"))
    store.register_contract(_make_contract("COIN2USDT", listed_at_ms=mid_window_list_ts))
    c1 = _generate_1m_series("COIN1USDT", t0, total_bars, prices_1)
    c2 = _generate_1m_series("COIN2USDT", t0, total_bars, prices_2)
    store.ingest_candles("COIN1USDT", "1m", c1)
    store.ingest_candles("COIN2USDT", "1m", c2)
    store.ingest_candles("COIN1USDT", "15m", _aggregate_15m(c1, "COIN1USDT"))
    store.ingest_candles("COIN2USDT", "15m", _aggregate_15m(c2, "COIN2USDT"))

    # End before the second 15m trigger so COIN2 never appears in any top-20 snapshot
    end_time = t0 + (eval_start - t0) + 29 * 60_000  # = eval_start + 29m (before 2nd 15m trigger)
    cfg = BacktestConfig(start_time_ms=eval_start, end_time_ms=end_time)
    runner = ChronologicalBacktestRunner(store=store, config=cfg)
    report = await runner.run()

    # COIN2 entered mid-window (after the 15m snapshot was frozen) so it must NEVER
    # have been created as a candidate within this backtest period.
    assert "COIN2USDT" not in report.market_selection.candidate_counts_by_symbol, (
        f"COIN2USDT must not be a candidate; got {report.market_selection.candidate_counts_by_symbol}"
    )

  @pytest.mark.asyncio
  async def test_17_deterministic_reproducibility(self):
    store1, cfg1 = self._build_full_replay_dataset()
    store2, cfg2 = self._build_full_replay_dataset()

    r1 = await ChronologicalBacktestRunner(store1, cfg1).run()
    r2 = await ChronologicalBacktestRunner(store2, cfg2).run()

    assert r1.performance.total_trades == r2.performance.total_trades
    assert r1.equity.ending_balance == r2.equity.ending_balance
    assert r1.trades[0].fill_price == r2.trades[0].fill_price
    assert r1.trades[0].net_pnl == r2.trades[0].net_pnl


class TestHistoricalDataQuality:
  """Test 16: Missing, duplicate, out-of-order, and invalid OHLC detection."""

  def test_16_detects_and_reports_data_anomalies_without_interpolation(self):
    t0 = 1_700_000_000_000
    store = HistoricalMarketStore()
    store.register_contract(_make_contract("BADUSDT"))

    valid_bar = Candle("BADUSDT", "1m", t0, t0 + 59999, 100.0, 101.0, 99.0, 100.5, 10.0, 1000.0, 5, True)
    dup_bar = Candle("BADUSDT", "1m", t0, t0 + 59999, 100.0, 101.0, 99.0, 100.5, 10.0, 1000.0, 5, True)
    ooo_bar = Candle("BADUSDT", "1m", t0 - 60000, t0 - 1, 100.0, 101.0, 99.0, 100.5, 10.0, 1000.0, 5, True)
    bad_ohlc = Candle("BADUSDT", "1m", t0 + 60000, t0 + 119999, 100.0, 98.0, 99.0, 100.5, 10.0, 1000.0, 5, True)
    gap_zero_vol = Candle("BADUSDT", "1m", t0 + 240000, t0 + 299999, 100.0, 101.0, 99.0, 100.5, 0.0, 0.0, 0, True)

    store.ingest_candles("BADUSDT", "1m", [valid_bar, dup_bar, ooo_bar, bad_ohlc, gap_zero_vol])

    issue_types = {i.issue_type for i in store.quality_report.issues}
    assert "DUPLICATE_CANDLE" in issue_types
    assert "OUT_OF_ORDER" in issue_types
    assert "INVALID_OHLC" in issue_types
    assert "MISSING_VOLUME" in issue_types
    assert "MISSING_CANDLE" in issue_types

    # Only 2 valid bars stored (no silent interpolation for the missing bar!)
    stored = store.get_klines_up_to("BADUSDT", "1m", t0 + 300000)
    assert len(stored) == 2
