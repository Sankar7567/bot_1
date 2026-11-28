"""Early-Momentum Breakout Market Scanner (separate workflow from Top Gainers).

Reuses BinanceFuturesClient (live) or HistoricalExchangeAdapter (backtesting)
and the same contract/liquidity filters, but ranks eligible USDT perpetuals by
composite 15m early-momentum indicators (Volume Surge, ROC-9, ADX-14, RSI-14,
BB Squeeze) rather than 24h percentage gain.
"""

import asyncio
import time
from typing import Dict, List, Optional, Tuple
from src.config.settings import Settings
from src.exchange.client import BinanceFuturesClient
from src.exchange.exceptions import BinanceConnectorError
from src.indicators.momentum import MomentumScore, evaluate_early_momentum
from src.market_data.models import Candle
from src.monitoring.logger import get_logger
from src.scanner.exceptions import (
    EmptyUniverseError,
    ScannerAPIError,
    ScannerError,
    StaleDataError,
)
from src.scanner.filters import is_eligible_contract, is_eligible_ticker
from src.scanner.models import ScannedSymbol, ScanResult

logger = get_logger("scanner.momentum")

FIFTEEN_MINUTES_MS = 900_000


class MomentumScanner:
  """Scans eligible USDT perpetuals and ranks top N early-momentum candidates."""

  def __init__(
      self,
      client: BinanceFuturesClient,
      settings: Optional[Settings] = None,
      top_n: Optional[int] = None,
      min_quote_volume_24h: Optional[float] = None,
      quote_asset: Optional[str] = None,
      contract_type: Optional[str] = None,
      max_data_age_ms: int = 120000,
      max_concurrency: int = 8,
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
    self._semaphore = asyncio.Semaphore(max_concurrency)

    self._last_scan: Optional[ScanResult] = None
    # Cache last computed 15m bucket score per symbol to avoid redundant work
    self._last_15m_bucket: int = -1
    self._cached_scores: Dict[str, MomentumScore] = {}

  @property
  def last_scan(self) -> Optional[ScanResult]:
    return self._last_scan

  async def stop_periodic_scan(self) -> None:
    return None

  async def _score_symbol(self, symbol: str) -> Optional[MomentumScore]:
    try:
      async with self._semaphore:
        raw = await self.client.get_klines(symbol, "15m", limit=45)
      now_ms = self.client.get_synchronized_time()
      candles = [
          Candle.from_rest_kline(
              symbol, "15m", r, is_closed=(int(r[6]) <= now_ms)
          )
          for r in raw
      ]
      return evaluate_early_momentum(symbol, candles)
    except Exception as exc:
      logger.debug("Momentum scoring skipped for %s: %s", symbol, exc)
      return None

  async def scan(self) -> ScanResult:
    """Executes an early-momentum scan across eligible USDT perpetual contracts."""
    start_time = time.perf_counter()
    now_ms = self.client.get_synchronized_time()

    try:
      exchange_info, tickers = await asyncio.gather(
          self.client.get_exchange_info(),
          self.client.get_24hr_tickers(),
      )
    except BinanceConnectorError as err:
      raise ScannerAPIError(f"API failure during momentum scan: {err}") from err
    except Exception as exc:
      raise ScannerError(
          f"Unexpected error retrieving market data: {exc}"
      ) from exc

    if not exchange_info or not tickers:
      raise EmptyUniverseError("Exchange info or tickers returned empty.")

    latest_close_time = max(t.close_time for t in tickers)
    if (
        latest_close_time > 0
        and (now_ms - latest_close_time) > self.max_data_age_ms
    ):
      raise StaleDataError(
          f"Market ticker data is stale (lag={now_ms - latest_close_time}ms)."
      )

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

    if not eligible_items:
      raise EmptyUniverseError("No contracts met eligibility criteria.")

    # Re-evaluate 15m momentum indicators once per completed 15m bucket
    current_15m_bucket = now_ms // FIFTEEN_MINUTES_MS
    if current_15m_bucket != self._last_15m_bucket or not self._cached_scores:
      tasks = [self._score_symbol(t.symbol) for t, _ in eligible_items]
      scores = await asyncio.gather(*tasks)
      self._cached_scores = {
          s.symbol: s for s in scores if s is not None
      }
      self._last_15m_bucket = current_15m_bucket

    scored_items: List[Tuple[float, object, object]] = []
    for ticker, info in eligible_items:
      m = self._cached_scores.get(ticker.symbol)
      m_score = m.score if m is not None else 0.0
      scored_items.append((m_score, ticker, info))

    # Deterministic sort: highest momentum score, then quoteVolume, then symbol ASC
    scored_items.sort(
        key=lambda item: (
            -item[0],
            -item[1].quote_volume,
            item[1].symbol,
        )
    )

    selected = scored_items[: self.top_n]
    scanned_symbols: List[ScannedSymbol] = []
    for idx, (_, ticker, info) in enumerate(selected, start=1):
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
    result = ScanResult(
        scan_timestamp=now_ms,
        symbols=scanned_symbols,
        total_evaluated_count=len(tickers),
        total_eligible_count=len(eligible_items),
        scan_duration_ms=elapsed_ms,
    )
    self._last_scan = result
    logger.info(
        "Momentum scan completed in %.1fms. Selected %d early-momentum candidates from %d eligible.",
        elapsed_ms,
        len(scanned_symbols),
        len(eligible_items),
    )
    return result
