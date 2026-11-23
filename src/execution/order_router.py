"""Order router and execution manager for Binance USD(S)-M Futures.

Handles market entry order submission, isolated margin and leverage preparation,
idempotent order tracking via deterministic clientOrderId, fill verification,
timeout reconciliation, and partial fill handling.
"""

import asyncio
import time
from typing import Any, Dict, Optional
from src.exchange.client import BinanceFuturesClient
from src.exchange.exceptions import (
    BinanceAPIError,
    BinanceNetworkError,
)
from src.execution.client_id import OrderPrefix, generate_client_order_id
from src.execution.exceptions import (
    ExecutionError,
    FillTimeoutError,
    OrderRejectedError,
)
from src.execution.models import (
    OrderExecutionRequest,
    OrderFill,
    OrderSide,
    OrderStatus,
    OrderType,
)
from src.monitoring.logger import get_logger

logger = get_logger("execution.order_router")


class OrderRouter:
  """Coordinates order routing and execution against Binance USD(S)-M Futures."""

  def __init__(self, exchange_client: BinanceFuturesClient):
    self.client = exchange_client

  async def execute_market_entry(
      self,
      symbol: str,
      side: OrderSide,
      quantity: float,
      leverage: int,
      client_order_id: Optional[str] = None,
      fill_timeout_seconds: float = 5.0,
      poll_interval_seconds: float = 0.2,
  ) -> OrderFill:
    """Prepares account settings and executes a market entry order.

    Execution Flow:
    1. Ensure margin type is ISOLATED for the symbol.
    2. Set symbol leverage to target leverage.
    3. Generate collision-resistant clientOrderId if not provided.
    4. Submit market order via REST API.
    5. Handle network timeout / disconnection: reconcile order status via query
       before declaring failure.
    6. Poll for completion if market order does not fill immediately.
    7. Return confirmed OrderFill with executed quantity and average fill price.

    Args:
        symbol: Symbol contract (e.g. 'BTCUSDT').
        side: OrderSide.BUY for LONG, OrderSide.SELL for SHORT.
        quantity: Step-size truncated position quantity.
        leverage: Max eligible leverage for the position notional.
        client_order_id: Optional custom clientOrderId.
        fill_timeout_seconds: Max seconds to wait for complete fill.
        poll_interval_seconds: Polling sleep interval when tracking fills.

    Returns:
        OrderFill: Fill details with executed_qty and avg_price.

    Raises:
        OrderRejectedError: If the exchange rejects the order.
        FillTimeoutError: If the order fails to fill within timeout.
        ExecutionError: On unrecoverable network or API errors.
    """
    sym = symbol.upper()
    cid = client_order_id or generate_client_order_id(OrderPrefix.ENTRY)

    # 1. Configure ISOLATED margin type
    try:
      await self.client.change_margin_type(sym, "ISOLATED")
      logger.info("Set margin type to ISOLATED for %s", sym)
    except BinanceAPIError as err:
      if err.code == -4046:  # Margin type already set
        logger.debug("Margin type already ISOLATED for %s", sym)
      else:
        logger.error("Failed to set margin type for %s: %s", sym, err)
        raise ExecutionError(f"Failed to set margin type: {err}", client_order_id=cid) from err

    # 2. Configure leverage
    try:
      await self.client.change_leverage(sym, leverage)
      logger.info("Set leverage to %dx for %s", leverage, sym)
    except BinanceAPIError as err:
      logger.error("Failed to set leverage %dx for %s: %s", leverage, sym, err)
      raise ExecutionError(f"Failed to set leverage: {err}", client_order_id=cid) from err

    # 3. Build market order request
    request = OrderExecutionRequest(
        symbol=sym,
        side=side,
        order_type=OrderType.MARKET,
        quantity=quantity,
        client_order_id=cid,
    )
    order_params = request.to_binance_params()

    # 4. Submit market order with network timeout reconciliation
    raw_response: Optional[Dict[str, Any]] = None
    try:
      logger.info(
          "Submitting MARKET %s order for %s qty=%.6f [cid=%s]",
          side.value,
          sym,
          quantity,
          cid,
      )
      raw_response = await self.client.place_order(order_params)
    except (BinanceNetworkError, asyncio.TimeoutError) as net_err:
      logger.warning(
          "Network timeout submitting order %s for %s: %s. Reconciling via query...",
          cid,
          sym,
          net_err,
      )
      raw_response = await self._reconcile_unconfirmed_order(sym, cid)
    except BinanceAPIError as api_err:
      logger.error("Exchange rejected market order for %s [cid=%s]: %s", sym, cid, api_err)
      raise OrderRejectedError(
          f"Order rejected by exchange: {api_err.message} (code={api_err.code})",
          client_order_id=cid,
      ) from api_err

    if not raw_response:
      raise ExecutionError(
          f"Failed to submit or reconcile market entry order for {sym}",
          client_order_id=cid,
      )

    fill = OrderFill.from_raw_response(raw_response, default_type=OrderType.MARKET)

    # 5. Check if order status is already terminal
    if fill.status in (OrderStatus.REJECTED, OrderStatus.EXPIRED, OrderStatus.CANCELED):
      raise OrderRejectedError(
          f"Market order {cid} ended in terminal status: {fill.status.value}",
          client_order_id=cid,
      )

    if fill.is_fully_filled:
      logger.info(
          "Market order %s FILLED immediately for %s: executedQty=%.6f, avgPrice=%.4f",
          cid,
          sym,
          fill.executed_qty,
          fill.avg_price,
      )
      return fill

    # 6. Poll for fill completion if NEW or PARTIALLY_FILLED
    start_time = time.monotonic()
    latest_fill = fill

    while time.monotonic() - start_time < fill_timeout_seconds:
      await asyncio.sleep(poll_interval_seconds)
      try:
        query_raw = await self.client.query_order(sym, client_order_id=cid)
        latest_fill = OrderFill.from_raw_response(query_raw, default_type=OrderType.MARKET)

        if latest_fill.is_fully_filled:
          logger.info(
              "Market order %s FILLED after polling for %s: executedQty=%.6f, avgPrice=%.4f",
              cid,
              sym,
              latest_fill.executed_qty,
              latest_fill.avg_price,
          )
          return latest_fill

        if latest_fill.status in (OrderStatus.REJECTED, OrderStatus.EXPIRED, OrderStatus.CANCELED):
          break
      except Exception as poll_err:
        logger.warning("Error polling order %s: %s", cid, poll_err)

    # 7. Handle timeout / incomplete fill
    if latest_fill.executed_qty > 0:
      logger.warning(
          "Market order %s partially filled after timeout: executedQty=%.6f of %.6f",
          cid,
          latest_fill.executed_qty,
          quantity,
      )
      return latest_fill

    raise FillTimeoutError(
        f"Market order {cid} for {sym} failed to fill within {fill_timeout_seconds}s "
        f"(status={latest_fill.status.value})",
        client_order_id=cid,
    )

  async def _reconcile_unconfirmed_order(
      self,
      symbol: str,
      client_order_id: str,
      max_attempts: int = 3,
      delay_seconds: float = 0.5,
  ) -> Optional[Dict[str, Any]]:
    """Queries exchange to verify if an order was placed despite a network error."""
    for attempt in range(1, max_attempts + 1):
      try:
        logger.info(
            "Reconciliation attempt %d/%d for order %s...",
            attempt,
            max_attempts,
            client_order_id,
        )
        return await self.client.query_order(symbol, client_order_id=client_order_id)
      except BinanceAPIError as api_err:
        if api_err.code == -2013:  # Order does not exist
          logger.info("Order %s does not exist on exchange.", client_order_id)
          return None
        logger.warning(
            "API error during reconciliation attempt %d: %s", attempt, api_err
        )
      except Exception as err:
        logger.warning(
            "Transport error during reconciliation attempt %d: %s", attempt, err
        )

      if attempt < max_attempts:
        await asyncio.sleep(delay_seconds)

    return None

  async def query_order_fill(
      self,
      symbol: str,
      client_order_id: Optional[str] = None,
      order_id: Optional[int] = None,
  ) -> OrderFill:
    """Queries the exchange for the current status and fill of an order."""
    raw = await self.client.query_order(
        symbol=symbol,
        client_order_id=client_order_id,
        order_id=order_id,
    )
    return OrderFill.from_raw_response(raw)

  async def cancel_order(
      self,
      symbol: str,
      client_order_id: Optional[str] = None,
      order_id: Optional[int] = None,
  ) -> Dict[str, Any]:
    """Cancels an open order idempotently, tolerating already cancelled/filled orders."""
    try:
      return await self.client.cancel_order(
          symbol=symbol,
          client_order_id=client_order_id,
          order_id=order_id,
      )
    except BinanceAPIError as err:
      if err.code in (-2011, -2013):
        # -2011: Unknown order sent (already filled or cancelled)
        # -2013: Order does not exist
        logger.info(
            "Cancel order %s %s ignored: %s (code=%d)",
            symbol,
            client_order_id or order_id,
            err.message,
            err.code,
        )
        return {"code": 200, "msg": err.message}
      raise
