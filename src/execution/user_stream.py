"""Binance Futures User Data Stream WebSocket client.

Monitors real-time account and order execution events (ORDER_TRADE_UPDATE)
to immediately detect Stop-Loss / Take-Profit fills and trigger sibling
cancellation.
"""

import asyncio
import json
from typing import Callable, Coroutine, List, Optional
import websockets
from websockets.exceptions import ConnectionClosed
from src.exchange.client import BinanceFuturesClient
from src.monitoring.logger import get_logger

logger = get_logger("execution.user_stream")


class BinanceUserDataStream:
  """Manages the Binance Futures User Data WebSocket stream and listenKey keepalive."""

  def __init__(
      self,
      client: BinanceFuturesClient,
      ws_base_url: str = "wss://stream.binancefuture.com",
      keepalive_interval_seconds: float = 1800.0,  # 30 minutes
      reconnect_delay_seconds: float = 2.0,
      max_reconnect_delay_seconds: float = 30.0,
  ):
    self.client = client
    self.ws_base_url = ws_base_url.rstrip("/")
    if self.ws_base_url.endswith("/ws"):
      self.ws_base_url = self.ws_base_url[:-3]
    self.keepalive_interval = keepalive_interval_seconds
    self.reconnect_delay = reconnect_delay_seconds
    self.max_reconnect_delay = max_reconnect_delay_seconds

    self._listen_key: Optional[str] = None
    self._order_update_handlers: List[Callable[[dict], Coroutine]] = []
    self._ws_task: Optional[asyncio.Task] = None
    self._keepalive_task: Optional[asyncio.Task] = None
    self._stop_event = asyncio.Event()

  def register_order_update_handler(
      self, handler: Callable[[dict], Coroutine]
  ) -> None:
    """Registers an async callback invoked on ORDER_TRADE_UPDATE events."""
    self._order_update_handlers.append(handler)

  def _build_stream_url(self, listen_key: str) -> str:
    """Constructs the User Data Stream WebSocket URL."""
    return f"{self.ws_base_url}/ws/{listen_key}"

  async def process_event(self, payload: dict) -> None:
    """Parses and dispatches a User Data Stream message."""
    event_type = payload.get("e")
    if event_type == "ORDER_TRADE_UPDATE":
      for handler in self._order_update_handlers:
        try:
          await handler(payload)
        except Exception as exc:
          logger.error("Error in ORDER_TRADE_UPDATE handler: %s", exc)
    elif event_type == "listenKeyExpired":
      logger.warning("User Data Stream listenKey expired. Reconnecting...")

  async def _keepalive_loop(self) -> None:
    """Periodically keeps the listenKey alive every 30 minutes."""
    while not self._stop_event.is_set():
      try:
        await asyncio.wait_for(
            self._stop_event.wait(), timeout=self.keepalive_interval
        )
        break
      except asyncio.TimeoutError:
        try:
          await self.client.keepalive_listen_key()
          logger.debug("User Data Stream listenKey keepalive succeeded.")
        except Exception as exc:
          logger.warning("Failed to keepalive listenKey: %s", exc)

  async def _run_loop(self) -> None:
    """Continuous User Data Stream connection loop with auto-reconnect."""
    delay = self.reconnect_delay

    while not self._stop_event.is_set():
      try:
        self._listen_key = await self.client.create_listen_key()
        url = self._build_stream_url(self._listen_key)
        logger.info("Connecting to Binance User Data Stream...")

        async with websockets.connect(
            url, ping_interval=20, ping_timeout=10
        ) as ws:
          logger.info("User Data Stream connected.")
          delay = self.reconnect_delay

          while not self._stop_event.is_set():
            try:
              msg_raw = await ws.recv()
              payload = json.loads(msg_raw)
              await self.process_event(payload)
              if payload.get("e") == "listenKeyExpired":
                break
            except ConnectionClosed as cc:
              logger.warning("User Data Stream closed: %s", cc)
              break

      except (ConnectionClosed, OSError, asyncio.TimeoutError) as net_err:
        if self._stop_event.is_set():
          break
        logger.warning(
            "User Data Stream dropped (%s). Reconnecting in %.1fs...",
            net_err,
            delay,
        )
        await asyncio.sleep(delay)
        delay = min(delay * 1.5, self.max_reconnect_delay)
      except Exception as exc:
        if self._stop_event.is_set():
          break
        logger.error(
            "Unexpected error in User Data Stream: %s. Reconnecting in %.1fs...",
            exc,
            delay,
        )
        await asyncio.sleep(delay)
        delay = min(delay * 1.5, self.max_reconnect_delay)

  def start(self) -> None:
    """Starts the User Data Stream WebSocket and keepalive background tasks."""
    if self._ws_task and not self._ws_task.done():
      return
    self._stop_event.clear()
    self._ws_task = asyncio.create_task(self._run_loop(), name="BinanceUserWS")
    self._keepalive_task = asyncio.create_task(
        self._keepalive_loop(), name="UserWSKeepalive"
    )

  async def stop(self) -> None:
    """Stops the User Data Stream and closes the listenKey."""
    self._stop_event.set()
    for task in (self._keepalive_task, self._ws_task):
      if task:
        task.cancel()
        try:
          await task
        except (asyncio.CancelledError, Exception):
          pass
    self._keepalive_task = None
    self._ws_task = None

    if self._listen_key:
      try:
        await self.client.close_listen_key()
      except Exception:
        pass
      self._listen_key = None

    logger.info("User Data Stream stopped.")
