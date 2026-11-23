"""Data transfer models and schemas for Binance USDⓈ-M Futures connector.

Immutable data structures with strict type definitions for account data,
market filters, orders, and positions.
"""

from dataclasses import dataclass
import math
from typing import List, Optional


@dataclass(frozen=True)
class ServerTimeResponse:
  """Binance server time and client clock offset."""

  server_time: int  # UTC ms timestamp from Binance
  local_time: int  # UTC ms timestamp on client machine
  time_offset_ms: int  # server_time - local_time


@dataclass(frozen=True)
class SymbolFilter:
  """Parsed trading filters for a specific Futures symbol."""

  symbol: str
  tick_size: float
  step_size: float
  min_qty: float
  max_qty: float
  min_notional: float
  price_precision: int
  quantity_precision: int

  @classmethod
  def from_raw_filters(
      cls,
      symbol: str,
      filters: List[dict],
      price_precision: int = 2,
      quantity_precision: int = 3,
  ) -> "SymbolFilter":
    """Extracts and parses PRICE_FILTER, LOT_SIZE, and MIN_NOTIONAL filters."""
    tick_size = 0.01
    step_size = 0.001
    min_qty = 0.001
    max_qty = 1000000.0
    min_notional = 5.0

    for f in filters:
      f_type = f.get("filterType")
      if f_type == "PRICE_FILTER":
        tick_size = float(f.get("tickSize", 0.01))
      elif f_type == "LOT_SIZE":
        step_size = float(f.get("stepSize", 0.001))
        min_qty = float(f.get("minQty", 0.001))
        max_qty = float(f.get("maxQty", 1000000.0))
      elif f_type == "MIN_NOTIONAL":
        min_notional = float(f.get("notional", 5.0))

    # Derive decimal precision from step_size and tick_size if precision not provided
    if tick_size > 0:
      price_prec = max(0, int(round(-math.log10(tick_size))))
    else:
      price_prec = price_precision

    if step_size > 0:
      qty_prec = max(0, int(round(-math.log10(step_size))))
    else:
      qty_prec = quantity_precision

    return cls(
        symbol=symbol,
        tick_size=tick_size,
        step_size=step_size,
        min_qty=min_qty,
        max_qty=max_qty,
        min_notional=min_notional,
        price_precision=price_prec,
        quantity_precision=qty_prec,
    )


@dataclass(frozen=True)
class SymbolInfo:
  """Metadata and parsed filters for a trading contract."""

  symbol: str
  status: str
  base_asset: str
  quote_asset: str
  contract_type: str
  filters: SymbolFilter

  @property
  def is_trading(self) -> bool:
    return self.status == "TRADING"

  @property
  def is_usdt_perpetual(self) -> bool:
    return self.quote_asset == "USDT" and self.contract_type == "PERPETUAL"


@dataclass(frozen=True)
class Ticker24hr:
  """24-hour rolling ticker price change statistics."""

  symbol: str
  price_change: float
  price_change_percent: float
  weighted_avg_price: float
  last_price: float
  last_qty: float
  open_price: float
  high_price: float
  low_price: float
  volume: float
  quote_volume: float
  open_time: int
  close_time: int
  first_id: int
  last_id: int
  count: int


@dataclass(frozen=True)
class AccountInfo:
  """Futures account summary."""

  fee_tier: int
  can_trade: bool
  can_deposit: bool
  can_withdraw: bool
  total_margin_balance: float
  total_wallet_balance: float
  total_unrealized_profit: float
  available_balance: float


@dataclass(frozen=True)
class AssetBalance:
  """Individual asset balance (e.g., USDT)."""

  asset: str
  wallet_balance: float
  unrealized_profit: float
  margin_balance: float
  available_balance: float


@dataclass(frozen=True)
class PositionRisk:
  """Real-time position information for a contract."""

  symbol: str
  position_amt: float
  entry_price: float
  mark_price: float
  unrealized_profit: float
  liquidation_price: float
  leverage: int
  margin_type: str
  position_side: str

  @property
  def is_isolated(self) -> bool:
    return self.margin_type.upper() == "ISOLATED"

  @property
  def is_open(self) -> bool:
    return abs(self.position_amt) > 1e-8


@dataclass(frozen=True)
class OpenOrder:
  """Active open order on the exchange."""

  symbol: str
  order_id: int
  client_order_id: str
  price: float
  orig_qty: float
  executed_qty: float
  status: str
  order_type: str
  side: str
  time_in_force: str
  stop_price: float
  reduce_only: bool
  update_time: int


@dataclass(frozen=True)
class LeverageBracketTier:
  """Individual leverage tier in symbol leverage bracket."""

  bracket: int
  initial_leverage: int
  notional_cap: float
  notional_floor: float
  maint_margin_ratio: float


@dataclass(frozen=True)
class SymbolLeverageBrackets:
  """Leverage bracket structure for a symbol."""

  symbol: str
  brackets: List[LeverageBracketTier]

  @property
  def max_leverage(self) -> int:
    """Returns the maximum leverage available for the lowest notional tier."""
    if not self.brackets:
      return 20
    return max(b.initial_leverage for b in self.brackets)

  def get_bracket_for_notional(
      self, notional: float
  ) -> Optional[LeverageBracketTier]:
    """Finds the applicable leverage bracket tier for a target position notional."""
    for tier in sorted(self.brackets, key=lambda b: b.notional_cap):
      if notional <= tier.notional_cap:
        return tier
    # If notional exceeds all caps, return highest cap tier
    return self.brackets[-1] if self.brackets else None
