"""Data models for Market Scanner."""

from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class ScannedSymbol:
  """Ranked market candidate qualifying for the trading universe."""

  rank: int  # 1-based rank (1 = highest percentage gainer)
  symbol: str
  price_change_percent: float
  quote_volume: float
  last_price: float
  base_asset: str
  quote_asset: str
  contract_type: str
  scan_timestamp: int


@dataclass(frozen=True)
class ScanResult:
  """Completed market scan result capturing universe snapshot and metrics."""

  scan_timestamp: int  # UTC ms when scan completed
  symbols: List[ScannedSymbol]  # Top N ranked symbols
  total_evaluated_count: int
  total_eligible_count: int
  scan_duration_ms: float

  def get_symbol_names(self) -> List[str]:
    """Returns list of symbol names in ranked order."""
    return [s.symbol for s in self.symbols]

  def get_rank(self, symbol: str) -> Optional[int]:
    """Returns the 1-based rank of a symbol if present in the scanned universe."""
    for s in self.symbols:
      if s.symbol == symbol:
        return s.rank
    return None

  def is_in_universe(self, symbol: str) -> bool:
    """Returns True if the symbol is in the scanned universe."""
    return any(s.symbol == symbol for s in self.symbols)
