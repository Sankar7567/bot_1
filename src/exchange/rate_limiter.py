"""Client-side rate limit tracking and throttling for Binance Futures.

Monitors X-MBX-USED-WEIGHT-* response headers and regulates request dispatching
to prevent HTTP 429 and IP bans.
"""

import asyncio
import time
from typing import Mapping, Optional
from src.monitoring.logger import get_logger

logger = get_logger("exchange.rate_limiter")


class RateLimiter:
  """Token-bucket and exchange-header-driven rate limiter."""

  def __init__(
      self,
      max_weight_per_minute: int = 2400,
      safety_threshold_ratio: float = 0.85,  # Slow down when reaching 85% of limit
  ):
    self.max_weight_per_minute = max_weight_per_minute
    self.safety_threshold = int(
        max_weight_per_minute * safety_threshold_ratio
    )  # 2040
    self.current_used_weight_1m = 0
    self._lock = asyncio.Lock()
    self._backoff_until: float = 0.0

  def update_from_headers(self, headers: Mapping[str, str]) -> None:
    """Parses exchange response headers to update current used weight."""
    for key, value in headers.items():
      lower_key = key.lower()
      if lower_key == "x-mbx-used-weight-1m":
        try:
          self.current_used_weight_1m = int(value)
          if self.current_used_weight_1m >= self.safety_threshold:
            logger.warning(
                "High Binance rate limit usage detected: %d / %d",
                self.current_used_weight_1m,
                self.max_weight_per_minute,
            )
        except ValueError:
          pass
      elif lower_key == "retry-after":
        try:
          delay = float(value)
          self.set_backoff(delay)
        except ValueError:
          pass

  def set_backoff(self, delay_seconds: float) -> None:
    """Sets a mandatory pause window before subsequent requests can proceed."""
    self._backoff_until = max(self._backoff_until, time.time() + delay_seconds)
    logger.warning("Rate limiter backoff active for %.2f seconds", delay_seconds)

  async def acquire(self, weight: int = 1) -> None:
    """Awaits capacity before allowing a request with the specified weight to proceed."""
    async with self._lock:
      now = time.time()
      if now < self._backoff_until:
        sleep_time = self._backoff_until - now
        logger.info(
            "Rate limiter sleeping for %.2fs due to active backoff", sleep_time
        )
        await asyncio.sleep(sleep_time)

      # If current 1-minute usage exceeds safety threshold, add a proactive throttling delay
      if self.current_used_weight_1m >= self.safety_threshold:
        throttle_delay = 0.5 * (
            self.current_used_weight_1m / self.safety_threshold
        )
        logger.warning(
            "Proactively throttling request by %.2fs (used weight: %d)",
            throttle_delay,
            self.current_used_weight_1m,
        )
        await asyncio.sleep(throttle_delay)
