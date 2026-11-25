"""Deterministic simulated UTC clock for chronological backtesting."""


class SimulatedClock:
  """Monotonic simulated UTC millisecond clock."""

  def __init__(self, initial_time_ms: int = 0):
    self._current_time_ms = initial_time_ms

  def now_ms(self) -> int:
    """Returns current simulated UTC timestamp in milliseconds."""
    return self._current_time_ms

  def advance_to(self, timestamp_ms: int) -> None:
    """Advances simulated clock to timestamp_ms. Never moves backwards."""
    if timestamp_ms < self._current_time_ms:
      raise ValueError(
          f"SimulatedClock cannot move backwards: {timestamp_ms} < {self._current_time_ms}"
      )
    self._current_time_ms = timestamp_ms
