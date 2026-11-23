"""Binance USDⓈ-M Futures exchange connector package."""

from src.exchange.client import BinanceFuturesClient
from src.exchange.exceptions import (
    BinanceAPIError,
    BinanceAuthError,
    BinanceConnectorError,
    BinanceFilterError,
    BinanceNetworkError,
    BinanceRateLimitError,
)
from src.exchange.models import (
    AccountInfo,
    AssetBalance,
    LeverageBracketTier,
    OpenOrder,
    PositionRisk,
    ServerTimeResponse,
    SymbolFilter,
    SymbolInfo,
    SymbolLeverageBrackets,
)
from src.exchange.rate_limiter import RateLimiter
from src.exchange.signer import calculate_signature, sign_parameters

__all__ = [
    "BinanceFuturesClient",
    "BinanceConnectorError",
    "BinanceNetworkError",
    "BinanceAuthError",
    "BinanceRateLimitError",
    "BinanceAPIError",
    "BinanceFilterError",
    "ServerTimeResponse",
    "SymbolFilter",
    "SymbolInfo",
    "AccountInfo",
    "AssetBalance",
    "PositionRisk",
    "OpenOrder",
    "LeverageBracketTier",
    "SymbolLeverageBrackets",
    "RateLimiter",
    "sign_parameters",
    "calculate_signature",
]
