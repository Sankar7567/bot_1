"""Global Slot Arbitrator for Strategy Confirmation Window.

Provides concurrency-safe, deterministic arbitration when candidates confirm,
resolving same-timestamp confirmations using original snapshot rankings.
"""

import asyncio
from typing import List, Optional
from src.monitoring.logger import get_logger
from src.strategy.models import (
    CandidateState,
    CandidateStatus,
    ConfirmationWindow,
    TradeSignal,
    WindowStatus,
)

logger = get_logger("strategy.arbitrator")


class GlobalSlotArbitrator:
  """Thread- and task-safe arbitrator resolving the global entry slot."""

  def __init__(self):
    self._lock = asyncio.Lock()

  async def arbitrate_winners(
      self,
      window: ConfirmationWindow,
      confirmed_candidates: List[CandidateState],
  ) -> Optional[CandidateState]:
    """Arbitrates among one or more candidate confirmations arriving for the same 1m timestamp.

    Deterministic tie-breaking:
    1. Initial rank in snapshot ascending (1 is best).
    2. Alphabetical symbol name ascending.

    Returns the winning candidate and atomically consumes the window.
    """
    async with self._lock:
      # If window is already consumed or expired, reject all
      if not window.is_active or window.is_slot_claimed:
        return None

      if not confirmed_candidates:
        return None

      # Deterministic sorting
      sorted_candidates = sorted(
          confirmed_candidates,
          key=lambda c: (c.initial_rank, c.symbol),
      )
      winner = sorted_candidates[0]

      # Atomically claim slot
      window.is_slot_claimed = True
      window.winning_symbol = winner.symbol
      window.status = WindowStatus.SLOT_CLAIMED
      window.window_consumed = True
      winner.status = CandidateStatus.QUALIFIED

      # Cancel all other candidates in the window
      for sym, cand in window.candidates.items():
        if sym != winner.symbol and cand.status == CandidateStatus.MONITORING:
          cand.status = CandidateStatus.CANCELLED

      logger.info(
          "Candidate %s won the global entry slot in window %s (Rank: %d,"
          " %d candidates competed).",
          winner.symbol,
          window.window_id,
          winner.initial_rank,
          len(confirmed_candidates),
      )

      return winner
