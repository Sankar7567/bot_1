"""Backtest report and metrics builder (Amendment 01 §13)."""

from typing import List
from src.backtesting.ledger import AccountLedger
from src.backtesting.models import (
    BacktestConfig,
    BacktestReport,
    CostMetrics,
    DataQualityReport,
    EquityMetrics,
    MarketSelectionMetrics,
    PerformanceMetrics,
    RiskMetrics,
    StrategyMetrics,
    TradeAuditRecord,
)


def build_backtest_report(
    config: BacktestConfig,
    run_timestamp_ms: int,
    data_quality: DataQualityReport,
    market_selection: MarketSelectionMetrics,
    strategy: StrategyMetrics,
    ledger: AccountLedger,
    trades: List[TradeAuditRecord],
) -> BacktestReport:
  """Aggregates simulated trades and account ledger into a complete BacktestReport."""
  total_trades = len(trades)
  wins = [t for t in trades if t.net_pnl > 0]
  losses = [t for t in trades if t.net_pnl <= 0]

  gross_profit = sum(t.gross_pnl for t in trades if t.gross_pnl > 0)
  gross_loss = abs(sum(t.gross_pnl for t in trades if t.gross_pnl < 0))
  net_profit = ledger.wallet_balance - ledger.initial_balance
  net_return_pct = (
      (net_profit / ledger.initial_balance * 100.0)
      if ledger.initial_balance > 0
      else 0.0
  )

  win_rate = (len(wins) / total_trades * 100.0) if total_trades > 0 else 0.0
  avg_win = (sum(t.net_pnl for t in wins) / len(wins)) if wins else 0.0
  avg_loss = (abs(sum(t.net_pnl for t in losses)) / len(losses)) if losses else 0.0
  profit_factor = (
      (gross_profit / gross_loss)
      if gross_loss > 0
      else (float("inf") if gross_profit > 0 else 0.0)
  )
  expectancy = (net_profit / total_trades) if total_trades > 0 else 0.0

  # Average achieved RR & holding time
  rr_values = []
  holding_times = []
  max_consec_wins = 0
  max_consec_losses = 0
  cur_wins = 0
  cur_losses = 0
  max_nom_risk = 0.0

  for t in trades:
    risk_dist = abs(t.fill_price - t.sl_price) * t.quantity
    if risk_dist > 0:
      rr_values.append(t.net_pnl / risk_dist)
      max_nom_risk = max(max_nom_risk, risk_dist)
    if t.exit_timestamp > t.fill_timestamp:
      holding_times.append(t.exit_timestamp - t.fill_timestamp)

    if t.net_pnl > 0:
      cur_wins += 1
      cur_losses = 0
      max_consec_wins = max(max_consec_wins, cur_wins)
    else:
      cur_losses += 1
      cur_wins = 0
      max_consec_losses = max(max_consec_losses, cur_losses)

  perf = PerformanceMetrics(
      total_trades=total_trades,
      winning_trades=len(wins),
      losing_trades=len(losses),
      win_rate=win_rate,
      gross_profit=gross_profit,
      gross_loss=gross_loss,
      net_profit=net_profit,
      net_return_pct=net_return_pct,
      average_win=avg_win,
      average_loss=avg_loss,
      average_rr_achieved=(sum(rr_values) / len(rr_values)) if rr_values else 0.0,
      profit_factor=profit_factor,
      expectancy=expectancy,
      average_holding_time_ms=(
          sum(holding_times) / len(holding_times) if holding_times else 0.0
      ),
      max_consecutive_wins=max_consec_wins,
      max_consecutive_losses=max_consec_losses,
  )

  largest_win = max((t.net_pnl for t in trades), default=0.0)
  largest_loss = min((t.net_pnl for t in trades), default=0.0)

  risk = RiskMetrics(
      max_drawdown_usdt=ledger.max_drawdown_usdt,
      max_drawdown_pct=ledger.max_drawdown_pct,
      max_account_exposure=ledger.max_account_exposure,
      max_nominal_risk_per_trade=max_nom_risk,
      largest_win=max(0.0, largest_win),
      largest_loss=min(0.0, largest_loss),
      liquidation_events=ledger.liquidation_events,
      max_margin_utilization=ledger.max_margin_utilization,
  )

  costs = CostMetrics(
      entry_fees=ledger.entry_fees,
      exit_fees=ledger.exit_fees,
      total_fees=ledger.entry_fees + ledger.exit_fees,
      funding_paid=ledger.funding_paid,
      estimated_spread_cost=ledger.spread_cost,
      estimated_slippage_cost=ledger.slippage_cost,
  )

  equity = EquityMetrics(
      starting_balance=ledger.initial_balance,
      ending_balance=ledger.wallet_balance,
      realized_equity_curve=list(ledger.equity_curve),
      daily_pnl=dict(ledger.daily_pnl),
      daily_trade_count=dict(ledger.daily_trade_count),
  )

  return BacktestReport(
      config=config,
      run_timestamp_ms=run_timestamp_ms,
      data_quality=data_quality,
      market_selection=market_selection,
      strategy=strategy,
      performance=perf,
      risk=risk,
      costs=costs,
      equity=equity,
      trades=trades,
  )
