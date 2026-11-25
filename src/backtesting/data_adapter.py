"""Historical market data store, quality validator, and rolling 24h ticker reconstructor."""

from bisect import bisect_right
from typing import Dict, List, Optional
from src.backtesting.models import (
    ContractMetadata,
    DataQualityIssue,
    DataQualityReport,
)
from src.exchange.models import SymbolInfo, Ticker24hr
from src.market_data.models import Candle, interval_to_ms


class HistoricalMarketStore:
  """Holds historical OHLCV bars and contract metadata with strict non-lookahead access."""

  def __init__(self):
    self._contracts: Dict[str, ContractMetadata] = {}
    # symbol -> chronological List[Candle]
    self._candles_1m: Dict[str, List[Candle]] = {}
    self._close_times_1m: Dict[str, List[int]] = {}
    self._candles_15m: Dict[str, List[Candle]] = {}
    self._close_times_15m: Dict[str, List[int]] = {}
    # Optional sub-1m higher-resolution candles for same-bar SL/TP disambiguation
    self._sub_candles: Dict[str, List[Candle]] = {}
    self.quality_report = DataQualityReport()

  def register_contract(self, meta: ContractMetadata) -> None:
    """Registers historical contract metadata and listing/delisting boundaries."""
    self._contracts[meta.symbol.upper()] = meta

  def ingest_candles(
      self,
      symbol: str,
      interval: str,
      candles: List[Candle],
      sub_candles: Optional[List[Candle]] = None,
  ) -> None:
    """Validates quality and ingests historical candles chronologically without interpolation."""
    sym = symbol.upper()
    interval_ms = interval_to_ms(interval)
    seen_open_times = set()
    clean: List[Candle] = []
    prev_open: Optional[int] = None

    for c in candles:
      self.quality_report.total_bars_checked += 1

      # Check out-of-order
      if prev_open is not None and c.open_time < prev_open:
        self.quality_report.add_issue(
            DataQualityIssue(
                timestamp_ms=c.open_time,
                symbol=sym,
                interval=interval,
                issue_type="OUT_OF_ORDER",
                details=f"open_time {c.open_time} < previous {prev_open}",
            )
        )
        continue

      # Check duplicate
      if c.open_time in seen_open_times:
        self.quality_report.add_issue(
            DataQualityIssue(
                timestamp_ms=c.open_time,
                symbol=sym,
                interval=interval,
                issue_type="DUPLICATE_CANDLE",
                details=f"Duplicate candle at open_time={c.open_time}",
            )
        )
        continue

      # Check incomplete candle
      if not c.is_closed:
        self.quality_report.add_issue(
            DataQualityIssue(
                timestamp_ms=c.open_time,
                symbol=sym,
                interval=interval,
                issue_type="INCOMPLETE_CANDLE",
                details="Unclosed candle excluded from historical store.",
            )
        )
        continue

      # Validate OHLC integrity
      if (
          c.low <= 0
          or c.high < max(c.open, c.close)
          or c.low > min(c.open, c.close)
          or c.high < c.low
      ):
        self.quality_report.add_issue(
            DataQualityIssue(
                timestamp_ms=c.open_time,
                symbol=sym,
                interval=interval,
                issue_type="INVALID_OHLC",
                details=f"Invalid OHLC: O={c.open} H={c.high} L={c.low} C={c.close}",
            )
        )
        continue

      # Flag zero/negative volume
      if c.volume <= 0:
        self.quality_report.add_issue(
            DataQualityIssue(
                timestamp_ms=c.open_time,
                symbol=sym,
                interval=interval,
                issue_type="MISSING_VOLUME",
                details=f"Non-positive volume {c.volume}",
            )
        )

      # Detect missing gap
      if prev_open is not None and (c.open_time - prev_open) > interval_ms:
        self.quality_report.add_issue(
            DataQualityIssue(
                timestamp_ms=c.open_time,
                symbol=sym,
                interval=interval,
                issue_type="MISSING_CANDLE",
                details=(
                    f"Gap of {(c.open_time - prev_open) // interval_ms - 1} bars"
                    f" before {c.open_time}"
                ),
            )
        )

      seen_open_times.add(c.open_time)
      prev_open = c.open_time
      clean.append(c)

    if interval == "1m":
      self._candles_1m[sym] = clean
      self._close_times_1m[sym] = [c.close_time for c in clean]
    elif interval == "15m":
      self._candles_15m[sym] = clean
      self._close_times_15m[sym] = [c.close_time for c in clean]
    else:
      raise ValueError(f"Unsupported historical interval: {interval}")

    if sub_candles:
      self._sub_candles[sym] = sorted(sub_candles, key=lambda x: x.open_time)

  def get_contract(self, symbol: str) -> Optional[ContractMetadata]:
    return self._contracts.get(symbol.upper())

  def get_all_symbols(self) -> List[str]:
    return sorted(self._contracts.keys())

  def get_exchange_info_at(self, timestamp_ms: int) -> Dict[str, SymbolInfo]:
    """Returns only contracts listed and active at timestamp_ms (no survivorship bias)."""
    info_map: Dict[str, SymbolInfo] = {}
    for sym, meta in self._contracts.items():
      if meta.is_listed_at(timestamp_ms):
        info_map[sym] = SymbolInfo(
            symbol=meta.symbol,
            status=meta.status,
            base_asset=meta.base_asset,
            quote_asset=meta.quote_asset,
            contract_type=meta.contract_type,
            filters=meta.filters,
        )
    return info_map

  def get_klines_up_to(
      self,
      symbol: str,
      interval: str,
      up_to_ms: int,
      limit: int = 500,
  ) -> List[Candle]:
    """Strictly returns completed candles with close_time <= up_to_ms."""
    sym = symbol.upper()
    if interval == "1m":
      candles = self._candles_1m.get(sym, [])
      close_times = self._close_times_1m.get(sym, [])
    elif interval == "15m":
      candles = self._candles_15m.get(sym, [])
      close_times = self._close_times_15m.get(sym, [])
    else:
      return []

    idx = bisect_right(close_times, up_to_ms)
    start = max(0, idx - limit)
    return candles[start:idx]

  def get_next_1m_candle_after(
      self, symbol: str, timestamp_ms: int
  ) -> Optional[Candle]:
    """Returns the earliest 1m candle whose open_time > timestamp_ms for fill simulation."""
    sym = symbol.upper()
    candles = self._candles_1m.get(sym, [])
    for c in candles:
      if c.open_time > timestamp_ms:
        return c
    return None

  def get_sub_candles_for_bar(
      self, symbol: str, bar_open_ms: int, bar_close_ms: int
  ) -> List[Candle]:
    """Returns higher-resolution candles inside [bar_open_ms, bar_close_ms] if available."""
    subs = self._sub_candles.get(symbol.upper(), [])
    return [
        c for c in subs if c.open_time >= bar_open_ms and c.close_time <= bar_close_ms
    ]

  def get_all_event_timestamps(
      self, start_ms: int, end_ms: int
  ) -> List[int]:
    """Collects all unique 1m and 15m close timestamps within [start_ms, end_ms]."""
    ts_set = set()
    for close_list in self._close_times_1m.values():
      for t in close_list:
        if start_ms <= t <= end_ms:
          ts_set.add(t)
    for close_list in self._close_times_15m.values():
      for t in close_list:
        if start_ms <= t <= end_ms:
          ts_set.add(t)
    return sorted(ts_set)

  def reconstruct_24h_tickers(
      self,
      at_ms: int,
      min_coverage_ratio: float = 0.80,
  ) -> List[Ticker24hr]:
    """Reconstructs rolling 24-hour Ticker24hr stats strictly from data <= at_ms.

    Formula:
      gain% = ((close(T) - open(T - 24h)) / open(T - 24h)) * 100
      quote_volume = sum(quote_volume for completed 1m bars in (T - 24h, T])
    """
    lookback_start = at_ms - 86_400_000
    tickers: List[Ticker24hr] = []

    for sym, meta in self._contracts.items():
      if not meta.is_listed_at(at_ms):
        continue

      candles = self._candles_1m.get(sym, [])
      close_times = self._close_times_1m.get(sym, [])
      if not candles:
        continue

      end_idx = bisect_right(close_times, at_ms)
      if end_idx == 0:
        continue

      # Collect bars in rolling 24h window: open_time >= at_ms - 86,400,000 and close_time <= at_ms
      window_bars: List[Candle] = []
      for i in range(end_idx - 1, -1, -1):
        c = candles[i]
        if c.open_time < lookback_start:
          break
        window_bars.append(c)

      window_bars.reverse()
      coverage = len(window_bars) / 1440.0
      if not window_bars or coverage < min_coverage_ratio:
        self.quality_report.incomplete_24h_intervals += 1
        continue

      base_open = window_bars[0].open
      last_close = window_bars[-1].close
      if base_open <= 0 or last_close <= 0:
        continue

      gain_pct = ((last_close - base_open) / base_open) * 100.0
      price_chg = last_close - base_open
      high_24 = max(b.high for b in window_bars)
      low_24 = min(b.low for b in window_bars)
      vol_24 = sum(b.volume for b in window_bars)
      qvol_24 = sum(b.quote_volume for b in window_bars)

      tickers.append(
          Ticker24hr(
              symbol=sym,
              price_change=price_chg,
              price_change_percent=gain_pct,
              weighted_avg_price=qvol_24 / vol_24 if vol_24 > 0 else last_close,
              last_price=last_close,
              last_qty=window_bars[-1].volume,
              open_price=base_open,
              high_price=high_24,
              low_price=low_24,
              volume=vol_24,
              quote_volume=qvol_24,
              open_time=window_bars[0].open_time,
              close_time=window_bars[-1].close_time,
              first_id=1,
              last_id=len(window_bars),
              count=len(window_bars),
          )
      )

    return tickers
