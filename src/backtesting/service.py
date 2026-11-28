"""Background Backtest Service for the Trading Control Panel.

Coordinates asynchronous historical data acquisition, chronological backtesting
via ChronologicalBacktestRunner, duplicate job prevention, report serialization,
and CSV export of TradeAuditRecord entries.
"""

import asyncio
import csv
from dataclasses import asdict
import io
import math
import time
from typing import Any, Dict, List, Optional
import uuid
from src.backtesting.historical_data import (
    HistoricalCacheDB,
    HistoricalDataManager,
    MetadataProvenance,
)
from src.backtesting.models import BacktestConfig, BacktestReport, ExecutionAssumptions
from src.backtesting.runner import ChronologicalBacktestRunner
from src.config.settings import Settings
from src.exchange.client import BinanceFuturesClient
from src.monitoring.logger import get_logger

logger = get_logger("backtesting.service")


def _safe_float(val: float) -> float:
  if math.isinf(val) or math.isnan(val):
    return 0.0
  return val


def serialize_report(
    report: BacktestReport, provenance: Optional[MetadataProvenance] = None
) -> Dict[str, Any]:
  """Converts a BacktestReport into a JSON-safe dictionary for the UI."""
  perf = asdict(report.performance)
  perf["profit_factor"] = _safe_float(perf["profit_factor"])

  trades_list = [asdict(t) for t in report.trades]

  def _dir_stats(direction: str) -> Dict[str, Any]:
    sub = [t for t in report.trades if t.direction == direction]
    wins = [t for t in sub if t.net_pnl > 0]
    net = sum(t.net_pnl for t in sub)
    return {
        "trades": len(sub),
        "wins": len(wins),
        "losses": len(sub) - len(wins),
        "win_rate": (len(wins) / len(sub) * 100.0) if sub else 0.0,
        "net_pnl": net,
    }

  return {
      "run_timestamp_ms": report.run_timestamp_ms,
      "metadata": provenance.to_dict() if provenance else None,
      "direction_breakdown": {
          "LONG": _dir_stats("LONG"),
          "SHORT": _dir_stats("SHORT"),
      },
      "config": {
          "start_time_ms": report.config.start_time_ms,
          "end_time_ms": report.config.end_time_ms,
          "initial_balance": report.config.initial_balance,
          "scanner_refresh_interval_ms": report.config.scanner_refresh_interval_ms,
          "warmup_ms": report.config.warmup_ms,
          "data_source": report.config.data_source,
      },
      "data_quality": {
          "total_bars_checked": report.data_quality.total_bars_checked,
          "issues_count": len(report.data_quality.issues),
          "incomplete_24h_intervals": report.data_quality.incomplete_24h_intervals,
          "symbols_with_issues": sorted(report.data_quality.symbols_with_issues),
          "issues": [asdict(i) for i in report.data_quality.issues[:200]],
      },
      "market_selection": asdict(report.market_selection),
      "strategy": asdict(report.strategy),
      "performance": perf,
      "risk": asdict(report.risk),
      "costs": asdict(report.costs),
      "equity": asdict(report.equity),
      "trades": trades_list,
  }


def export_trades_csv(trades: List[Dict[str, Any]]) -> str:
  """Exports serialized TradeAuditRecord list to RFC 4180 CSV."""
  output = io.StringIO()
  fieldnames = [
      "trade_id",
      "symbol",
      "direction",
      "candidate_rank",
      "window_id",
      "trigger_15m_timestamp",
      "candidate_created_at",
      "confirmation_1m_timestamp",
      "order_submitted_at",
      "fill_timestamp",
      "fill_price",
      "sl_price",
      "tp_price",
      "quantity",
      "leverage",
      "isolated_margin",
      "exit_timestamp",
      "exit_fill_price",
      "exit_reason",
      "gross_pnl",
      "entry_fee",
      "exit_fee",
      "funding_paid",
      "spread_cost",
      "slippage_cost",
      "net_pnl",
      "equity_after_trade",
  ]
  writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
  writer.writeheader()
  for t in trades:
    writer.writerow(t)
  return output.getvalue()


class BacktestService:
  """Manages background backtest execution with duplicate-job protection."""

  def __init__(
      self,
      settings: Optional[Settings] = None,
      cache_db: Optional[HistoricalCacheDB] = None,
  ):
    self.settings = settings or Settings()
    self.cache_db = cache_db or HistoricalCacheDB()
    self.cache_db.initialize()

    self._lock = asyncio.Lock()
    self._current_task: Optional[asyncio.Task] = None
    self._status: Dict[str, Any] = {
        "job_id": None,
        "status": "idle",  # idle | downloading | running | completed | failed
        "progress_pct": 0.0,
        "progress_message": "No backtest started yet.",
        "current_symbol": None,
        "symbols_done": 0,
        "symbols_total": 0,
        "started_at_ms": None,
        "completed_at_ms": None,
        "error": None,
    }
    self._last_report: Optional[Dict[str, Any]] = None

  def is_running(self) -> bool:
    return self._current_task is not None and not self._current_task.done()

  def get_status(self) -> Dict[str, Any]:
    return dict(self._status)

  def get_last_report(self) -> Optional[Dict[str, Any]]:
    return self._last_report

  async def start_backtest(
      self,
      start_time_ms: int,
      end_time_ms: int,
      initial_balance: float = 100.0,
      metadata_mode: str = "RESEARCH_MODE",
      assumed_max_leverage: int = 20,
      assumed_maint_margin_ratio: float = 0.005,
      symbols_filter: Optional[List[str]] = None,
      client: Optional[BinanceFuturesClient] = None,
  ) -> str:
    """Starts an asynchronous backtest job. Rejects duplicate concurrent runs."""
    if start_time_ms >= end_time_ms:
      raise ValueError("start_time_ms must be strictly less than end_time_ms.")
    if initial_balance <= 0:
      raise ValueError("initial_balance must be positive.")
    if metadata_mode == "VERIFIED_METADATA" and (
        not self.settings.binance_api_key.get_secret_value()
        or not self.settings.binance_api_secret.get_secret_value()
    ):
      raise ValueError(
          "Verified Metadata Mode requires configured Binance API credentials."
      )

    async with self._lock:
      if self.is_running():
        raise RuntimeError("A backtest job is already in progress.")

      job_id = f"bt_{uuid.uuid4().hex[:10]}"
      self._status = {
          "job_id": job_id,
          "status": "downloading",
          "progress_pct": 0.0,
          "progress_message": "Initializing historical data download...",
          "current_symbol": None,
          "symbols_done": 0,
          "symbols_total": 0,
          "started_at_ms": int(time.time() * 1000),
          "completed_at_ms": None,
          "error": None,
      }
      self._current_task = asyncio.create_task(
          self._run_job(
              job_id=job_id,
              start_time_ms=start_time_ms,
              end_time_ms=end_time_ms,
              initial_balance=initial_balance,
              metadata_mode=metadata_mode,
              assumed_max_leverage=assumed_max_leverage,
              assumed_maint_margin_ratio=assumed_maint_margin_ratio,
              symbols_filter=symbols_filter,
              external_client=client,
          ),
          name=f"BacktestJob-{job_id}",
      )
      return job_id

  async def _run_job(
      self,
      job_id: str,
      start_time_ms: int,
      end_time_ms: int,
      initial_balance: float,
      metadata_mode: str,
      assumed_max_leverage: int,
      assumed_maint_margin_ratio: float,
      symbols_filter: Optional[List[str]],
      external_client: Optional[BinanceFuturesClient],
  ) -> None:
    owns_client = external_client is None
    client = external_client or BinanceFuturesClient(self.settings)
    try:
      data_mgr = HistoricalDataManager(client=client, cache_db=self.cache_db)

      def _on_progress(done: int, total: int, sym: str) -> None:
        pct = round((done / total * 70.0), 1) if total > 0 else 0.0
        self._status.update({
            "status": "downloading",
            "progress_pct": pct,
            "progress_message": f"Downloading/verifying {sym} ({done}/{total})...",
            "current_symbol": sym,
            "symbols_done": done,
            "symbols_total": total,
        })

      config = BacktestConfig(
          start_time_ms=start_time_ms,
          end_time_ms=end_time_ms,
          initial_balance=initial_balance,
          execution=ExecutionAssumptions(
              taker_fee_rate=self.settings.estimated_taker_fee_ratio,
          ),
      )

      store, provenance = await data_mgr.prepare_store(
          start_ms=start_time_ms,
          end_ms=end_time_ms,
          warmup_ms=config.warmup_ms,
          metadata_mode=metadata_mode,
          assumed_max_leverage=assumed_max_leverage,
          assumed_maint_margin_ratio=assumed_maint_margin_ratio,
          symbols_filter=symbols_filter,
          progress_callback=_on_progress,
      )

      self._status.update({
          "status": "running",
          "progress_pct": 75.0,
          "progress_message": "Running chronological simulation...",
          "current_symbol": None,
      })

      runner = ChronologicalBacktestRunner(
          store=store,
          config=config,
          settings=self.settings,
      )
      report = await runner.run()
      self._last_report = serialize_report(report, provenance=provenance)

      self._status.update({
          "status": "completed",
          "progress_pct": 100.0,
          "progress_message": (
              f"Completed with {report.performance.total_trades} trades."
          ),
          "completed_at_ms": int(time.time() * 1000),
      })
      logger.info(
          "Backtest %s completed: trades=%d, net_pnl=%.4f",
          job_id,
          report.performance.total_trades,
          report.performance.net_profit,
      )
    except Exception as exc:
      logger.error("Backtest %s failed: %s", job_id, exc, exc_info=True)
      self._status.update({
          "status": "failed",
          "progress_message": f"Backtest failed: {exc}",
          "completed_at_ms": int(time.time() * 1000),
          "error": str(exc),
      })
    finally:
      if owns_client:
        await client.close()
