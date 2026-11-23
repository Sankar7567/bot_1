"""Market Scanner for Binance USDⓈ-M Futures.

Scans exchange contracts, filters for active USDT perpetuals, validates 24h
volume,
deterministically ranks gainers, and maintains the top 20 trading universe.
"""

import asyncio
import time
from typing import List, Optional
from src.config.settings import Settings
from src.exchange.client import BinanceFuturesClient
from src.exchange.exceptions import BinanceConnectorError
from src.monitoring.logger import get_logger
from src.scanner.exceptions import (
    EmptyUniverseError,
    ScannerAPIError,
    ScannerError,
    StaleDataError,
)
from src.scanner.filters import (
    deterministic_ranking_sort_key,
    is_eligible_contract,
    is_eligible_ticker,
)
from src.scanner.models import ScannedSymbol, ScanResult

logger = get_logger("scanner")


class MarketScanner:
  """Scans, filters, and ranks Binance Futures 24h gainers."""

  def __init__(
      self,
      client: BinanceFuturesClient,
      settings: Optional[Settings] = None,
      top_n: Optional[int] = None,
      min_quote_volume_24h: Optional[float] = None,
      quote_asset: Optional[str] = None,
      contract_type: Optional[str] = None,
      max_data_age_ms: int = 120000,  # 2 minutes maximum data age
  ):
    cfg = settings or Settings()
    self.client = client
    self.top_n = top_n if top_n is not None else cfg.scanner_top_n
    self.min_quote_volume_24h = (
        min_quote_volume_24h
        if min_quote_volume_24h is not None
        else cfg.scanner_min_quote_volume_24h
    )
    self.quote_asset = quote_asset or cfg.scanner_quote_asset
    self.contract_type = contract_type or cfg.scanner_contract_type
    self.max_data_age_ms = max_data_age_ms

    self._last_scan: Optional[ScanResult] = None
    self._periodic_task: Optional[asyncio.Task] = None
    self._stop_event = asyncio.Event()

  @property
  def last_scan(self) -> Optional[ScanResult]:
    """Returns the most recently completed scan result."""
    return self._last_scan

  async def scan(self) -> ScanResult:
    """Executes a complete market scan, filtering, ranking, and updating top N symbols."""
    start_time = time.perf_counter()
    now_ms = self.client.get_synchronized_time()

    try:
      # Step 1: Query exchange info and 24h tickers concurrently
      exchange_info_task = asyncio.create_task(self.client.get_exchange_info())
      tickers_task = asyncio.create_task(self.client.get_24hr_tickers())
      exchange_info, tickers = await asyncio.gather(
          exchange_info_task, tickers_task
      )
    except BinanceConnectorError as err:
      logger.error("Exchange API error during market scan: %s", err)
      raise ScannerAPIError(f"API failure during market scan: {err}") from err
    except Exception as exc:
      logger.error("Unexpected error during market scan: %s", exc)
      raise ScannerError(
          f"Unexpected error retrieving market data: {exc}"
      ) from exc

    # Step 2: Validate payload completeness
    if not exchange_info:
      raise EmptyUniverseError(
          "Exchange info returned empty; cannot establish market universe."
      )

    if not tickers:
      raise EmptyUniverseError(
          "Ticker 24hr statistics returned empty; cannot rank contracts."
      )

    # Step 3: Validate against stale data
    # Check if latest ticker closeTime is reasonably recent compared to current synchronized time
    latest_close_time = max(t.close_time for t in tickers)
    if (
        latest_close_time > 0
        and (now_ms - latest_close_time) > self.max_data_age_ms
    ):
      raise StaleDataError(
          f"Market ticker data is stale. Latest closeTime: {latest_close_time},"
          f" current: {now_ms} (lag: {now_ms - latest_close_time}ms >"
          f" {self.max_data_age_ms}ms)"
      )

    # Step 4: Apply contract eligibility and volume filters
    total_evaluated = len(tickers)
    eligible_items = []

    for ticker in tickers:
      info = exchange_info.get(ticker.symbol)
      if not info:
        continue

      if not is_eligible_contract(info, self.quote_asset, self.contract_type):
        continue

      if not is_eligible_ticker(ticker, self.min_quote_volume_24h):
        continue

      eligible_items.append((ticker, info))

    total_eligible = len(eligible_items)
    if total_eligible == 0:
      raise EmptyUniverseError(
          f"No contracts met eligibility criteria (Quote: {self.quote_asset},"
          f" Type: {self.contract_type}, MinVol: {self.min_quote_volume_24h})."
      )

    # Step 5: Deterministic ranking
    # Sort by: -priceChangePercent, -quoteVolume, symbol ASC
    eligible_items.sort(key=lambda item: deterministic_ranking_sort_key(item[0]))

    # Step 6: Select top N gainers
    selected_slice = eligible_items[: self.top_n]
    scanned_symbols: List[ScannedSymbol] = []

    for idx, (ticker, info) in enumerate(selected_slice, start=1):
      scanned_symbols.append(
          ScannedSymbol(
              rank=idx,
              symbol=ticker.symbol,
              price_change_percent=ticker.price_change_percent,
              quote_volume=ticker.quote_volume,
              last_price=ticker.last_price,
              base_asset=info.base_asset,
              quote_asset=info.quote_asset,
              contract_type=info.contract_type,
              scan_timestamp=now_ms,
          )
      )

    elapsed_ms = (time.perf_counter() - start_time) * 1000.0
    scan_result = ScanResult(
        scan_timestamp=now_ms,
        symbols=scanned_symbols,
        total_evaluated_count=total_evaluated,
        total_eligible_count=total_eligible,
        scan_duration_ms=elapsed_ms,
    )

    self._last_scan = scan_result
    logger.info(
        "Market scan completed in %.1fms. Identified %d top gainers from %d"
        " eligible contracts.",
        elapsed_ms,
        len(scanned_symbols),
        total_eligible,
    )

    return scan_result

  # --------------------------------------------------------------------------
  # Periodic Background Scanning
  # --------------------------------------------------------------------------

  async def _periodic_loop(self, interval_sec: int) -> None:
    """Internal loop executing scan periodically."""
    logger.info(
        "Starting periodic market scanner with %ds interval.", interval_sec
    )
    while not self._stop_event.is_set():
      try:
        await self.scan()
      except Exception as err:
        logger.warning(
            "Periodic scan encountered an error (will retry next interval):"
            " %s",
            err,
        )

      try:
        await asyncio.wait_for(self._stop_event.wait(), timeout=interval_sec)
      except asyncio.TimeoutError:
        pass

    logger.info("Periodic market scanner stopped.")

  def start_periodic_scan(self, interval_sec: int = 60) -> None:
    """Launches the periodic background scanning task."""
    if self._periodic_task and not self._periodic_task.done():
      logger.warning("Periodic scanner is already running.")
      return

    self._stop_event.clear()
    self._periodic_task = asyncio.create_task(
        self._periodic_loop(interval_sec), name="MarketScannerLoop"
    )

  async def stop_periodic_scan(self) -> None:
    """Gracefully halts the periodic background scanning task."""
    if self._periodic_task:
      self._stop_event.set()
      try:
        await asyncio.wait_for(self._periodic_task, timeout=5.0)
      except asyncio.TimeoutError:
        self._periodic_task.cancel()
      self._periodic_task = None
