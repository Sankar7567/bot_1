"""Multi-Symbol Strategy Engine (Strategy Amendment v1.1).

Coordinates global 15-minute confirmation windows, concurrent multi-candle 1m
monitoring across top-20 candidates, first-trigger-wins slot arbitration,
and deterministic tie-breaking.
"""

import asyncio
import time
from typing import Callable, Coroutine, Dict, List, Optional
from src.indicators.heikin_ashi import HeikinAshiCandle
from src.market_data.models import CandleCloseEvent
from src.monitoring.logger import get_logger
from src.scanner.models import ScannedSymbol
from src.strategy.arbitrator import GlobalSlotArbitrator
from src.strategy.models import (
    CandidateState,
    CandidateStatus,
    ConfirmationWindow,
    Direction,
    TradeSignal,
    WindowStatus,
)

logger = get_logger("strategy.engine")


class MultiSymbolStrategyEngine:
  """Coordinates 15m directional triggers and concurrent 1m multi-symbol confirmation."""

  def __init__(
      self,
      has_open_position_callback: Optional[Callable[[], bool]] = None,
      arbitrator: Optional[GlobalSlotArbitrator] = None,
  ):
    self.has_open_position = has_open_position_callback or (lambda: False)
    self.arbitrator = arbitrator or GlobalSlotArbitrator()

    self._active_window: Optional[ConfirmationWindow] = None
    self._signal_listeners: List[Callable[[TradeSignal], Coroutine]] = []
    self._window_history: List[ConfirmationWindow] = []
    self._lock = asyncio.Lock()

  @property
  def active_window(self) -> Optional[ConfirmationWindow]:
    """Returns the currently active confirmation window if any."""
    return self._active_window

  def subscribe_signal(
      self, listener: Callable[[TradeSignal], Coroutine]
  ) -> None:
    """Registers an asynchronous callback to be notified when a winning TradeSignal is emitted."""
    self._signal_listeners.append(listener)

  # --------------------------------------------------------------------------
  # 15-Minute Trigger Window Creation
  # --------------------------------------------------------------------------

  async def on_15m_candle_closed(
      self,
      symbols_snapshot: List[ScannedSymbol],
      closed_15m_ha: Dict[str, HeikinAshiCandle],
      timestamp_ms: int,
  ) -> Optional[ConfirmationWindow]:
    """Creates a new 15-minute confirmation window from the top-20 snapshot.

    Args:
        symbols_snapshot: Ranked top 20 symbols at 15m close.
        closed_15m_ha: Map of symbol -> newly closed 15m Heikin-Ashi candle.
        timestamp_ms: UTC ms of the 15m candle close.

    Returns:
        ConfirmationWindow: The newly registered confirmation window.
    """
    async with self._lock:
      # Expire any existing active window
      if self._active_window and self._active_window.is_active:
        self._expire_window_internal(self._active_window, timestamp_ms)

      window_id = f"WIN_{timestamp_ms}"
      end_timestamp = timestamp_ms + (15 * 60 * 1000)  # +15 minutes
      window = ConfirmationWindow(
          window_id=window_id,
          start_timestamp=timestamp_ms,
          end_timestamp=end_timestamp,
      )

      # Evaluate 15m HA candles for each symbol in snapshot
      for scanned in symbols_snapshot:
        sym = scanned.symbol.upper()
        ha_candle = closed_15m_ha.get(sym)
        if not ha_candle or not ha_candle.is_closed:
          continue

        if ha_candle.is_bullish:
          direction = Direction.LONG
        elif ha_candle.is_bearish:
          direction = Direction.SHORT
        else:
          # Doji generates no trigger
          continue

        candidate = CandidateState(
            symbol=sym,
            direction=direction,
            initial_rank=scanned.rank,
            registered_at=timestamp_ms,
            trigger_15m_candle=ha_candle,
            status=CandidateStatus.MONITORING,
        )
        window.candidates[sym] = candidate

      self._active_window = window
      self._window_history.append(window)

      logger.info(
          "Created confirmation window %s [%d -> %d] with %d registered"
          " candidates.",
          window_id,
          timestamp_ms,
          end_timestamp,
          len(window.candidates),
      )

      return window

  # --------------------------------------------------------------------------
  # 1-Minute Candidate Confirmation
  # --------------------------------------------------------------------------

  async def on_1m_candle_closed(
      self,
      event: CandleCloseEvent,
      ha_1m: HeikinAshiCandle,
  ) -> Optional[TradeSignal]:
    """Evaluates a newly closed 1m Heikin-Ashi candle for a monitored candidate.

    Args:
        event: 1m CandleCloseEvent.
        ha_1m: Corresponding 1m HeikinAshiCandle.

    Returns:
        Optional[TradeSignal]: Emitted if this candidate wins the global slot.
    """
    window = self._active_window
    if not window or not window.is_active:
      return None

    candle = event.candle
    close_time = candle.close_time

    # Reject late-arriving candles belonging to expired window
    if close_time >= window.end_timestamp:
      async with self._lock:
        if window.is_active:
          self._expire_window_internal(window, close_time)
      return None

    # Reject candles with close_time strictly before window opened
    if close_time <= window.start_timestamp:
      return None

    # Check one-position enforcement
    if self.has_open_position():
      logger.info(
          "Candidate %s 1m confirmation ignored because account position is"
          " already open.",
          candle.symbol,
      )
      return None

    # Check if slot already claimed or window consumed
    if window.is_slot_claimed or window.window_consumed:
      return None

    candidate = window.get_candidate(candle.symbol)
    if not candidate or candidate.status != CandidateStatus.MONITORING:
      return None

    # Directional alignment check
    is_aligned = False
    if candidate.direction == Direction.LONG and ha_1m.is_bullish:
      is_aligned = True
    elif candidate.direction == Direction.SHORT and ha_1m.is_bearish:
      is_aligned = True

    if not is_aligned:
      # Misaligned or Doji -> continue monitoring on subsequent 1m bars
      return None

    # Candidate has qualified! Record confirmation
    candidate.confirmation_candle = ha_1m
    candidate.confirmed_at = close_time

    # Arbitrate slot atomically
    winner = await self.arbitrator.arbitrate_winners(window, [candidate])
    if not winner or winner.symbol != candidate.symbol:
      return None

    # Generate winning signal
    signal_id = f"SIG_{window.window_id}_{winner.symbol}_{close_time}"
    signal = TradeSignal(
        signal_id=signal_id,
        window_id=window.window_id,
        symbol=winner.symbol,
        direction=winner.direction,
        trigger_15m_timestamp=winner.registered_at,
        confirmation_1m_timestamp=close_time,
        trigger_price=winner.trigger_15m_candle.ha_close,
        confirmation_price=ha_1m.ha_close,
        candidate_rank=winner.initial_rank,
        created_at=int(time.time() * 1000),
        expires_at=window.end_timestamp,
    )

    window.winning_signal = signal

    # Dispatch to listeners
    for listener in self._signal_listeners:
      try:
        await listener(signal)
      except Exception as l_err:
        logger.error("Error in trade signal listener: %s", l_err)

    return signal

  async def process_simultaneous_confirmations(
      self,
      candidates: List[CandidateState],
  ) -> Optional[TradeSignal]:
    """Arbitrates among multiple candidate confirmations occurring at the same 1m timestamp."""
    window = self._active_window
    if not window or not window.is_active or self.has_open_position():
      return None

    winner = await self.arbitrator.arbitrate_winners(window, candidates)
    if not winner or not winner.confirmation_candle:
      return None

    signal_id = f"SIG_{window.window_id}_{winner.symbol}_{winner.confirmed_at}"
    signal = TradeSignal(
        signal_id=signal_id,
        window_id=window.window_id,
        symbol=winner.symbol,
        direction=winner.direction,
        trigger_15m_timestamp=winner.registered_at,
        confirmation_1m_timestamp=winner.confirmed_at or 0,
        trigger_price=winner.trigger_15m_candle.ha_close,
        confirmation_price=winner.confirmation_candle.ha_close,
        candidate_rank=winner.initial_rank,
        created_at=int(time.time() * 1000),
        expires_at=window.end_timestamp,
    )

    window.winning_signal = signal

    for listener in self._signal_listeners:
      try:
        await listener(signal)
      except Exception as l_err:
        logger.error("Error in trade signal listener: %s", l_err)

    return signal

  # --------------------------------------------------------------------------
  # Risk Validation Handoff & Window Consumption
  # --------------------------------------------------------------------------

  def handle_risk_validation_failure(
      self, window_id: str, symbol: str, reason: str
  ) -> None:
    """Invoked when the winning candidate fails risk validation.

    Marks the window as consumed and prevents any second candidate from being
    chosen.
    """
    if self._active_window and self._active_window.window_id == window_id:
      self._active_window.window_consumed = True
      logger.warning(
          "Winning candidate %s failed risk validation (%s). Window %s"
          " consumed with no secondary pick.",
          symbol,
          reason,
          window_id,
      )

  # --------------------------------------------------------------------------
  # Expiration & Cleanup
  # --------------------------------------------------------------------------

  def _expire_window_internal(
      self, window: ConfirmationWindow, current_time_ms: int
  ) -> None:
    """Internal helper expiring an active window."""
    window.status = WindowStatus.EXPIRED
    for cand in window.candidates.values():
      if cand.status == CandidateStatus.MONITORING:
        cand.status = CandidateStatus.EXPIRED

    logger.info(
        "Confirmation window %s expired at %d. Unconfirmed candidates expired.",
        window.window_id,
        current_time_ms,
    )

  async def expire_active_window(self, current_time_ms: int) -> None:
    """Explicitly expires the current active confirmation window."""
    async with self._lock:
      if self._active_window and self._active_window.is_active:
        self._expire_window_internal(self._active_window, current_time_ms)

  # --------------------------------------------------------------------------
  # State Persistence & Recovery
  # --------------------------------------------------------------------------

  def serialize_state(self) -> dict:
    """Serializes active confirmation window state for persistent storage / recovery."""
    if not self._active_window:
      return {}

    w = self._active_window
    candidates_data = {}
    for sym, c in w.candidates.items():
      candidates_data[sym] = {
          "symbol": c.symbol,
          "direction": c.direction.value,
          "initial_rank": c.initial_rank,
          "registered_at": c.registered_at,
          "status": c.status.value,
          "confirmed_at": c.confirmed_at,
          "trigger_ha_close": c.trigger_15m_candle.ha_close,
          "trigger_ha_open": c.trigger_15m_candle.ha_open,
          "trigger_ha_high": c.trigger_15m_candle.ha_high,
          "trigger_ha_low": c.trigger_15m_candle.ha_low,
      }

    return {
        "window_id": w.window_id,
        "start_timestamp": w.start_timestamp,
        "end_timestamp": w.end_timestamp,
        "status": w.status.value,
        "is_slot_claimed": w.is_slot_claimed,
        "winning_symbol": w.winning_symbol,
        "window_consumed": w.window_consumed,
        "candidates": candidates_data,
    }

  def restore_state(self, state_dict: dict) -> None:
    """Restores active confirmation window state from persistent storage."""
    if not state_dict:
      self._active_window = None
      return

    w = ConfirmationWindow(
        window_id=state_dict["window_id"],
        start_timestamp=state_dict["start_timestamp"],
        end_timestamp=state_dict["end_timestamp"],
        status=WindowStatus(state_dict["status"]),
        is_slot_claimed=state_dict["is_slot_claimed"],
        winning_symbol=state_dict["winning_symbol"],
        window_consumed=state_dict["window_consumed"],
    )

    for sym, cd in state_dict.get("candidates", {}).items():
      trigger_ha = HeikinAshiCandle(
          symbol=cd["symbol"],
          interval="15m",
          open_time=cd["registered_at"],
          close_time=cd["registered_at"] + 899999,
          ha_open=cd["trigger_ha_open"],
          ha_high=cd["trigger_ha_high"],
          ha_low=cd["trigger_ha_low"],
          ha_close=cd["trigger_ha_close"],
          is_closed=True,
      )
      c = CandidateState(
          symbol=cd["symbol"],
          direction=Direction(cd["direction"]),
          initial_rank=cd["initial_rank"],
          registered_at=cd["registered_at"],
          trigger_15m_candle=trigger_ha,
          status=CandidateStatus(cd["status"]),
          confirmed_at=cd["confirmed_at"],
      )
      w.candidates[sym] = c

    self._active_window = w
    logger.info(
        "Restored active confirmation window %s with %d candidates.",
        w.window_id,
        len(w.candidates),
    )
