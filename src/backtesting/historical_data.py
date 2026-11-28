"""Historical market data acquisition, persistent SQLite caching, and 15m aggregation.

Uses the existing BinanceFuturesClient public endpoints to download 1m klines
with pagination and resumable checkpoints, caches them in SQLite (WAL mode),
aggregates complete 15m candles from 1m candles, and populates HistoricalMarketStore.
"""

import asyncio
from dataclasses import dataclass, field
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from src.backtesting.data_adapter import HistoricalMarketStore
from src.backtesting.models import ContractMetadata
from src.exchange.client import BinanceFuturesClient
from src.exchange.models import (
    LeverageBracketTier,
    SymbolFilter,
    SymbolInfo,
    SymbolLeverageBrackets,
)
from src.market_data.models import Candle
from src.monitoring.logger import get_logger

logger = get_logger("backtesting.historical_data")

ONE_MINUTE_MS = 60_000
FIFTEEN_MINUTES_MS = 900_000
MAX_KLINES_PER_REQUEST = 1500

CACHE_SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS cached_contracts (
    symbol TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    base_asset TEXT NOT NULL,
    quote_asset TEXT NOT NULL,
    contract_type TEXT NOT NULL,
    filters_json TEXT NOT NULL,
    updated_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cached_klines_1m (
    symbol TEXT NOT NULL,
    open_time INTEGER NOT NULL,
    close_time INTEGER NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    quote_volume REAL NOT NULL,
    trades_count INTEGER NOT NULL,
    PRIMARY KEY (symbol, open_time)
);

CREATE INDEX IF NOT EXISTS idx_klines_1m_sym_time
    ON cached_klines_1m(symbol, open_time);

CREATE TABLE IF NOT EXISTS download_checkpoints (
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    range_start_ms INTEGER NOT NULL,
    range_end_ms INTEGER NOT NULL,
    last_fetched_open_time INTEGER NOT NULL,
    completed INTEGER NOT NULL DEFAULT 0,
    updated_at_ms INTEGER NOT NULL,
    PRIMARY KEY (symbol, interval, range_start_ms, range_end_ms)
);

CREATE TABLE IF NOT EXISTS backtest_runs (
    job_id TEXT PRIMARY KEY,
    strategy_name TEXT NOT NULL DEFAULT 'top_gainers_ha',
    status TEXT NOT NULL,
    progress_pct REAL NOT NULL DEFAULT 0.0,
    progress_message TEXT NOT NULL DEFAULT '',
    start_time_ms INTEGER NOT NULL,
    end_time_ms INTEGER NOT NULL,
    initial_balance REAL NOT NULL,
    started_at_ms INTEGER,
    completed_at_ms INTEGER,
    error TEXT,
    report_json TEXT
);
"""


@dataclass(frozen=True)
class MetadataProvenance:
  """Documents whether leverage/margin metadata is research-assumed or exchange-verified."""

  mode: str  # "RESEARCH_MODE" or "VERIFIED_METADATA"
  source: str
  retrieved_at_ms: int
  assumed_max_leverage: Optional[int] = None
  assumed_maint_margin_ratio: Optional[float] = None
  listing_limitation_note: str = (
      "Binance public exchangeInfo provides currently active contracts only; "
      "historically delisted contracts are not available via public REST."
  )

  def to_dict(self) -> Dict[str, Any]:
    return {
        "mode": self.mode,
        "source": self.source,
        "retrieved_at_ms": self.retrieved_at_ms,
        "assumed_max_leverage": self.assumed_max_leverage,
        "assumed_maint_margin_ratio": self.assumed_maint_margin_ratio,
        "listing_limitation_note": self.listing_limitation_note,
    }


def aggregate_1m_to_15m(candles_1m: List[Candle]) -> List[Candle]:
  """Aggregates complete 1m candles into complete 15m candles chronologically.

  Only emits a 15m candle when all 15 constituent 1m bars are present and closed,
  preventing incomplete-bar or lookahead artifacts.
  """
  if not candles_1m:
    return []

  buckets: Dict[int, List[Candle]] = {}
  for c in candles_1m:
    if not c.is_closed:
      continue
    bucket_open = (c.open_time // FIFTEEN_MINUTES_MS) * FIFTEEN_MINUTES_MS
    buckets.setdefault(bucket_open, []).append(c)

  result: List[Candle] = []
  for bucket_open in sorted(buckets.keys()):
    group = sorted(buckets[bucket_open], key=lambda x: x.open_time)
    # Require all 15 constituent 1-minute bars for a complete 15m candle
    if len(group) != 15:
      continue
    expected_times = [bucket_open + i * ONE_MINUTE_MS for i in range(15)]
    if [c.open_time for c in group] != expected_times:
      continue

    result.append(
        Candle(
            symbol=group[0].symbol,
            interval="15m",
            open_time=bucket_open,
            close_time=bucket_open + FIFTEEN_MINUTES_MS - 1,
            open=group[0].open,
            high=max(c.high for c in group),
            low=min(c.low for c in group),
            close=group[-1].close,
            volume=sum(c.volume for c in group),
            quote_volume=sum(c.quote_volume for c in group),
            trades_count=sum(c.trades_count for c in group),
            is_closed=True,
        )
    )
  return result


class HistoricalCacheDB:
  """Persistent SQLite cache for historical contracts, 1m klines, and download checkpoints."""

  def __init__(self, db_path: Path | str = "data/historical_cache.db"):
    self.db_path = Path(db_path) if isinstance(db_path, str) else db_path
    self._conn: Optional[sqlite3.Connection] = None

  def initialize(self) -> None:
    if self.db_path != Path(":memory:"):
      self.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = self.get_connection()
    conn.executescript(CACHE_SCHEMA_SQL)
    conn.commit()

  def get_connection(self) -> sqlite3.Connection:
    if self._conn is None:
      self._conn = sqlite3.connect(
          str(self.db_path),
          timeout=10.0,
          check_same_thread=False,
      )
      self._conn.row_factory = sqlite3.Row
    return self._conn

  def close(self) -> None:
    if self._conn is not None:
      try:
        self._conn.close()
      finally:
        self._conn = None

  def save_contracts(self, contracts: List[SymbolInfo]) -> None:
    now_ms = int(time.time() * 1000)
    conn = self.get_connection()
    with conn:
      for info in contracts:
        filters_payload = {
            "tick_size": info.filters.tick_size,
            "step_size": info.filters.step_size,
            "min_qty": info.filters.min_qty,
            "max_qty": info.filters.max_qty,
            "min_notional": info.filters.min_notional,
            "price_precision": info.filters.price_precision,
            "quantity_precision": info.filters.quantity_precision,
        }
        conn.execute(
            """
            INSERT INTO cached_contracts (
                symbol, status, base_asset, quote_asset, contract_type,
                filters_json, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                status = excluded.status,
                filters_json = excluded.filters_json,
                updated_at_ms = excluded.updated_at_ms;
            """,
            (
                info.symbol.upper(),
                info.status,
                info.base_asset,
                info.quote_asset,
                info.contract_type,
                json.dumps(filters_payload),
                now_ms,
            ),
        )

  def load_contracts(self) -> List[SymbolInfo]:
    conn = self.get_connection()
    rows = conn.execute(
        "SELECT * FROM cached_contracts ORDER BY symbol ASC;"
    ).fetchall()
    result: List[SymbolInfo] = []
    for r in rows:
      f = json.loads(r["filters_json"])
      sym_filter = SymbolFilter(
          symbol=r["symbol"],
          tick_size=float(f["tick_size"]),
          step_size=float(f["step_size"]),
          min_qty=float(f["min_qty"]),
          max_qty=float(f["max_qty"]),
          min_notional=float(f["min_notional"]),
          price_precision=int(f["price_precision"]),
          quantity_precision=int(f["quantity_precision"]),
      )
      result.append(
          SymbolInfo(
              symbol=r["symbol"],
              status=r["status"],
              base_asset=r["base_asset"],
              quote_asset=r["quote_asset"],
              contract_type=r["contract_type"],
              filters=sym_filter,
          )
      )
    return result

  def save_klines_1m(self, candles: List[Candle]) -> int:
    if not candles:
      return 0
    conn = self.get_connection()
    rows = [
        (
            c.symbol.upper(),
            c.open_time,
            c.close_time,
            c.open,
            c.high,
            c.low,
            c.close,
            c.volume,
            c.quote_volume,
            c.trades_count,
        )
        for c in candles
        if c.is_closed
    ]
    with conn:
      conn.executemany(
          """
          INSERT OR IGNORE INTO cached_klines_1m (
              symbol, open_time, close_time, open, high, low, close,
              volume, quote_volume, trades_count
          ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
          """,
          rows,
      )
    return len(rows)

  def load_klines_1m(
      self, symbol: str, start_ms: int, end_ms: int
  ) -> List[Candle]:
    conn = self.get_connection()
    rows = conn.execute(
        """
        SELECT * FROM cached_klines_1m
        WHERE symbol = ? AND open_time >= ? AND close_time <= ?
        ORDER BY open_time ASC;
        """,
        (symbol.upper(), start_ms, end_ms),
    ).fetchall()
    return [
        Candle(
            symbol=r["symbol"],
            interval="1m",
            open_time=int(r["open_time"]),
            close_time=int(r["close_time"]),
            open=float(r["open"]),
            high=float(r["high"]),
            low=float(r["low"]),
            close=float(r["close"]),
            volume=float(r["volume"]),
            quote_volume=float(r["quote_volume"]),
            trades_count=int(r["trades_count"]),
            is_closed=True,
        )
        for r in rows
    ]

  def get_checkpoint(
      self, symbol: str, interval: str, range_start_ms: int, range_end_ms: int
  ) -> Optional[Tuple[int, bool]]:
    conn = self.get_connection()
    row = conn.execute(
        """
        SELECT last_fetched_open_time, completed FROM download_checkpoints
        WHERE symbol = ? AND interval = ? AND range_start_ms = ? AND range_end_ms = ?;
        """,
        (symbol.upper(), interval, range_start_ms, range_end_ms),
    ).fetchone()
    if row is None:
      return None
    return int(row["last_fetched_open_time"]), bool(row["completed"])

  def upsert_checkpoint(
      self,
      symbol: str,
      interval: str,
      range_start_ms: int,
      range_end_ms: int,
      last_fetched_open_time: int,
      completed: bool,
  ) -> None:
    now_ms = int(time.time() * 1000)
    conn = self.get_connection()
    with conn:
      conn.execute(
          """
          INSERT INTO download_checkpoints (
              symbol, interval, range_start_ms, range_end_ms,
              last_fetched_open_time, completed, updated_at_ms
          ) VALUES (?, ?, ?, ?, ?, ?, ?)
          ON CONFLICT(symbol, interval, range_start_ms, range_end_ms) DO UPDATE SET
              last_fetched_open_time = excluded.last_fetched_open_time,
              completed = excluded.completed,
              updated_at_ms = excluded.updated_at_ms;
          """,
          (
              symbol.upper(),
              interval,
              range_start_ms,
              range_end_ms,
              last_fetched_open_time,
              1 if completed else 0,
              now_ms,
          ),
      )

  def get_missing_1m_ranges(
      self, symbol: str, start_ms: int, end_ms: int
  ) -> List[Tuple[int, int]]:
    """Returns uncached [sub_start, sub_end) intervals within [start_ms, end_ms).

    Subtracts both completed download_checkpoints and existing rows in
    cached_klines_1m so overlapping backtest ranges (e.g. 2 months after 1 month)
    never re-download already-fetched data.
    """
    sym = symbol.upper()
    aligned_start = (start_ms // ONE_MINUTE_MS) * ONE_MINUTE_MS
    aligned_end = (end_ms // ONE_MINUTE_MS) * ONE_MINUTE_MS
    if aligned_start >= aligned_end:
      return []

    conn = self.get_connection()

    # 1. Subtract any already-completed checkpoint ranges
    cp_rows = conn.execute(
        """
        SELECT range_start_ms, range_end_ms FROM download_checkpoints
        WHERE symbol = ? AND interval = '1m' AND completed = 1
          AND range_end_ms > ? AND range_start_ms < ?
        ORDER BY range_start_ms ASC;
        """,
        (sym, aligned_start, aligned_end),
    ).fetchall()

    uncovered: List[Tuple[int, int]] = [(aligned_start, aligned_end)]
    for row in cp_rows:
      cs = int(row["range_start_ms"])
      ce = int(row["range_end_ms"])
      next_uncovered: List[Tuple[int, int]] = []
      for us, ue in uncovered:
        if ce <= us or cs >= ue:
          next_uncovered.append((us, ue))
        else:
          if us < cs:
            next_uncovered.append((us, cs))
          if ce < ue:
            next_uncovered.append((ce, ue))
      uncovered = next_uncovered

    if not uncovered:
      return []

    # 2. Within remaining uncovered ranges, subtract any bars already in cached_klines_1m
    missing: List[Tuple[int, int]] = []
    for us, ue in uncovered:
      kline_rows = conn.execute(
          """
          SELECT open_time FROM cached_klines_1m
          WHERE symbol = ? AND open_time >= ? AND open_time < ?
          ORDER BY open_time ASC;
          """,
          (sym, us, ue),
      ).fetchall()
      expected = us
      for kr in kline_rows:
        ot = int(kr["open_time"])
        if ot > expected:
          missing.append((expected, ot))
        if ot >= expected:
          expected = ot + ONE_MINUTE_MS
      if expected < ue:
        missing.append((expected, ue))

    return missing

  def save_backtest_run(
      self,
      job_id: str,
      strategy_name: str,
      status: str,
      progress_pct: float,
      progress_message: str,
      start_time_ms: int,
      end_time_ms: int,
      initial_balance: float,
      started_at_ms: Optional[int],
      completed_at_ms: Optional[int] = None,
      error: Optional[str] = None,
      report_json: Optional[str] = None,
  ) -> None:
    """Persists a backtest job status and serialized report so UI reloads restore it."""
    conn = self.get_connection()
    with conn:
      conn.execute(
          """
          INSERT INTO backtest_runs (
              job_id, strategy_name, status, progress_pct, progress_message,
              start_time_ms, end_time_ms, initial_balance, started_at_ms,
              completed_at_ms, error, report_json
          ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
          ON CONFLICT(job_id) DO UPDATE SET
              status = excluded.status,
              progress_pct = excluded.progress_pct,
              progress_message = excluded.progress_message,
              completed_at_ms = excluded.completed_at_ms,
              error = excluded.error,
              report_json = COALESCE(excluded.report_json, backtest_runs.report_json);
          """,
          (
              job_id,
              strategy_name,
              status,
              progress_pct,
              progress_message,
              start_time_ms,
              end_time_ms,
              initial_balance,
              started_at_ms,
              completed_at_ms,
              error,
              report_json,
          ),
      )

  def get_latest_backtest_run(self) -> Optional[Dict[str, Any]]:
    """Retrieves the most recent persisted backtest run (including report_json if completed)."""
    conn = self.get_connection()
    row = conn.execute(
        "SELECT * FROM backtest_runs ORDER BY started_at_ms DESC LIMIT 1;"
    ).fetchone()
    if row is None:
      return None
    return {
        "job_id": row["job_id"],
        "strategy_name": row["strategy_name"],
        "status": row["status"],
        "progress_pct": float(row["progress_pct"]),
        "progress_message": row["progress_message"],
        "start_time_ms": int(row["start_time_ms"]),
        "end_time_ms": int(row["end_time_ms"]),
        "initial_balance": float(row["initial_balance"]),
        "started_at_ms": row["started_at_ms"],
        "completed_at_ms": row["completed_at_ms"],
        "error": row["error"],
        "report_json": row["report_json"],
    }


class HistoricalDataManager:
  """Downloads, caches, validates, and loads historical market data into HistoricalMarketStore."""

  def __init__(
      self,
      client: BinanceFuturesClient,
      cache_db: Optional[HistoricalCacheDB] = None,
      max_concurrency: int = 4,
  ):
    self.client = client
    self.cache = cache_db or HistoricalCacheDB()
    self.cache.initialize()
    self._semaphore = asyncio.Semaphore(max_concurrency)

  async def fetch_eligible_contracts(self) -> List[SymbolInfo]:
    """Fetches all active USDT Perpetual contracts from Binance public exchangeInfo."""
    info_map = await self.client.get_exchange_info()
    eligible = [
        info
        for info in info_map.values()
        if info.is_trading and info.is_usdt_perpetual
    ]
    eligible.sort(key=lambda s: s.symbol)
    self.cache.save_contracts(eligible)
    return eligible

  async def download_symbol_1m_range(
      self,
      symbol: str,
      start_ms: int,
      end_ms: int,
  ) -> int:
    """Downloads only missing 1m kline sub-ranges in [start_ms, end_ms] with pagination.

    Returns the number of newly persisted 1m candles.
    """
    sym = symbol.upper()
    aligned_start = (start_ms // ONE_MINUTE_MS) * ONE_MINUTE_MS
    aligned_end = (end_ms // ONE_MINUTE_MS) * ONE_MINUTE_MS

    missing_ranges = self.cache.get_missing_1m_ranges(
        sym, aligned_start, aligned_end
    )
    if not missing_ranges:
      return 0

    total_saved = 0
    now_ms = int(time.time() * 1000)

    for sub_start, sub_end in missing_ranges:
      cursor = sub_start
      while cursor < sub_end:
        async with self._semaphore:
          raw_klines = await self.client.get_klines(
              symbol=sym,
              interval="1m",
              limit=MAX_KLINES_PER_REQUEST,
              start_time=cursor,
              end_time=sub_end,
          )

        if not raw_klines:
          break

        parsed: List[Candle] = []
        last_open_time = cursor
        for raw in raw_klines:
          close_time = int(raw[6])
          is_closed = close_time < now_ms and close_time <= sub_end
          c = Candle.from_rest_kline(sym, "1m", raw, is_closed=is_closed)
          last_open_time = max(last_open_time, c.open_time)
          if c.is_closed:
            parsed.append(c)

        if parsed:
          total_saved += self.cache.save_klines_1m(parsed)

        self.cache.upsert_checkpoint(
            sym,
            "1m",
            sub_start,
            sub_end,
            last_open_time,
            completed=False,
        )

        if len(raw_klines) < MAX_KLINES_PER_REQUEST:
          break

        next_cursor = last_open_time + ONE_MINUTE_MS
        if next_cursor <= cursor:
          break
        cursor = next_cursor

      self.cache.upsert_checkpoint(
          sym,
          "1m",
          sub_start,
          sub_end,
          sub_end,
          completed=True,
      )

    self.cache.upsert_checkpoint(
        sym,
        "1m",
        aligned_start,
        aligned_end,
        aligned_end,
        completed=True,
    )
    return total_saved

  async def prepare_store(
      self,
      start_ms: int,
      end_ms: int,
      warmup_ms: int,
      metadata_mode: str = "RESEARCH_MODE",
      assumed_max_leverage: int = 20,
      assumed_maint_margin_ratio: float = 0.005,
      symbols_filter: Optional[List[str]] = None,
      progress_callback: Optional[Callable[[int, int, str], None]] = None,
  ) -> Tuple[HistoricalMarketStore, MetadataProvenance]:
    """Downloads/caches historical data and builds a validated HistoricalMarketStore."""
    fetch_start = max(0, start_ms - warmup_ms)
    contracts = await self.fetch_eligible_contracts()
    if symbols_filter:
      allowed = {s.upper() for s in symbols_filter}
      contracts = [c for c in contracts if c.symbol in allowed]

    # Resolve leverage brackets according to selected mode
    brackets_map: Dict[str, SymbolLeverageBrackets] = {}
    retrieved_at = int(time.time() * 1000)

    if metadata_mode == "VERIFIED_METADATA":
      brackets_map = await self.client.get_leverage_brackets()
      provenance = MetadataProvenance(
          mode="VERIFIED_METADATA",
          source=f"{self.client.base_url}/fapi/v1/leverageBracket",
          retrieved_at_ms=retrieved_at,
      )
    else:
      provenance = MetadataProvenance(
          mode="RESEARCH_MODE",
          source="Research assumption (unsigned public endpoints only)",
          retrieved_at_ms=retrieved_at,
          assumed_max_leverage=assumed_max_leverage,
          assumed_maint_margin_ratio=assumed_maint_margin_ratio,
      )

    total = len(contracts)
    for idx, info in enumerate(contracts, start=1):
      if progress_callback:
        progress_callback(idx - 1, total, info.symbol)
      await self.download_symbol_1m_range(info.symbol, fetch_start, end_ms)
      if progress_callback:
        progress_callback(idx, total, info.symbol)

    store = HistoricalMarketStore()
    for info in contracts:
      sym_brackets = brackets_map.get(info.symbol)
      if sym_brackets is None:
        sym_brackets = SymbolLeverageBrackets(
            symbol=info.symbol,
            brackets=[
                LeverageBracketTier(
                    bracket=1,
                    initial_leverage=assumed_max_leverage,
                    notional_cap=10_000_000.0,
                    notional_floor=0.0,
                    maint_margin_ratio=assumed_maint_margin_ratio,
                )
            ],
        )

      store.register_contract(
          ContractMetadata(
              symbol=info.symbol,
              status=info.status,
              base_asset=info.base_asset,
              quote_asset=info.quote_asset,
              contract_type=info.contract_type,
              filters=info.filters,
              brackets=sym_brackets,
              listed_at_ms=0,
              delisted_at_ms=None,
          )
      )

      candles_1m = self.cache.load_klines_1m(info.symbol, fetch_start, end_ms)
      if candles_1m:
        candles_15m = aggregate_1m_to_15m(candles_1m)
        store.ingest_candles(info.symbol, "1m", candles_1m)
        if candles_15m:
          store.ingest_candles(info.symbol, "15m", candles_15m)

    return store, provenance
