"""Chronological financial accounting ledger for backtesting."""

from datetime import datetime, timezone
from typing import Dict, List, Tuple


class AccountLedger:
  """Tracks simulated wallet balance, isolated margin, costs, equity curve, and drawdown."""

  def __init__(self, initial_balance: float = 1000.0):
    self.initial_balance = initial_balance
    self.wallet_balance = initial_balance
    self.isolated_margin_locked = 0.0

    # Cumulative cost & PnL counters
    self.realized_pnl = 0.0
    self.entry_fees = 0.0
    self.exit_fees = 0.0
    self.funding_paid = 0.0
    self.spread_cost = 0.0
    self.slippage_cost = 0.0

    # Drawdown tracking
    self._peak_equity = initial_balance
    self.max_drawdown_usdt = 0.0
    self.max_drawdown_pct = 0.0
    self.max_account_exposure = 0.0
    self.max_margin_utilization = 0.0
    self.liquidation_events = 0

    # Time series
    self.equity_curve: List[Tuple[int, float]] = []
    self.daily_pnl: Dict[str, float] = {}
    self.daily_trade_count: Dict[str, int] = {}

  @property
  def available_balance(self) -> float:
    return max(0.0, self.wallet_balance - self.isolated_margin_locked)

  def lock_margin(self, amount: float) -> None:
    self.isolated_margin_locked += amount
    if self.wallet_balance > 0:
      util = self.isolated_margin_locked / self.wallet_balance
      self.max_margin_utilization = max(self.max_margin_utilization, util)

  def release_margin(self) -> None:
    self.isolated_margin_locked = 0.0

  def record_exposure(self, notional: float) -> None:
    self.max_account_exposure = max(self.max_account_exposure, notional)

  def charge_entry_fee(self, fee: float, spread_cost: float, slippage_cost: float) -> None:
    self.wallet_balance -= fee
    self.entry_fees += fee
    self.spread_cost += spread_cost
    self.slippage_cost += slippage_cost

  def apply_funding(self, amount: float) -> None:
    """Applies funding payment (positive = paid by trader, negative = received)."""
    self.wallet_balance -= amount
    self.funding_paid += amount

  def settle_trade_exit(
      self,
      timestamp_ms: int,
      gross_pnl: float,
      exit_fee: float,
      spread_cost: float,
      slippage_cost: float,
      is_liquidation: bool = False,
  ) -> float:
    """Settles realized PnL and exit fees, updates drawdown and daily stats."""
    self.release_margin()
    self.wallet_balance += gross_pnl - exit_fee
    self.realized_pnl += gross_pnl
    self.exit_fees += exit_fee
    self.spread_cost += spread_cost
    self.slippage_cost += slippage_cost
    if is_liquidation:
      self.liquidation_events += 1

    # Update equity watermark & drawdown
    if self.wallet_balance > self._peak_equity:
      self._peak_equity = self.wallet_balance
    dd_usdt = self._peak_equity - self.wallet_balance
    dd_pct = (dd_usdt / self._peak_equity * 100.0) if self._peak_equity > 0 else 0.0
    self.max_drawdown_usdt = max(self.max_drawdown_usdt, dd_usdt)
    self.max_drawdown_pct = max(self.max_drawdown_pct, dd_pct)

    self.equity_curve.append((timestamp_ms, self.wallet_balance))

    # Daily attribution (UTC)
    day_str = datetime.fromtimestamp(
        timestamp_ms / 1000.0, tz=timezone.utc
    ).strftime("%Y-%m-%d")
    self.daily_pnl[day_str] = self.daily_pnl.get(day_str, 0.0) + (
        gross_pnl - exit_fee
    )
    self.daily_trade_count[day_str] = self.daily_trade_count.get(day_str, 0) + 1

    return self.wallet_balance
