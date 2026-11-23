"""Market Data Engine package."""

from src.market_data.candle_buffer import CandleBuffer
from src.market_data.engine import MarketDataEngine
from src.market_data.exceptions import (
    CandleDataError,
    MarketDataError,
    MissingCandleError,
    StaleCandleError,
    WebSocketFeedError,
)
from src.market_data.feed import BinanceKlineWebSocket
from src.market_data.models import Candle, CandleCloseEvent, interval_to_ms

__all__ = [
    "MarketDataEngine",
    "CandleBuffer",
    "Candle",
    "CandleCloseEvent",
    "interval_to_ms",
    "BinanceKlineWebSocket",
    "MarketDataError",
    "CandleDataError",
    "MissingCandleError",
    "StaleCandleError",
    "WebSocketFeedError",
]
