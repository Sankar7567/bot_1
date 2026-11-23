"""Filtering and deterministic ranking logic for market scanning."""

import math
from typing import Any, Tuple
from src.exchange.models import SymbolInfo, Ticker24hr


def is_eligible_contract(
    info: SymbolInfo,
    required_quote_asset: str = "USDT",
    required_contract_type: str = "PERPETUAL",
) -> bool:
  """Validates if a symbol contract meets trading eligibility criteria.

  Must be TRADING status, quote asset USDT, and contract type PERPETUAL.
  """
  if not info.is_trading:
    return False
  if info.quote_asset != required_quote_asset:
    return False
  if info.contract_type != required_contract_type:
    return False
  return True


def is_eligible_ticker(
    ticker: Ticker24hr,
    min_quote_volume_24h: float = 1000000.0,
) -> bool:
  """Validates if 24hr ticker data meets liquidity and validity criteria.

  Must have quote volume >= minimum threshold, valid non-NaN prices, and
  positive last price.
  """
  if math.isnan(ticker.price_change_percent) or math.isinf(
      ticker.price_change_percent
  ):
    return False
  if ticker.last_price <= 0:
    return False
  if ticker.quote_volume < min_quote_volume_24h:
    return False
  return True


def deterministic_ranking_sort_key(ticker: Ticker24hr) -> Tuple[float, float, str]:
  """Computes deterministic tuple sort key for ranking 24h ticker gainers.

  1. Percentage price change descending (-price_change_percent)
  2. Quote volume descending (-quote_volume) as primary tie-breaker
  3. Symbol name ascending (symbol) as secondary tie-breaker
  """
  return (
      -ticker.price_change_percent,
      -ticker.quote_volume,
      ticker.symbol,
  )
