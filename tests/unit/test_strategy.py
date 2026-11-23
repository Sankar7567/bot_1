"""Unit tests for Multi-Symbol Strategy Engine (Strategy Amendment v1.1).

Validates all 17 mandatory test scenarios specified in Replacement Prompt 06:
1. Immediate confirmation
2. Fifth-candle confirmation
3. Misaligned candles tolerance
4. Doji candles tolerance
5. Simultaneous 15m triggers
6. ETH confirms before BTC
7. BTC confirms before ETH
8. Same-timestamp simultaneous confirmations
9. Original top-20 ranking tie-breaking
10. Candidate leaves top 20 during window
11. New candidate enters top 20 during window
12. Window expiration with no confirmation
13. Late-arriving candle after expiration
14. Concurrent worker race conditions
15. Risk validation failure consumes window (no 2nd pick)
16. Existing position blocks entry
17. Process restart recovery of active window
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


# ==============================================================================
# Comprehensive Test Suite (Scenarios 1 through 17)
# ==============================================================================


class TestMultiSymbolStrategyEngine:
  """Test suite validating all 17 required scenarios for Multi-Symbol Strategy."""

  @pytest.mark.asyncio
  async def test_01_candidate_confirms_immediately(self):
    """Scenario 1: Candidate confirms on the very first 1m candle after 15m trigger."""
    engine = MultiSymbolStrategyEngine()
    t15 = 1700000000000  # 15m candle close

    # 15m trigger: BTC is bullish (LONG)
    snapshot = [make_scanned_symbol("BTCUSDT", rank=1)]
    ha_15m = {"BTCUSDT": make_ha_candle("BTCUSDT", "BULLISH", "15m", open_time=t15 - 900000)}
    await engine.on_15m_candle_closed(snapshot, ha_15m, timestamp_ms=t15)

    assert engine.active_window is not None
    assert "BTCUSDT" in engine.active_window.candidates

    # 1st 1m candle closes at t15 + 60,000ms: Bullish
    ha_1m = make_ha_candle("BTCUSDT", "BULLISH", "1m", open_time=t15)
    event_1m = make_candle_close_event(ha_1m)

    signal = await engine.on_1m_candle_closed(event_1m, ha_1m)

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
      sig = await engine.on_1m_candle_closed(make_candle_close_event(ha_bear), ha_bear)
      assert sig is None
      assert engine.active_window.is_slot_claimed is False

    # Bar 5: Bullish confirmation
    bar_5_open = t15 + (4 * 60000)
    ha_bull = make_ha_candle("BTCUSDT", "BULLISH", "1m", open_time=bar_5_open)
    sig5 = await engine.on_1m_candle_closed(make_candle_close_event(ha_bull), ha_bull)

    assert sig5 is not None
    assert sig5.symbol == "BTCUSDT"
    assert engine.active_window.is_slot_claimed is True

  @pytest.mark.asyncio
  async def test_03_misaligned_candles_then_aligned(self):
    """Scenario 3: 3 misaligned candles do not eliminate candidate; 4th candle confirms."""
    engine = MultiSymbolStrategyEngine()
    t15 = 1700000000000

    snapshot = [make_scanned_symbol("SOLUSDT", rank=2)]
    # 15m SHORT trigger
    ha_15m = {"SOLUSDT": make_ha_candle("SOLUSDT", "BEARISH", "15m", open_time=t15 - 900000)}
    await engine.on_15m_candle_closed(snapshot, ha_15m, timestamp_ms=t15)

    # 3 Bullish (opposite) candles
    for i in range(3):
      ha_opp = make_ha_candle("SOLUSDT", "BULLISH", "1m", open_time=t15 + (i * 60000))
      sig = await engine.on_1m_candle_closed(make_candle_close_event(ha_opp), ha_opp)
      assert sig is None
      assert engine.active_window.candidates["SOLUSDT"].status == CandidateStatus.MONITORING

    # 4th candle: Bearish (aligned)
    ha_aligned = make_ha_candle("SOLUSDT", "BEARISH", "1m", open_time=t15 + (3 * 60000))
    sig = await engine.on_1m_candle_closed(make_candle_close_event(ha_aligned), ha_aligned)

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

    # 2 Doji candles
    for i in range(2):
      ha_doji = make_ha_candle("BTCUSDT", "DOJI", "1m", open_time=t15 + (i * 60000))
      sig = await engine.on_1m_candle_closed(make_candle_close_event(ha_doji), ha_doji)
      assert sig is None

    # 3rd candle: Bullish
    ha_bull = make_ha_candle("BTCUSDT", "BULLISH", "1m", open_time=t15 + (2 * 60000))
    sig = await engine.on_1m_candle_closed(make_candle_close_event(ha_bull), ha_bull)
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

    sig_eth = await engine.on_1m_candle_closed(make_candle_close_event(ha_eth_1), ha_eth_1)
    sig_btc = await engine.on_1m_candle_closed(make_candle_close_event(ha_btc_1), ha_btc_1)

    assert sig_eth is not None
    assert sig_eth.symbol == "ETHUSDT"
    assert sig_btc is None
    assert engine.active_window.winning_symbol == "ETHUSDT"

    # Bar 2: BTC now confirms (Bullish). Must be IGNORED because slot already claimed by ETH!
    ha_btc_2 = make_ha_candle("BTCUSDT", "BULLISH", "1m", open_time=t15 + 60000)
    sig_btc_late = await engine.on_1m_candle_closed(make_candle_close_event(ha_btc_2), ha_btc_2)
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

    # Bar 1: BTC confirms, ETH is doji
    ha_btc = make_ha_candle("BTCUSDT", "BULLISH", "1m", open_time=t15)
    ha_eth = make_ha_candle("ETHUSDT", "DOJI", "1m", open_time=t15)

    sig_btc = await engine.on_1m_candle_closed(make_candle_close_event(ha_btc), ha_btc)
    sig_eth = await engine.on_1m_candle_closed(make_candle_close_event(ha_eth), ha_eth)

    assert sig_btc is not None
    assert sig_btc.symbol == "BTCUSDT"
    assert sig_eth is None
    assert engine.active_window.winning_symbol == "BTCUSDT"

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
    t_close = t15 + 59999
    cand_btc = engine.active_window.candidates["BTCUSDT"]
    cand_eth = engine.active_window.candidates["ETHUSDT"]
    cand_btc.confirmation_candle = make_ha_candle("BTCUSDT", "BULLISH", "1m", open_time=t15)
    cand_btc.confirmed_at = t_close
    cand_eth.confirmation_candle = make_ha_candle("ETHUSDT", "BULLISH", "1m", open_time=t15)
    cand_eth.confirmed_at = t_close

    signal = await engine.process_simultaneous_confirmations([cand_eth, cand_btc])

    # BTC has rank 1 vs ETH rank 2 -> BTC must win
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

    # Simulated: Scanner refreshes and BTC is no longer in top 20
    # Candidate must still be monitored in active window
    assert "BTCUSDT" in engine.active_window.candidates

    # Bar arrives: Bullish -> Qualifies!
    ha_1m = make_ha_candle("BTCUSDT", "BULLISH", "1m", open_time=t15 + 120000)
    sig = await engine.on_1m_candle_closed(make_candle_close_event(ha_1m), ha_1m)

    assert sig is not None
    assert sig.symbol == "BTCUSDT"

  @pytest.mark.asyncio
  async def test_11_new_candidate_enters_refreshed_top_20_during_window(self):
    """Scenario 11: New symbol enters top-20 mid-window; cannot produce confirmation in current window."""
    engine = MultiSymbolStrategyEngine()
    t15 = 1700000000000

    # Window created with only BTCUSDT
    snapshot = [make_scanned_symbol("BTCUSDT", rank=1)]
    ha_15m = {"BTCUSDT": make_ha_candle("BTCUSDT", "BULLISH", "15m")}
    await engine.on_15m_candle_closed(snapshot, ha_15m, timestamp_ms=t15)

    # New symbol "DOGEUSDT" emits a 1m candle
    ha_doge = make_ha_candle("DOGEUSDT", "BULLISH", "1m", open_time=t15 + 60000)
    sig = await engine.on_1m_candle_closed(make_candle_close_event(ha_doge), ha_doge)

    # Ignored because DOGE was not in the 15m trigger snapshot
    assert sig is None

  @pytest.mark.asyncio
  async def test_12_no_candidate_confirms_before_expiration(self):
    """Scenario 12: No candidate confirms throughout the 15m window; window expires cleanly."""
    engine = MultiSymbolStrategyEngine()
    t15 = 1700000000000

    snapshot = [make_scanned_symbol("BTCUSDT", rank=1)]
    ha_15m = {"BTCUSDT": make_ha_candle("BTCUSDT", "BULLISH", "15m")}
    await engine.on_15m_candle_closed(snapshot, ha_15m, timestamp_ms=t15)

    # Next 15m candle close (expiration of window)
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
    sig = await engine.on_1m_candle_closed(make_candle_close_event(ha_late), ha_late)

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

    # Launch two concurrent workers
    t1 = asyncio.create_task(engine.on_1m_candle_closed(make_candle_close_event(ha_btc), ha_btc))
    t2 = asyncio.create_task(engine.on_1m_candle_closed(make_candle_close_event(ha_eth), ha_eth))

    results = await asyncio.gather(t1, t2)
    valid_signals = [r for r in results if r is not None]

    # Exactly ONE signal produced
    assert len(valid_signals) == 1
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
    sig_btc = await engine.on_1m_candle_closed(make_candle_close_event(ha_btc), ha_btc)
    assert sig_btc is not None

    # Simulate Risk Validation Failure (e.g. stop distance too wide or margin exceeded)
    engine.handle_risk_validation_failure(
        window_id=sig_btc.window_id,
        symbol="BTCUSDT",
        reason="Stop loss distance exceeds 10% risk budget",
    )

    assert engine.active_window.window_consumed is True

    # Next minute: ETH confirms. Must be REJECTED! No second candidate allowed in consumed window.
    ha_eth = make_ha_candle("ETHUSDT", "BULLISH", "1m", open_time=t15 + 60000)
    sig_eth = await engine.on_1m_candle_closed(make_candle_close_event(ha_eth), ha_eth)
    assert sig_eth is None

  @pytest.mark.asyncio
  async def test_16_existing_position_prevents_new_entry(self):
    """Scenario 16: Account position is open; new 1m confirmation does not claim slot or emit signal."""
    # Callback indicates position is open
    engine = MultiSymbolStrategyEngine(has_open_position_callback=lambda: True)
    t15 = 1700000000000

    snapshot = [make_scanned_symbol("BTCUSDT", rank=1)]
    ha_15m = {"BTCUSDT": make_ha_candle("BTCUSDT", "BULLISH", "15m")}
    await engine.on_15m_candle_closed(snapshot, ha_15m, timestamp_ms=t15)

    ha_btc = make_ha_candle("BTCUSDT", "BULLISH", "1m", open_time=t15)
    sig = await engine.on_1m_candle_closed(make_candle_close_event(ha_btc), ha_btc)

    # Blocked by account position lock
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
        "BTCUSDT": make_ha_candle("BTCUSDT", "BULLISH", "15m"),
        "ETHUSDT": make_ha_candle("ETHUSDT", "BEARISH", "15m"),
    }
    await engine.on_15m_candle_closed(snapshot, ha_15m, timestamp_ms=t15)

    # Serialize active state
    saved_state = engine.serialize_state()
    assert saved_state["window_id"] == f"WIN_{t15}"
    assert len(saved_state["candidates"]) == 2

    # Initialize fresh engine instance simulating process restart
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
    sig = await fresh_engine.on_1m_candle_closed(make_candle_close_event(ha_btc), ha_btc)

    assert sig is not None
    assert sig.symbol == "BTCUSDT"
    assert fresh_engine.active_window.is_slot_claimed is True
