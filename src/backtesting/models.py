"""Data models and schemas for Chronological Backtesting & Replay (Amendment 01)."""

from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional, Set, Tuple
from src.exchange.models import SymbolFilter, SymbolLeverageBrackets


@dataclass(frozen=True)
class ContractMetadata:
  """Historical contract specification with listing/delisting boundaries."""

  symbol: str
  status: str
  base_asset: str
  quote_asset: str
  contract_type: str
  filters: SymbolFilter
  brackets: SymbolLeverageBrackets
  listed_at_ms: int = 0
  delisted_at_ms: Optional[int] = None

  def is_listed_at(self, timestamp_ms: int) -> bool:
    """Returns True if the contract was listed and not delisted at timestamp_ms."""
    if timestamp_ms < self.listed_at_ms:
      return False
    if self.delisted_at_ms is not None and timestamp_ms >= self.delisted_at_ms:
      return False
    return True


@dataclass(frozen=True)
class ExecutionAssumptions:
  """Conservative execution, cost, and ambiguity assumptions for replay."""

  signal_to_order_latency_ms: int = 200
  half_spread_bps: float = 1.0  # 0.01%
  entry_slippage_bps: float = 5.0  # 0.05%
  stop_slippage_bps: float = 10.0  # 0.10%
  tp_slippage_bps: float = 0.0
  taker_fee_rate: float = 0.0005  # 0.05%
  funding_rate_8h: float = 0.0001  # 0.01% per 8h funding interval
  ambiguity_resolution: Literal["CONSERVATIVE_SL_FIRST"] = "CONSERVATIVE_SL_FIRST"


@dataclass(frozen=True)
class BacktestConfig:
  """Configuration and provenance parameters for a deterministic backtest run."""

  start_time_ms: int
  end_time_ms: int
  initial_balance: float = 1000.0
  scanner_refresh_interval_ms: int = 60_000
  warmup_ms: int = 86_400_000 + 3_600_000  # 24h rolling + 1h indicator warm-up
  min_24h_coverage_ratio: float = 0.80
  execution: ExecutionAssumptions = field(default_factory=ExecutionAssumptions)
  random_seed: int = 42
  data_source: str = "historical_klines"
  data_version: str = "1.0"
  software_version: str = "0.12.0"


@dataclass(frozen=True)
class DataQualityIssue:
  """Represents a detected historical data anomaly."""

  timestamp_ms: int
  symbol: str
  interval: str
  issue_type: str
  details: str


@dataclass
class DataQualityReport:
  """Aggregated data quality diagnostics across the historical dataset."""

  total_bars_checked: int = 0
  issues: List[DataQualityIssue] = field(default_factory=list)
  incomplete_24h_intervals: int = 0
  symbols_with_issues: Set[str] = field(default_factory=set)

  def add_issue(self, issue: DataQualityIssue) -> None:
    self.issues.append(issue)
    self.symbols_with_issues.add(issue.symbol)


@dataclass
class TradeAuditRecord:
  """Full chronological audit trail for a single executed backtest trade."""

  trade_id: str
  symbol: str
  direction: str
  candidate_rank: int
  window_id: str
  snapshot_symbols: List[str]
  trigger_15m_timestamp: int
  trigger_15m_ha_open: float
  trigger_15m_ha_close: float
  candidate_created_at: int
  monitored_1m_bars_count: int
  confirmation_1m_timestamp: int
  tie_break_candidates: List[str]
  pivot_price: float
  pivot_timestamp: int
  pivot_confirmed_at: int
  signal_price: float
  order_submitted_at: int
  fill_timestamp: int
  fill_price: float
  quantity: float
  leverage: int
  isolated_margin: float
  sl_price: float
  tp_price: float
  exit_timestamp: int = 0
  exit_reason: str = ""
  exit_trigger_price: float = 0.0
  exit_fill_price: float = 0.0
  ambiguity_resolution_note: str = ""
  gross_pnl: float = 0.0
  entry_fee: float = 0.0
  exit_fee: float = 0.0
  funding_paid: float = 0.0
  spread_cost: float = 0.0
  slippage_cost: float = 0.0
  net_pnl: float = 0.0
  equity_after_trade: float = 0.0


@dataclass
class MarketSelectionMetrics:
  eligible_contracts_count: int = 0
  historical_universe_size: int = 0
  top_20_appearances: Dict[str, int] = field(default_factory=dict)
  candidate_counts_by_symbol: Dict[str, int] = field(default_factory=dict)
  candidate_counts_by_direction: Dict[str, int] = field(
      default_factory=lambda: {"LONG": 0, "SHORT": 0}
  )


@dataclass
class StrategyMetrics:
  total_signals: int = 0
  confirmed_signals: int = 0
  rejected_signals: int = 0
  expired_candidates: int = 0
  risk_validation_failures: int = 0
  tie_breaking_events: int = 0
  trades_skipped_open_position: int = 0


@dataclass
class PerformanceMetrics:
  total_trades: int = 0
  winning_trades: int = 0
  losing_trades: int = 0
  win_rate: float = 0.0
  gross_profit: float = 0.0
  gross_loss: float = 0.0
  net_profit: float = 0.0
  net_return_pct: float = 0.0
  average_win: float = 0.0
  average_loss: float = 0.0
  average_rr_achieved: float = 0.0
  profit_factor: float = 0.0
  expectancy: float = 0.0
  average_holding_time_ms: float = 0.0
  max_consecutive_wins: int = 0
  max_consecutive_losses: int = 0


@dataclass
class RiskMetrics:
  max_drawdown_usdt: float = 0.0
  max_drawdown_pct: float = 0.0
  max_account_exposure: float = 0.0
  max_nominal_risk_per_trade: float = 0.0
  largest_win: float = 0.0
  largest_loss: float = 0.0
  liquidation_events: int = 0
  max_margin_utilization: float = 0.0


@dataclass
class CostMetrics:
  entry_fees: float = 0.0
  exit_fees: float = 0.0
  total_fees: float = 0.0
  funding_paid: float = 0.0
  estimated_spread_cost: float = 0.0
  estimated_slippage_cost: float = 0.0


@dataclass
class EquityMetrics:
  starting_balance: float = 0.0
  ending_balance: float = 0.0
  realized_equity_curve: List[Tuple[int, float]] = field(default_factory=list)
  daily_pnl: Dict[str, float] = field(default_factory=dict)
  daily_trade_count: Dict[str, int] = field(default_factory=dict)


@dataclass
class BacktestReport:
  """Complete reproducible backtest result and audit bundle."""

  config: BacktestConfig
  run_timestamp_ms: int
  data_quality: DataQualityReport
  market_selection: MarketSelectionMetrics
  strategy: StrategyMetrics
  performance: PerformanceMetrics
  risk: RiskMetrics
  costs: CostMetrics
  equity: EquityMetrics
  trades: List[TradeAuditRecord] = field(default_factory=list)
