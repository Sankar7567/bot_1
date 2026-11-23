"""Leverage bracket matching for the Risk Management module.

Encapsulates the bracket-selection logic that determines maximum eligible
leverage and maintenance margin ratio for a given position notional.

Binance sorts brackets from lowest notional_cap to highest.  The applicable
bracket for a given notional N is the first bracket where N ≤ notional_cap.
"""

from typing import List, Optional
from src.exchange.models import LeverageBracketTier, SymbolLeverageBrackets
from src.risk.models import BracketInfo
from src.monitoring.logger import get_logger

logger = get_logger("risk.brackets")


def get_bracket_for_notional(
    symbol_brackets: SymbolLeverageBrackets,
    notional: float,
) -> Optional[BracketInfo]:
  """Finds the applicable leverage bracket for the given notional value.

  Iterates brackets sorted by notional_cap ascending and returns the first
  bracket whose notional_cap ≥ notional.  If the notional exceeds all caps,
  returns the last bracket (highest notional, lowest leverage).

  Args:
      symbol_brackets: Full leverage bracket structure for the symbol.
      notional:        Target position notional (quantity × entry_price).

  Returns:
      BracketInfo if a bracket is found, None if the brackets list is empty.
  """
  if not symbol_brackets.brackets:
    logger.warning(
        "No leverage brackets available for %s", symbol_brackets.symbol
    )
    return None

  sorted_tiers: List[LeverageBracketTier] = sorted(
      symbol_brackets.brackets, key=lambda b: b.notional_cap
  )

  for tier in sorted_tiers:
    if notional <= tier.notional_cap:
      logger.debug(
          "Symbol %s notional=%.4f → bracket %d (max_leverage=%d, mmr=%.4f)",
          symbol_brackets.symbol,
          notional,
          tier.bracket,
          tier.initial_leverage,
          tier.maint_margin_ratio,
      )
      return BracketInfo(
          bracket_number=tier.bracket,
          max_leverage=tier.initial_leverage,
          notional_cap=tier.notional_cap,
          notional_floor=tier.notional_floor,
          maint_margin_ratio=tier.maint_margin_ratio,
      )

  # Notional exceeds all brackets — return the highest notional / lowest leverage tier
  last = sorted_tiers[-1]
  logger.warning(
      "Symbol %s notional=%.4f exceeds all bracket caps; using last bracket"
      " %d (max_leverage=%d).",
      symbol_brackets.symbol,
      notional,
      last.bracket,
      last.initial_leverage,
  )
  return BracketInfo(
      bracket_number=last.bracket,
      max_leverage=last.initial_leverage,
      notional_cap=last.notional_cap,
      notional_floor=last.notional_floor,
      maint_margin_ratio=last.maint_margin_ratio,
  )


def get_max_eligible_leverage(
    symbol_brackets: SymbolLeverageBrackets,
    notional: float,
) -> int:
  """Returns the maximum eligible leverage integer for the given notional.

  Args:
      symbol_brackets: Full leverage bracket structure for the symbol.
      notional:        Target position notional.

  Returns:
      Integer leverage (e.g. 20, 50, 125).  Defaults to 1 if no bracket found.
  """
  info = get_bracket_for_notional(symbol_brackets, notional)
  if info is None:
    return 1
  return info.max_leverage
