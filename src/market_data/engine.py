"""Market Data Engine managing 1-minute and 15-minute Binance Futures candles.

Coordinates historical REST pre-seeding, WebSocket live kline ingestion,
closed-candle detection, chronological ring buffers, gap detection, and
event dispatching across monitored candidate symbols.
"""

import asyncio
import time
from typing import Callable, Coroutine, Dict, List, Optional, Tuple
from src.config.settings import Settings
from src.exchange.client import BinanceFuturesClient
from src.market_data.candle_buffer import CandleBuffer
from src.market_data.exceptions import CandleDataError
from src.market_data.feed import BinanceKlineWebSocket
from src.market_data.models import Candle, CandleCloseEvent, interval_to_ms
from src.monitoring.logger import get_logger

logger = get_logger("market_data.engine")


class MarketDataEngine:
  """Central market data engine managing 1m and 15m candle history and streaming."""

  def __init__(
      self,
      client: BinanceFuturesClient,
      settings: Optional[Settings] = None,
      ws_client: Optional[BinanceKlineWebSocket] = None,
      buffer_capacity: int = 300,
  ):
    self.client = client
    self.settings = settings or Settings()
    self.buffer_capacity = buffer_capacity

    # Map of (symbol, interval) -> CandleBuffer
    self._buffers: Dict[Tuple[str, str], CandleBuffer] = {}
    self._event_listeners: List[Callable[[CandleCloseEvent], Coroutine]] = []

    # WebSocket Stream Handler
    self.ws = ws_client or BinanceKlineWebSocket(
        ws_base_url=self.settings.binance_ws_base_url
    )
    self.ws.register_handler(self._on_ws_kline)
    self.ws.register_reconnect_callback(self._on_ws_reconnect)

  def subscribe_candle_closed(
      self, listener: Callable[[CandleCloseEvent], Coroutine]
  ) -> None:
    """Registers an asynchronous listener invoked strictly when a candle closes."""
    self._event_listeners.append(listener)

  def get_buffer(self, symbol: str, interval: str) -> CandleBuffer:
    """Returns or lazily creates a CandleBuffer for the symbol and interval."""
    key = (symbol.upper(), interval)
    if key not in self._buffers:
      self._buffers[key] = CandleBuffer(
          symbol=symbol,
          interval=interval,
          max_capacity=self.buffer_capacity,
          strict_closed=True,
      )
    return self._buffers[key]

  def get_candles(
      self, symbol: str, interval: str, n: Optional[int] = None
  ) -> List[Candle]:
    """Retrieves the latest n closed candles in chronological order."""
    buf = self.get_buffer(symbol, interval)
    return buf.get_candles(n)

  def get_latest_closed_candle(
      self, symbol: str, interval: str
  ) -> Optional[Candle]:
    """Retrieves the most recent closed candle for a symbol and interval."""
    buf = self.get_buffer(symbol, interval)
    return buf.get_latest_candle()

  def is_data_stale(
      self,
      symbol: str,
      interval: str,
      max_staleness_ms: Optional[int] = None,
  ) -> bool:
    """Checks whether the latest closed candle lags significantly behind exchange time.

    Default allowed staleness is 2.5 intervals.
    """
    latest = self.get_latest_closed_candle(symbol, interval)
    if not latest:
      return True

    now_ms = self.client.get_synchronized_time()
    interval_ms = interval_to_ms(interval)
    threshold = (
        max_staleness_ms
        if max_staleness_ms is not None
        else int(interval_ms * 2.5)
    )

    return (now_ms - latest.close_time) > threshold

  # --------------------------------------------------------------------------
  # Historical Pre-Seeding & REST Reconciliation
  # --------------------------------------------------------------------------

  async def seed_symbol_history(
      self,
      symbol: str,
      intervals: Tuple[str, ...] = ("1m", "15m"),
      limits: Dict[str, int] = {"1m": 120, "15m": 50},
  ) -> None:
    """Fetches historical closed klines via REST and pre-seeds the buffers."""
    now_ms = self.client.get_synchronized_time()

    for interval in intervals:
      limit = limits.get(interval, 100)
      raw_klines = await self.client.get_klines(
          symbol=symbol, interval=interval, limit=limit
      )
      buf = self.get_buffer(symbol, interval)

      for item in raw_klines:
        close_time = int(item[6])
        # Only treat candle as closed if close_time <= current time
        is_closed = close_time <= now_ms
        if is_closed:
          candle = Candle.from_rest_kline(
              symbol=symbol,
              interval=interval,
              raw=item,
              is_closed=True,
          )
          buf.add_candle(candle)

      logger.info(
          "Pre-seeded %d historical %s candles for %s",
          len(buf),
          interval,
          symbol,
      )

  async def reconcile_rest_snapshot(
      self, symbol: str, interval: str, limit: int = 50
  ) -> int:
    """Fetches latest REST klines to backfill any missed closed candles."""
    now_ms = self.client.get_synchronized_time()
    raw_klines = await self.client.get_klines(
        symbol=symbol, interval=interval, limit=limit
    )
    buf = self.get_buffer(symbol, interval)
    new_added = 0

    for item in raw_klines:
      close_time = int(item[6])
      if close_time <= now_ms:
        candle = Candle.from_rest_kline(
            symbol=symbol, interval=interval, raw=item, is_closed=True
        )
        if buf.add_candle(candle):
          new_added += 1

    if new_added > 0:
      logger.info(
          "Reconciled %d missing %s candles for %s", new_added, interval, symbol
      )
    return new_added

  # --------------------------------------------------------------------------
  # Live Streaming & Event Ingestion
  # --------------------------------------------------------------------------

  async def process_kline_payload(
      self, payload: dict
  ) -> Optional[CandleCloseEvent]:
    """Ingests a WebSocket kline event.

    Strictly discards unfinished candles and emits CandleCloseEvent only when
    kline.x is True.
    """
    try:
      candle = Candle.from_ws_payload(payload)
    except CandleDataError as parse_err:
      logger.warning("Skipping invalid kline payload: %s", parse_err)
      return None

    # CRITICAL: Discard unfinished candles
    if not candle.is_closed:
      return None

    buf = self.get_buffer(candle.symbol, candle.interval)
    is_new = buf.add_candle(candle)

    # Emit event only if newly closed candle accepted
    if is_new:
      now_ms = self.client.get_synchronized_time()
      event = CandleCloseEvent(
          symbol=candle.symbol,
          interval=candle.interval,
          candle=candle,
          emitted_at=now_ms,
      )

      # Notify all registered listeners concurrently
      for listener in self._event_listeners:
        try:
          await listener(event)
        except Exception as l_err:
          logger.error(
              "Error in candle close listener for %s (%s): %s",
              candle.symbol,
              candle.interval,
              l_err,
          )

      return event

    return None

  async def _on_ws_kline(self, payload: dict) -> None:
    """Internal WebSocket message handler."""
    await self.process_kline_payload(payload)

  async def _on_ws_reconnect(self) -> None:
    """Internal hook executed upon WebSocket reconnect; reconciles snapshots."""
    logger.info(
        "WebSocket reconnected. Triggering REST reconciliation for monitored"
        " symbols..."
    )
    symbols = {sym for (sym, _) in self._buffers.keys()}
    for sym in symbols:
      for interval in ("1m", "15m"):
        try:
          await self.reconcile_rest_snapshot(sym, interval)
        except Exception as rec_err:
          logger.warning(
              "Reconciliation failed for %s (%s): %s", sym, interval, rec_err
          )

  # --------------------------------------------------------------------------
  # Candidate Universe Lifecycle
  # --------------------------------------------------------------------------

  async def set_monitored_universe(
      self,
      symbols: List[str],
      intervals: Tuple[str, ...] = ("1m", "15m"),
  ) -> None:
    """Pre-seeds historical data and subscribes WebSocket feeds for candidate symbols."""
    streams: List[str] = []

    # Pre-seed history in parallel
    tasks = []
    for sym in symbols:
      tasks.append(self.seed_symbol_history(sym, intervals))
      for interval in intervals:
        streams.append(f"{sym.lower()}@kline_{interval}")

    if tasks:
      await asyncio.gather(*tasks, return_exceptions=True)

    # Update WebSocket stream subscriptions
    self.ws.unsubscribe_all()
    self.ws.subscribe_streams(streams)

  def start(self) -> None:
    """Starts the underlying WebSocket feed."""
    self.ws.start()

  async def stop(self) -> None:
    """Stops the underlying WebSocket feed."""
    await self.ws.stop()
