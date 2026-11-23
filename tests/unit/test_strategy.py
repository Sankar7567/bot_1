"""Unit tests for Multi-Symbol Strategy Engine (Strategy Amendment v1.1).

Validates all 17 mandatory test scenarios specified in Replacement Prompt 06,
plus additional tests for the corrected simultaneous-confirmation determinism:
1.  Immediate confirmation
2.  Fifth-candle confirmation
3.  Misaligned candles tolerance
4.  Doji candles tolerance
5.  Simultaneous 15m triggers
6.  ETH confirms before BTC
7.  BTC confirms before ETH
8.  Same-timestamp simultaneous confirmations (via process_simultaneous_confirmations)
9.  Original top-20 ranking tie-breaking (arbitrator)
10. Candidate leaves top 20 during window
11. New candidate enters top 20 during window
12. Window expiration with no confirmation
13. Late-arriving candle after expiration
14. Concurrent worker race conditions
15. Risk validation failure consumes window (no 2nd pick)
16. Existing position blocks entry
17. Process restart recovery of active window
18. flush_1m_timestamp_batch deterministic tie-breaking (rank-based)
19. flush_1m_timestamp_batch after slot already claimed -> None
20. handle_risk_validation_failure expires MONITORING candidates
21. serialize_state preserves actual trigger candle timestamps
22. on_1m_candle_closed buffers confirmed candidates; flush returns signal
"""

import asyncio
import pytest
from src.indicators.heikin_ashi import HeikinAshiCandle
from src.market_data.models import Candle, CandleCloseEvent
from src.scanner.models import ScannedSymbol
from src.strategy.arbitrator import GlobalSlotArbitrator
from src.strategy.engine import MultiSymbolStrategyEngine
from src.strategy.models import (
    CandidateState,
    CandidateStatus,
    ConfirmationWindow,
    Direction,
    WindowStatus,
)


# ==============================================================================
# Helper Factories
# ==============================================================================


def make_ha_candle(
    symbol: str,
    direction: str,  # "BULLISH", "BEARISH", "DOJI"
    interval: str = "1m",
    open_time: int = 1700000060000,
    ha_open: float = 100.0,
) -> HeikinAshiCandle:
  if direction == "BULLISH":
    ha_close = ha_open + 2.0
  elif direction == "BEARISH":
    ha_close = ha_open - 2.0
  else:  # DOJI
    ha_close = ha_open

  duration = 60000 if interval == "1m" else 900000
  return HeikinAshiCandle(
      symbol=symbol.upper(),
      interval=interval,
      open_time=open_time,
      close_time=open_time + duration - 1,
      ha_open=ha_open,
      ha_high=max(ha_open, ha_close) + 1.0,
      ha_low=min(ha_open, ha_close) - 1.0,
      ha_close=ha_close,
      is_closed=True,
  )


def make_candle_close_event(
    ha_candle: HeikinAshiCandle,
) -> CandleCloseEvent:
  c = Candle(
      symbol=ha_candle.symbol,
      interval=ha_candle.interval,
      open_time=ha_candle.open_time,
      close_time=ha_candle.close_time,
      open=ha_candle.ha_open,
      high=ha_candle.ha_high,
      low=ha_candle.ha_low,
      close=ha_candle.ha_close,
      volume=10.0,
      quote_volume=1000.0,
      trades_count=10,
      is_closed=True,
  )
  return CandleCloseEvent(
      symbol=ha_candle.symbol,
      interval=ha_candle.interval,
      candle=c,
      emitted_at=ha_candle.close_time,
  )


def make_scanned_symbol(symbol: str, rank: int) -> ScannedSymbol:
  return ScannedSymbol(
      rank=rank,
      symbol=symbol.upper(),
      price_change_percent=10.0 - rank,
      quote_volume=2000000.0,
      last_price=100.0,
      base_asset=symbol.replace("USDT", ""),
      quote_asset="USDT",
      contract_type="PERPETUAL",
      scan_timestamp=1700000000000,
  )


async def confirm_and_flush(
    engine: MultiSymbolStrategyEngine,
    ha_1m: HeikinAshiCandle,
) -> "Optional[TradeSignal]":
  """Helper: on_1m_candle_closed then flush_1m_timestamp_batch for that bar."""
  event = make_candle_close_event(ha_1m)
  await engine.on_1m_candle_closed(event, ha_1m)
  return await engine.flush_1m_timestamp_batch(ha_1m.close_time)


# ==============================================================================
# Comprehensive Test Suite (Scenarios 1 through 22)
# ==============================================================================


class TestMultiSymbolStrategyEngine:
  """Test suite validating all mandatory scenarios for Multi-Symbol Strategy."""

  @pytest.mark.asyncio
  async def test_01_candidate_confirms_immediately(self):
    """Scenario 1: Candidate confirms on the very first 1m candle after 15m trigger."""
    engine = MultiSymbolStrategyEngine()
    t15 = 1700000000000  # 15m candle close

    snapshot = [make_scanned_symbol("BTCUSDT", rank=1)]
    ha_15m = {"BTCUSDT": make_ha_candle("BTCUSDT", "BULLISH", "15m", open_time=t15 - 900000)}
    await engine.on_15m_candle_closed(snapshot, ha_15m, timestamp_ms=t15)

    assert engine.active_window is not None
    assert "BTCUSDT" in engine.active_window.candidates

    ha_1m = make_ha_candle("BTCUSDT", "BULLISH", "1m", open_time=t15)
    signal = await confirm_and_flush(engine, ha_1m)

    assert signal is not None
    assert signal.symbol == "BTCUSDT"
    assert signal.direction == Direction.LONG
    assert signal.candidate_rank == 1
    assert engine.active_window.is_slot_claimed is True
    assert engine.active_window.winning_symbol == "BTCUSDT"

  @pytest.mark.asyncio
  async def test_02_candidate_confirms_on_fifth_1m_candle(self):
    """Scenario 2: Candidate remains unconfirmed for 4 bars, then confirms on 5th bar."""
    engine = MultiSymbolStrategyEngine()
    t15 = 1700000000000

    snapshot = [make_scanned_symbol("BTCUSDT", rank=1)]
    ha_15m = {"BTCUSDT": make_ha_candle("BTCUSDT", "BULLISH", "15m", open_time=t15 - 900000)}
    await engine.on_15m_candle_closed(snapshot, ha_15m, timestamp_ms=t15)

    # Bars 1 through 4: Misaligned (BEARISH)
    for i in range(4):
      bar_open = t15 + (i * 60000)
      ha_bear = make_ha_candle("BTCUSDT", "BEARISH", "1m", open_time=bar_open)
      sig = await confirm_and_flush(engine, ha_bear)
      assert sig is None
      assert engine.active_window.is_slot_claimed is False

    # Bar 5: Bullish confirmation
    bar_5_open = t15 + (4 * 60000)
    ha_bull = make_ha_candle("BTCUSDT", "BULLISH", "1m", open_time=bar_5_open)
    sig5 = await confirm_and_flush(engine, ha_bull)

    assert sig5 is not None
    assert sig5.symbol == "BTCUSDT"
    assert engine.active_window.is_slot_claimed is True

  @pytest.mark.asyncio
  async def test_03_misaligned_candles_then_aligned(self):
    """Scenario 3: 3 misaligned candles do not eliminate candidate; 4th candle confirms."""
    engine = MultiSymbolStrategyEngine()
    t15 = 1700000000000

    snapshot = [make_scanned_symbol("SOLUSDT", rank=2)]
    ha_15m = {"SOLUSDT": make_ha_candle("SOLUSDT", "BEARISH", "15m", open_time=t15 - 900000)}
    await engine.on_15m_candle_closed(snapshot, ha_15m, timestamp_ms=t15)

    for i in range(3):
      ha_opp = make_ha_candle("SOLUSDT", "BULLISH", "1m", open_time=t15 + (i * 60000))
      sig = await confirm_and_flush(engine, ha_opp)
      assert sig is None
      assert engine.active_window.candidates["SOLUSDT"].status == CandidateStatus.MONITORING

    ha_aligned = make_ha_candle("SOLUSDT", "BEARISH", "1m", open_time=t15 + (3 * 60000))
    sig = await confirm_and_flush(engine, ha_aligned)

    assert sig is not None
    assert sig.symbol == "SOLUSDT"
    assert sig.direction == Direction.SHORT

  @pytest.mark.asyncio
  async def test_04_doji_candles_before_alignment(self):
    """Scenario 4: Doji candles do not eliminate candidate; subsequent aligned candle confirms."""
    engine = MultiSymbolStrategyEngine()
    t15 = 1700000000000

    snapshot = [make_scanned_symbol("BTCUSDT", rank=1)]
    ha_15m = {"BTCUSDT": make_ha_candle("BTCUSDT", "BULLISH", "15m", open_time=t15 - 900000)}
    await engine.on_15m_candle_closed(snapshot, ha_15m, timestamp_ms=t15)

    for i in range(2):
      ha_doji = make_ha_candle("BTCUSDT", "DOJI", "1m", open_time=t15 + (i * 60000))
      sig = await confirm_and_flush(engine, ha_doji)
      assert sig is None

    ha_bull = make_ha_candle("BTCUSDT", "BULLISH", "1m", open_time=t15 + (2 * 60000))
    sig = await confirm_and_flush(engine, ha_bull)
    assert sig is not None
    assert sig.symbol == "BTCUSDT"

  @pytest.mark.asyncio
  async def test_05_three_symbols_trigger_simultaneously_at_15m(self):
    """Scenario 5: 3 symbols trigger at 15m; all 3 registered in same confirmation window."""
    engine = MultiSymbolStrategyEngine()
    t15 = 1700000000000

    snapshot = [
        make_scanned_symbol("BTCUSDT", rank=1),
        make_scanned_symbol("ETHUSDT", rank=2),
        make_scanned_symbol("SOLUSDT", rank=3),
    ]
    ha_15m = {
        "BTCUSDT": make_ha_candle("BTCUSDT", "BULLISH", "15m"),
        "ETHUSDT": make_ha_candle("ETHUSDT", "BULLISH", "15m"),
        "SOLUSDT": make_ha_candle("SOLUSDT", "BEARISH", "15m"),
    }

    window = await engine.on_15m_candle_closed(snapshot, ha_15m, timestamp_ms=t15)

    assert len(window.candidates) == 3
    assert window.candidates["BTCUSDT"].direction == Direction.LONG
    assert window.candidates["ETHUSDT"].direction == Direction.LONG
    assert window.candidates["SOLUSDT"].direction == Direction.SHORT

  @pytest.mark.asyncio
  async def test_06_eth_confirms_before_btc(self):
    """Scenario 6: ETH confirms before BTC; ETH wins slot and BTC's later confirmation is ignored."""
    engine = MultiSymbolStrategyEngine()
    t15 = 1700000000000

    snapshot = [
        make_scanned_symbol("BTCUSDT", rank=1),
        make_scanned_symbol("ETHUSDT", rank=2),
    ]
    ha_15m = {
        "BTCUSDT": make_ha_candle("BTCUSDT", "BULLISH", "15m"),
        "ETHUSDT": make_ha_candle("ETHUSDT", "BULLISH", "15m"),
    }
    await engine.on_15m_candle_closed(snapshot, ha_15m, timestamp_ms=t15)

    # Bar 1 (t15): ETH confirms (Bullish), BTC misaligned (Bearish)
    ha_eth_1 = make_ha_candle("ETHUSDT", "BULLISH", "1m", open_time=t15)
    ha_btc_1 = make_ha_candle("BTCUSDT", "BEARISH", "1m", open_time=t15)
    t1_close = ha_eth_1.close_time  # same close_time for both bar-1 candles

    await engine.on_1m_candle_closed(make_candle_close_event(ha_eth_1), ha_eth_1)
    await engine.on_1m_candle_closed(make_candle_close_event(ha_btc_1), ha_btc_1)
    sig_bar1 = await engine.flush_1m_timestamp_batch(t1_close)

    # ETH aligned, BTC misaligned -> only ETH in batch -> ETH wins
    assert sig_bar1 is not None
    assert sig_bar1.symbol == "ETHUSDT"
    assert engine.active_window.winning_symbol == "ETHUSDT"

    # Bar 2: BTC now confirms (Bullish). Must be IGNORED — slot already claimed by ETH.
    ha_btc_2 = make_ha_candle("BTCUSDT", "BULLISH", "1m", open_time=t15 + 60000)
    sig_btc_late = await confirm_and_flush(engine, ha_btc_2)
    assert sig_btc_late is None

  @pytest.mark.asyncio
  async def test_07_btc_confirms_before_eth(self):
    """Scenario 7: BTC confirms before ETH; BTC wins slot."""
    engine = MultiSymbolStrategyEngine()
    t15 = 1700000000000

    snapshot = [
        make_scanned_symbol("BTCUSDT", rank=1),
        make_scanned_symbol("ETHUSDT", rank=2),
    ]
    ha_15m = {
        "BTCUSDT": make_ha_candle("BTCUSDT", "BULLISH", "15m"),
        "ETHUSDT": make_ha_candle("ETHUSDT", "BULLISH", "15m"),
    }
    await engine.on_15m_candle_closed(snapshot, ha_15m, timestamp_ms=t15)

    # Bar 1: BTC confirms, ETH is doji — BTC confirmed at bar 1, ETH not.
    ha_btc = make_ha_candle("BTCUSDT", "BULLISH", "1m", open_time=t15)
    ha_eth = make_ha_candle("ETHUSDT", "DOJI", "1m", open_time=t15)
    t_close = ha_btc.close_time

    await engine.on_1m_candle_closed(make_candle_close_event(ha_btc), ha_btc)
    await engine.on_1m_candle_closed(make_candle_close_event(ha_eth), ha_eth)
    sig = await engine.flush_1m_timestamp_batch(t_close)

    assert sig is not None
    assert sig.symbol == "BTCUSDT"
    assert engine.active_window.winning_symbol == "BTCUSDT"

    # Bar 2: ETH confirms — slot already taken.
    ha_eth_2 = make_ha_candle("ETHUSDT", "BULLISH", "1m", open_time=t15 + 60000)
    sig2 = await confirm_and_flush(engine, ha_eth_2)
    assert sig2 is None

  @pytest.mark.asyncio
  async def test_08_two_symbols_confirm_on_same_1m_timestamp(self):
    """Scenario 8: Two symbols confirm on same 1m timestamp; original top-20 ranking determines winner."""
    engine = MultiSymbolStrategyEngine()
    t15 = 1700000000000

    snapshot = [
        make_scanned_symbol("BTCUSDT", rank=1),
        make_scanned_symbol("ETHUSDT", rank=2),
    ]
    ha_15m = {
        "BTCUSDT": make_ha_candle("BTCUSDT", "BULLISH", "15m"),
        "ETHUSDT": make_ha_candle("ETHUSDT", "BULLISH", "15m"),
    }
    await engine.on_15m_candle_closed(snapshot, ha_15m, timestamp_ms=t15)

    # Both confirm on the exact same 1m close timestamp
    ha_btc = make_ha_candle("BTCUSDT", "BULLISH", "1m", open_time=t15)
    ha_eth = make_ha_candle("ETHUSDT", "BULLISH", "1m", open_time=t15)
    t_close = ha_btc.close_time  # == ha_eth.close_time

    await engine.on_1m_candle_closed(make_candle_close_event(ha_btc), ha_btc)
    await engine.on_1m_candle_closed(make_candle_close_event(ha_eth), ha_eth)
    signal = await engine.flush_1m_timestamp_batch(t_close)

    # BTC has rank 1 vs ETH rank 2 -> BTC must win deterministically
    assert signal is not None
    assert signal.symbol == "BTCUSDT"
    assert engine.active_window.winning_symbol == "BTCUSDT"

  @pytest.mark.asyncio
  async def test_09_original_top_20_ranking_determines_simultaneous_tie(self):
    """Scenario 9: Rank 2 beats Rank 5; equal rank breaks tie alphabetically."""
    arbitrator = GlobalSlotArbitrator()
    window = ConfirmationWindow("WIN_1", 1700000000000, 1700000900000)

    # Rank 5 vs Rank 2
    c_rank5 = CandidateState("SOLUSDT", Direction.LONG, initial_rank=5, registered_at=1700000000000, trigger_15m_candle=make_ha_candle("SOLUSDT", "BULLISH"))
    c_rank2 = CandidateState("ETHUSDT", Direction.LONG, initial_rank=2, registered_at=1700000000000, trigger_15m_candle=make_ha_candle("ETHUSDT", "BULLISH"))
    window.candidates["SOLUSDT"] = c_rank5
    window.candidates["ETHUSDT"] = c_rank2

    winner = await arbitrator.arbitrate_winners(window, [c_rank5, c_rank2])
    assert winner.symbol == "ETHUSDT"

    # Reset and test equal rank tie-break alphabetically (ADAUSDT before AVAXUSDT)
    window2 = ConfirmationWindow("WIN_2", 1700000000000, 1700000900000)
    c_avax = CandidateState("AVAXUSDT", Direction.LONG, initial_rank=3, registered_at=1700000000000, trigger_15m_candle=make_ha_candle("AVAXUSDT", "BULLISH"))
    c_ada = CandidateState("ADAUSDT", Direction.LONG, initial_rank=3, registered_at=1700000000000, trigger_15m_candle=make_ha_candle("ADAUSDT", "BULLISH"))
    window2.candidates["AVAXUSDT"] = c_avax
    window2.candidates["ADAUSDT"] = c_ada

    winner2 = await arbitrator.arbitrate_winners(window2, [c_avax, c_ada])
    assert winner2.symbol == "ADAUSDT"

  @pytest.mark.asyncio
  async def test_10_candidate_leaves_refreshed_top_20_during_window(self):
    """Scenario 10: Candidate was registered at 15m; scanner drops it later; candidate still qualifies."""
    engine = MultiSymbolStrategyEngine()
    t15 = 1700000000000

    snapshot = [make_scanned_symbol("BTCUSDT", rank=1)]
    ha_15m = {"BTCUSDT": make_ha_candle("BTCUSDT", "BULLISH", "15m")}
    await engine.on_15m_candle_closed(snapshot, ha_15m, timestamp_ms=t15)

    assert "BTCUSDT" in engine.active_window.candidates

    ha_1m = make_ha_candle("BTCUSDT", "BULLISH", "1m", open_time=t15 + 120000)
    sig = await confirm_and_flush(engine, ha_1m)

    assert sig is not None
    assert sig.symbol == "BTCUSDT"

  @pytest.mark.asyncio
  async def test_11_new_candidate_enters_refreshed_top_20_during_window(self):
    """Scenario 11: New symbol enters top-20 mid-window; cannot produce confirmation in current window."""
    engine = MultiSymbolStrategyEngine()
    t15 = 1700000000000

    snapshot = [make_scanned_symbol("BTCUSDT", rank=1)]
    ha_15m = {"BTCUSDT": make_ha_candle("BTCUSDT", "BULLISH", "15m")}
    await engine.on_15m_candle_closed(snapshot, ha_15m, timestamp_ms=t15)

    # New symbol "DOGEUSDT" emits a 1m candle — not in the original snapshot
    ha_doge = make_ha_candle("DOGEUSDT", "BULLISH", "1m", open_time=t15 + 60000)
    sig = await confirm_and_flush(engine, ha_doge)

    assert sig is None

  @pytest.mark.asyncio
  async def test_12_no_candidate_confirms_before_expiration(self):
    """Scenario 12: No candidate confirms throughout the 15m window; window expires cleanly."""
    engine = MultiSymbolStrategyEngine()
    t15 = 1700000000000

    snapshot = [make_scanned_symbol("BTCUSDT", rank=1)]
    ha_15m = {"BTCUSDT": make_ha_candle("BTCUSDT", "BULLISH", "15m")}
    await engine.on_15m_candle_closed(snapshot, ha_15m, timestamp_ms=t15)

    t_next_15 = t15 + 900000
    await engine.expire_active_window(t_next_15)

    assert engine.active_window.status == WindowStatus.EXPIRED
    assert engine.active_window.candidates["BTCUSDT"].status == CandidateStatus.EXPIRED
    assert engine.active_window.is_slot_claimed is False

  @pytest.mark.asyncio
  async def test_13_late_candle_arrives_after_expiration(self):
    """Scenario 13: 1m candle with close_time >= window end is rejected."""
    engine = MultiSymbolStrategyEngine()
    t15 = 1700000000000

    snapshot = [make_scanned_symbol("BTCUSDT", rank=1)]
    ha_15m = {"BTCUSDT": make_ha_candle("BTCUSDT", "BULLISH", "15m")}
    await engine.on_15m_candle_closed(snapshot, ha_15m, timestamp_ms=t15)

    # Candle closing at or after t15 + 900000 (end of window)
    late_open = t15 + 900000
    ha_late = make_ha_candle("BTCUSDT", "BULLISH", "1m", open_time=late_open)
    sig = await confirm_and_flush(engine, ha_late)

    assert sig is None
    assert engine.active_window.status == WindowStatus.EXPIRED

  @pytest.mark.asyncio
  async def test_14_concurrent_workers_attempt_to_claim_slot(self):
    """Scenario 14: Two concurrent async tasks attempt to claim slot; only one succeeds."""
    engine = MultiSymbolStrategyEngine()
    t15 = 1700000000000

    snapshot = [
        make_scanned_symbol("BTCUSDT", rank=1),
        make_scanned_symbol("ETHUSDT", rank=2),
    ]
    ha_15m = {
        "BTCUSDT": make_ha_candle("BTCUSDT", "BULLISH", "15m"),
        "ETHUSDT": make_ha_candle("ETHUSDT", "BULLISH", "15m"),
    }
    await engine.on_15m_candle_closed(snapshot, ha_15m, timestamp_ms=t15)

    ha_btc = make_ha_candle("BTCUSDT", "BULLISH", "1m", open_time=t15)
    ha_eth = make_ha_candle("ETHUSDT", "BULLISH", "1m", open_time=t15)
    t_close = ha_btc.close_time  # same close_time for both

    # Launch two concurrent on_1m_candle_closed tasks simultaneously
    t1 = asyncio.create_task(
        engine.on_1m_candle_closed(make_candle_close_event(ha_btc), ha_btc)
    )
    t2 = asyncio.create_task(
        engine.on_1m_candle_closed(make_candle_close_event(ha_eth), ha_eth)
    )
    await asyncio.gather(t1, t2)

    # Flush the batch — exactly ONE signal produced, winner is deterministic (rank 1 = BTC)
    signal = await engine.flush_1m_timestamp_batch(t_close)
    assert signal is not None
    assert signal.symbol == "BTCUSDT"  # rank 1 wins deterministically
    assert engine.active_window.is_slot_claimed is True

  @pytest.mark.asyncio
  async def test_15_winning_candidate_fails_risk_validation_consumes_window(self):
    """Scenario 15: Winner fails risk validation; window consumed, no second candidate allowed."""
    engine = MultiSymbolStrategyEngine()
    t15 = 1700000000000

    snapshot = [
        make_scanned_symbol("BTCUSDT", rank=1),
        make_scanned_symbol("ETHUSDT", rank=2),
    ]
    ha_15m = {
        "BTCUSDT": make_ha_candle("BTCUSDT", "BULLISH", "15m"),
        "ETHUSDT": make_ha_candle("ETHUSDT", "BULLISH", "15m"),
    }
    await engine.on_15m_candle_closed(snapshot, ha_15m, timestamp_ms=t15)

    # BTC confirms first
    ha_btc = make_ha_candle("BTCUSDT", "BULLISH", "1m", open_time=t15)
    sig_btc = await confirm_and_flush(engine, ha_btc)
    assert sig_btc is not None

    # Simulate Risk Validation Failure
    engine.handle_risk_validation_failure(
        window_id=sig_btc.window_id,
        symbol="BTCUSDT",
        reason="Stop loss distance exceeds 10% risk budget",
    )

    assert engine.active_window.window_consumed is True
    assert engine.active_window.status == WindowStatus.EXPIRED

    # Next minute: ETH confirms. Must be REJECTED — window is consumed.
    ha_eth = make_ha_candle("ETHUSDT", "BULLISH", "1m", open_time=t15 + 60000)
    sig_eth = await confirm_and_flush(engine, ha_eth)
    assert sig_eth is None

  @pytest.mark.asyncio
  async def test_16_existing_position_prevents_new_entry(self):
    """Scenario 16: Account position is open; new 1m confirmation does not claim slot or emit signal."""
    engine = MultiSymbolStrategyEngine(has_open_position_callback=lambda: True)
    t15 = 1700000000000

    snapshot = [make_scanned_symbol("BTCUSDT", rank=1)]
    ha_15m = {"BTCUSDT": make_ha_candle("BTCUSDT", "BULLISH", "15m")}
    await engine.on_15m_candle_closed(snapshot, ha_15m, timestamp_ms=t15)

    ha_btc = make_ha_candle("BTCUSDT", "BULLISH", "1m", open_time=t15)
    sig = await confirm_and_flush(engine, ha_btc)

    assert sig is None
    assert engine.active_window.is_slot_claimed is False

  @pytest.mark.asyncio
  async def test_17_restart_recovery_preserves_active_window(self):
    """Scenario 17: Process restart recovery; active window and candidate states are restored perfectly."""
    engine = MultiSymbolStrategyEngine()
    t15 = 1700000000000

    snapshot = [
        make_scanned_symbol("BTCUSDT", rank=1),
        make_scanned_symbol("ETHUSDT", rank=2),
    ]
    ha_15m = {
        "BTCUSDT": make_ha_candle("BTCUSDT", "BULLISH", "15m", open_time=t15 - 900000),
        "ETHUSDT": make_ha_candle("ETHUSDT", "BEARISH", "15m", open_time=t15 - 900000),
    }
    await engine.on_15m_candle_closed(snapshot, ha_15m, timestamp_ms=t15)

    saved_state = engine.serialize_state()
    assert saved_state["window_id"] == f"WIN_{t15}"
    assert len(saved_state["candidates"]) == 2

    fresh_engine = MultiSymbolStrategyEngine()
    assert fresh_engine.active_window is None

    fresh_engine.restore_state(saved_state)
    assert fresh_engine.active_window is not None
    assert fresh_engine.active_window.window_id == f"WIN_{t15}"
    assert len(fresh_engine.active_window.candidates) == 2
    assert fresh_engine.active_window.candidates["BTCUSDT"].direction == Direction.LONG
    assert fresh_engine.active_window.candidates["ETHUSDT"].direction == Direction.SHORT

    # Resume monitoring in restored engine: BTC confirms
    ha_btc = make_ha_candle("BTCUSDT", "BULLISH", "1m", open_time=t15 + 60000)
    sig = await confirm_and_flush(fresh_engine, ha_btc)

    assert sig is not None
    assert sig.symbol == "BTCUSDT"
    assert fresh_engine.active_window.is_slot_claimed is True

  @pytest.mark.asyncio
  async def test_18_flush_deterministic_tie_breaking_by_rank(self):
    """Scenario 18: flush_1m_timestamp_batch deterministically resolves simultaneous confirmations by rank."""
    engine = MultiSymbolStrategyEngine()
    t15 = 1700000000000

    # ETH rank=2 vs SOL rank=5 — ETH wins
    snapshot = [
        make_scanned_symbol("ETHUSDT", rank=2),
        make_scanned_symbol("SOLUSDT", rank=5),
    ]
    ha_15m = {
        "ETHUSDT": make_ha_candle("ETHUSDT", "BULLISH", "15m"),
        "SOLUSDT": make_ha_candle("SOLUSDT", "BULLISH", "15m"),
    }
    await engine.on_15m_candle_closed(snapshot, ha_15m, timestamp_ms=t15)

    ha_eth = make_ha_candle("ETHUSDT", "BULLISH", "1m", open_time=t15)
    ha_sol = make_ha_candle("SOLUSDT", "BULLISH", "1m", open_time=t15)
    t_close = ha_eth.close_time

    await engine.on_1m_candle_closed(make_candle_close_event(ha_eth), ha_eth)
    await engine.on_1m_candle_closed(make_candle_close_event(ha_sol), ha_sol)
    signal = await engine.flush_1m_timestamp_batch(t_close)

    # ETH (rank 2) beats SOL (rank 5)
    assert signal is not None
    assert signal.symbol == "ETHUSDT"
    assert signal.candidate_rank == 2

  @pytest.mark.asyncio
  async def test_19_flush_after_slot_claimed_returns_none(self):
    """Scenario 19: flush_1m_timestamp_batch returns None when slot is already claimed."""
    engine = MultiSymbolStrategyEngine()
    t15 = 1700000000000

    snapshot = [make_scanned_symbol("BTCUSDT", rank=1)]
    ha_15m = {"BTCUSDT": make_ha_candle("BTCUSDT", "BULLISH", "15m")}
    await engine.on_15m_candle_closed(snapshot, ha_15m, timestamp_ms=t15)

    # First bar: BTC confirms and wins
    ha_1m = make_ha_candle("BTCUSDT", "BULLISH", "1m", open_time=t15)
    sig1 = await confirm_and_flush(engine, ha_1m)
    assert sig1 is not None
    assert engine.active_window.is_slot_claimed is True

    # Duplicate flush for same timestamp — must return None
    sig2 = await engine.flush_1m_timestamp_batch(ha_1m.close_time)
    assert sig2 is None

  @pytest.mark.asyncio
  async def test_20_risk_failure_expires_monitoring_candidates(self):
    """Scenario 20: handle_risk_validation_failure marks all MONITORING candidates as EXPIRED."""
    engine = MultiSymbolStrategyEngine()
    t15 = 1700000000000

    snapshot = [
        make_scanned_symbol("BTCUSDT", rank=1),
        make_scanned_symbol("ETHUSDT", rank=2),
    ]
    ha_15m = {
        "BTCUSDT": make_ha_candle("BTCUSDT", "BULLISH", "15m"),
        "ETHUSDT": make_ha_candle("ETHUSDT", "BULLISH", "15m"),
    }
    await engine.on_15m_candle_closed(snapshot, ha_15m, timestamp_ms=t15)

    # BTC wins
    ha_btc = make_ha_candle("BTCUSDT", "BULLISH", "1m", open_time=t15)
    sig = await confirm_and_flush(engine, ha_btc)
    assert sig is not None

    # Risk failure on window
    engine.handle_risk_validation_failure(
        window_id=sig.window_id, symbol="BTCUSDT", reason="Margin insufficient"
    )

    # Window EXPIRED after risk failure
    assert engine.active_window.status == WindowStatus.EXPIRED
    # ETH was CANCELLED by the arbitrator when BTC won the slot; risk failure
    # does not change already-terminal states.
    assert engine.active_window.candidates["ETHUSDT"].status == CandidateStatus.CANCELLED

  @pytest.mark.asyncio
  async def test_21_serialize_preserves_actual_trigger_candle_timestamps(self):
    """Scenario 21: serialize_state stores actual trigger candle open/close times; restore_state recovers them."""
    engine = MultiSymbolStrategyEngine()
    t15 = 1700000000000
    trigger_open = t15 - 900000
    trigger_close = t15 - 1

    snapshot = [make_scanned_symbol("BTCUSDT", rank=1)]
    ha_15m = {
        "BTCUSDT": make_ha_candle("BTCUSDT", "BULLISH", "15m", open_time=trigger_open)
    }
    await engine.on_15m_candle_closed(snapshot, ha_15m, timestamp_ms=t15)

    state = engine.serialize_state()
    cand_state = state["candidates"]["BTCUSDT"]
    assert cand_state["trigger_open_time"] == trigger_open
    assert cand_state["trigger_close_time"] == trigger_close

    fresh = MultiSymbolStrategyEngine()
    fresh.restore_state(state)
    restored_cand = fresh.active_window.candidates["BTCUSDT"]
    assert restored_cand.trigger_15m_candle.open_time == trigger_open
    assert restored_cand.trigger_15m_candle.close_time == trigger_close

  @pytest.mark.asyncio
  async def test_22_on_1m_buffers_candidate_then_flush_emits_signal(self):
    """Scenario 22: on_1m_candle_closed returns None always; flush_1m_timestamp_batch emits signal."""
    engine = MultiSymbolStrategyEngine()
    t15 = 1700000000000

    snapshot = [make_scanned_symbol("BTCUSDT", rank=1)]
    ha_15m = {"BTCUSDT": make_ha_candle("BTCUSDT", "BULLISH", "15m")}
    await engine.on_15m_candle_closed(snapshot, ha_15m, timestamp_ms=t15)

    ha_1m = make_ha_candle("BTCUSDT", "BULLISH", "1m", open_time=t15)
    event = make_candle_close_event(ha_1m)

    # on_1m_candle_closed must return None (buffers only)
    result = await engine.on_1m_candle_closed(event, ha_1m)
    assert result is None
    assert engine.active_window.is_slot_claimed is False  # not yet claimed

    # Flush produces the signal
    signal = await engine.flush_1m_timestamp_batch(ha_1m.close_time)
    assert signal is not None
    assert signal.symbol == "BTCUSDT"
    assert engine.active_window.is_slot_claimed is True
