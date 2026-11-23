"""Data models and state representations for the Multi-Symbol Strategy Engine."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional
from src.indicators.heikin_ashi import HeikinAshiCandle


class Direction(str, Enum):
  LONG = "LONG"
  SHORT = "SHORT"


class CandidateStatus(str, Enum):
  MONITORING = "MONITORING"
  QUALIFIED = "QUALIFIED"
  CANCELLED = "CANCELLED"
  EXPIRED = "EXPIRED"


class WindowStatus(str, Enum):
  ACTIVE = "ACTIVE"
  SLOT_CLAIMED = "SLOT_CLAIMED"
  EXPIRED = "EXPIRED"


@dataclass
class CandidateState:
  """State of a candidate symbol within an active confirmation window."""

  symbol: str
  direction: Direction
  initial_rank: int
  registered_at: int  # 15m candle close timestamp in ms
  trigger_15m_candle: HeikinAshiCandle
  status: CandidateStatus = CandidateStatus.MONITORING
  confirmation_candle: Optional[HeikinAshiCandle] = None
  confirmed_at: Optional[int] = None


@dataclass(frozen=True)
class TradeSignal:
  """Qualified trading signal produced by winning candidate in confirmation window."""

  signal_id: str
  window_id: str
  symbol: str
  direction: Direction
  trigger_15m_timestamp: int
  confirmation_1m_timestamp: int
  trigger_price: float
  confirmation_price: float
  candidate_rank: int
  created_at: int
  expires_at: int


@dataclass
class ConfirmationWindow:
  """Global 15-minute confirmation window tracking candidates concurrently."""

  window_id: str
  start_timestamp: int  # ms UTC (15m candle close)
  end_timestamp: int  # ms UTC (start + 900,000ms)
  candidates: Dict[str, CandidateState] = field(default_factory=dict)
  status: WindowStatus = WindowStatus.ACTIVE
  is_slot_claimed: bool = False
  winning_symbol: Optional[str] = None
  winning_signal: Optional[TradeSignal] = None
  window_consumed: bool = False  # Set to True on winner selection or risk failure

  @property
  def is_active(self) -> bool:
    return self.status == WindowStatus.ACTIVE and not self.window_consumed

  def get_candidate(self, symbol: str) -> Optional[CandidateState]:
    return self.candidates.get(symbol.upper())

  def get_active_candidates(self) -> List[CandidateState]:
    return [
        c
        for c in self.candidates.values()
        if c.status == CandidateStatus.MONITORING
    ]
