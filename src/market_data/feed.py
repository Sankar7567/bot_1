"""Binance Futures Kline WebSocket client with automated reconnection."""

import asyncio
import json
from typing import Callable, Coroutine, List, Optional, Set
import websockets
from websockets.exceptions import ConnectionClosed
from src.monitoring.logger import get_logger

logger = get_logger("market_data.ws")


class BinanceKlineWebSocket:
  """Manages combined WebSocket streaming for Binance Futures klines."""

  def __init__(
      self,
      ws_base_url: str = "wss://stream.binancefuture.com",
      reconnect_delay_seconds: float = 2.0,
      max_reconnect_delay_seconds: float = 30.0,
  ):
    # Binance Futures combined streams base URL is /stream?streams=...
    self.ws_base_url = ws_base_url.rstrip("/")
    self.reconnect_delay = reconnect_delay_seconds
    self.max_reconnect_delay = max_reconnect_delay_seconds

    self._subscribed_streams: Set[str] = set()
    self._message_handlers: List[Callable[[dict], Coroutine]] = []
    self._reconnect_callbacks: List[Callable[[], Coroutine]] = []
    self._running = False
    self._task: Optional[asyncio.Task] = None
    self._stop_event = asyncio.Event()

  def register_handler(self, handler: Callable[[dict], Coroutine]) -> None:
    """Registers an async callback invoked on every received kline message."""
    self._message_handlers.append(handler)

  def register_reconnect_callback(self, callback: Callable[[], Coroutine]) -> None:
    """Registers an async callback invoked upon successful WebSocket reconnection."""
    self._reconnect_callbacks.append(callback)

  def subscribe_streams(self, streams: List[str]) -> None:
    """Adds streams to active subscription set (e.g., ['btcusdt@kline_1m', 'btcusdt@kline_15m'])."""
    for s in streams:
      self._subscribed_streams.add(s.lower())

  def unsubscribe_all(self) -> None:
    """Clears all active stream subscriptions."""
    self._subscribed_streams.clear()

  def _build_stream_url(self) -> str:
    """Constructs the combined stream WebSocket URL."""
    if not self._subscribed_streams:
      raise ValueError("No streams registered for WebSocket connection.")
    streams_param = "/".join(sorted(self._subscribed_streams))
    return f"{self.ws_base_url}/stream?streams={streams_param}"

  async def _run_loop(self) -> None:
    """Continuous connection loop with exponential backoff on disconnects."""
    delay = self.reconnect_delay

    while not self._stop_event.is_set():
      if not self._subscribed_streams:
        await asyncio.sleep(1.0)
        continue

      url = self._build_stream_url()
      logger.info(
          "Connecting to Binance Futures Kline WebSocket (%d streams)...",
          len(self._subscribed_streams),
      )

      try:
        async with websockets.connect(
            url, ping_interval=20, ping_timeout=10
        ) as ws:
          logger.info("Kline WebSocket connected successfully.")
          delay = self.reconnect_delay  # Reset delay on successful connection

          # Invoke post-connect / reconnect hooks (e.g. for REST reconciliation)
          for cb in self._reconnect_callbacks:
            try:
              await cb()
            except Exception as hook_err:
              logger.warning("Error in reconnect callback: %s", hook_err)

          while not self._stop_event.is_set():
            try:
              msg_raw = await ws.recv()
              payload = json.loads(msg_raw)
              # Combined stream format wraps payload in {"stream": "...", "data": {...}}
              data = payload.get("data", payload)

              for handler in self._message_handlers:
                try:
                  await handler(data)
                except Exception as h_err:
                  logger.error("Error in kline message handler: %s", h_err)

            except ConnectionClosed as cc:
              logger.warning("WebSocket connection closed: %s", cc)
              break

      except (ConnectionClosed, OSError, asyncio.TimeoutError) as net_err:
        if self._stop_event.is_set():
          break
        logger.warning(
            "WebSocket connection dropped (%s). Reconnecting in %.1fs...",
            net_err,
            delay,
        )
        await asyncio.sleep(delay)
        delay = min(delay * 1.5, self.max_reconnect_delay)
      except Exception as exc:
        if self._stop_event.is_set():
          break
        logger.error(
            "Unexpected error in WebSocket loop: %s. Reconnecting in %.1fs...",
            exc,
            delay,
        )
        await asyncio.sleep(delay)
        delay = min(delay * 1.5, self.max_reconnect_delay)

  def start(self) -> None:
    """Starts the WebSocket connection task."""
    if self._task and not self._task.done():
      return
    self._stop_event.clear()
    self._task = asyncio.create_task(self._run_loop(), name="BinanceKlineWS")

  async def stop(self) -> None:
    """Stops the WebSocket connection gracefully."""
    self._stop_event.set()
    if self._task:
      self._task.cancel()
      try:
        await self._task
      except (asyncio.CancelledError, Exception):
        pass
      self._task = None
    logger.info("Kline WebSocket stopped.")
