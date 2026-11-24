"""Protective Order Manager for Binance USD(S)-M Futures.

Responsible for:
1. Sizing protective SL and TP orders strictly based on actual entry executedQty.
2. Recalculating SL and 3:1 TP using actual average entry fill price.
3. Applying directional tickSize rounding (LONG: SL floor, TP ceil; SHORT: SL ceil, TP floor).
4. Submitting exchange-side STOP_MARKET and TAKE_PROFIT_MARKET orders with reduceOnly=true.
5. Verifying both conditional orders exist on the exchange in NEW state.
6. Immediate cancellation of sibling order when SL or TP triggers and fills.
7. Verification of zero net exposure on the exchange after exit.
8. Fail-safe emergency market close if protective orders cannot be established or verified.
"""

import asyncio
import math
import time
from typing import Dict, Optional
from src.exchange.client import BinanceFuturesClient
from src.exchange.exceptions import BinanceAPIError
from src.execution.client_id import OrderPrefix, generate_client_order_id
from src.execution.exceptions import ProtectiveOrderError
from src.execution.models import OrderSide, OrderStatus, OrderType
from src.monitoring.logger import get_logger
from src.positions.models import (
    EmergencyExitResult,
    ExitReason,
    ExitTriggerResult,
    ProtectiveOrderPair,
)

logger = get_logger("positions.protective")


def round_price_directional(
    price: float,
    tick_size: float,
    round_up: bool,
) -> float:
  """Rounds price to tick_size either strictly UP (ceil) or strictly DOWN (floor).

  Uses integer scaled arithmetic to prevent floating point inaccuracies.
  """
  if tick_size <= 0:
    raise ValueError(f"tick_size must be positive, got {tick_size}")

  precision = max(0, int(math.ceil(-math.log10(tick_size)))) if tick_size < 1.0 else 0
  scale = 10**precision
  scaled_price = price * scale
  scaled_tick = round(tick_size * scale) if scale > 1 else round(tick_size)

  if scaled_tick <= 0:
    scaled_tick = 1

  if round_up:
    rounded_int = math.ceil(scaled_price / scaled_tick) * scaled_tick
  else:
    rounded_int = math.floor(scaled_price / scaled_tick) * scaled_tick

  return rounded_int / scale


class ProtectiveOrderManager:
  """Manages the creation, verification, sibling cancellation, and emergency exit

  of exchange-side protective orders (STOP_MARKET and TAKE_PROFIT_MARKET).
  """

  def __init__(self, exchange_client: BinanceFuturesClient):
    self.client = exchange_client
    self._active_pairs: Dict[str, ProtectiveOrderPair] = {}
    self._lock = asyncio.Lock()

  def get_active_pair(self, symbol: str) -> Optional[ProtectiveOrderPair]:
    """Retrieves the active protective order pair for a symbol if one exists."""
    return self._active_pairs.get(symbol.upper())

  async def place_protective_orders(
      self,
      symbol: str,
      entry_side: OrderSide,
      executed_qty: float,
      avg_entry_price: float,
      stop_loss_price: float,
      tick_size: float,
      step_size: float,
      take_profit_price: Optional[float] = None,
      rr_ratio: float = 3.0,
      working_type: str = "CONTRACT_PRICE",
      verify_on_exchange: bool = True,
  ) -> ProtectiveOrderPair:
    """Calculates, submits, and verifies exchange-side SL and TP protective orders.

    Args:
        symbol: Symbol contract (e.g. 'BTCUSDT').
        entry_side: Side of the filled entry order (BUY for LONG, SELL for
          SHORT).
        executed_qty: Actual executed quantity from entry fill.
        avg_entry_price: Actual average fill price of entry.
        stop_loss_price: Confirmed swing pivot stop price.
        tick_size: PRICE_FILTER tickSize for directional rounding.
        step_size: LOT_SIZE stepSize for quantity validation.
        take_profit_price: Optional pre-calculated TP (recalculated from actual
          fill if None).
        rr_ratio: Reward-to-risk ratio (default 3.0).
        working_type: 'CONTRACT_PRICE' or 'MARK_PRICE'.
        verify_on_exchange: Whether to query exchange orders after placement.

    Returns:
        ProtectiveOrderPair: Active confirmed protective orders.

    Raises:
        ProtectiveOrderError: If orders cannot be established or verified.
    """
    sym = symbol.upper()
    exit_side = OrderSide.SELL if entry_side == OrderSide.BUY else OrderSide.BUY

    if executed_qty <= 0:
      raise ProtectiveOrderError(
          f"Cannot establish protection on non-positive quantity: {executed_qty}"
      )
    if avg_entry_price <= 0:
      raise ProtectiveOrderError(
          f"Cannot establish protection on non-positive entry price: {avg_entry_price}"
      )

    # 1. Recalculate and validate SL / TP based on actual fill price
    if entry_side == OrderSide.BUY:  # LONG
      stop_dist = avg_entry_price - stop_loss_price
      if stop_dist <= 0:
        raise ProtectiveOrderError(
            f"LONG stop price ({stop_loss_price}) must be strictly below actual"
            f" entry ({avg_entry_price})"
        )
      target_tp = (
          take_profit_price
          if take_profit_price is not None
          else (avg_entry_price + (rr_ratio * stop_dist))
      )
      sl_rounded = round_price_directional(
          stop_loss_price, tick_size, round_up=False
      )
      tp_rounded = round_price_directional(target_tp, tick_size, round_up=True)
    else:  # SHORT
      stop_dist = stop_loss_price - avg_entry_price
      if stop_dist <= 0:
        raise ProtectiveOrderError(
            f"SHORT stop price ({stop_loss_price}) must be strictly above"
            f" actual entry ({avg_entry_price})"
        )
      target_tp = (
          take_profit_price
          if take_profit_price is not None
          else (avg_entry_price - (rr_ratio * stop_dist))
      )
      sl_rounded = round_price_directional(
          stop_loss_price, tick_size, round_up=True
      )
      tp_rounded = round_price_directional(target_tp, tick_size, round_up=False)

    sl_cid = generate_client_order_id(OrderPrefix.STOP_LOSS)
    tp_cid = generate_client_order_id(OrderPrefix.TAKE_PROFIT)

    sl_order_id: Optional[int] = None
    tp_order_id: Optional[int] = None
    sl_placed = False
    tp_placed = False

    try:
      # 2. Submit STOP_MARKET order
      sl_params = {
          "symbol": sym,
          "side": exit_side.value,
          "type": OrderType.STOP_MARKET.value,
          "quantity": executed_qty,
          "stopPrice": sl_rounded,
          "reduceOnly": "true",
          "newClientOrderId": sl_cid,
          "workingType": working_type,
      }
      logger.info(
          "Placing STOP_MARKET for %s: qty=%.6f, stopPrice=%.4f [cid=%s]",
          sym,
          executed_qty,
          sl_rounded,
          sl_cid,
      )
      sl_res = await self.client.place_order(sl_params)
      sl_placed = True
      sl_order_id = int(sl_res.get("orderId", 0))

      if sl_res.get("status") not in (
          OrderStatus.NEW.value,
          OrderStatus.FILLED.value,
      ):
        raise ProtectiveOrderError(
            f"SL order placement returned unexpected status:"
            f" {sl_res.get('status')}"
        )

      # 3. Submit TAKE_PROFIT_MARKET order
      tp_params = {
          "symbol": sym,
          "side": exit_side.value,
          "type": OrderType.TAKE_PROFIT_MARKET.value,
          "quantity": executed_qty,
          "stopPrice": tp_rounded,
          "reduceOnly": "true",
          "newClientOrderId": tp_cid,
          "workingType": working_type,
      }
      logger.info(
          "Placing TAKE_PROFIT_MARKET for %s: qty=%.6f, stopPrice=%.4f"
          " [cid=%s]",
          sym,
          executed_qty,
          tp_rounded,
          tp_cid,
      )
      tp_res = await self.client.place_order(tp_params)
      tp_placed = True
      tp_order_id = int(tp_res.get("orderId", 0))

      if tp_res.get("status") not in (
          OrderStatus.NEW.value,
          OrderStatus.FILLED.value,
      ):
        raise ProtectiveOrderError(
            f"TP order placement returned unexpected status:"
            f" {tp_res.get('status')}"
        )

      # 4. Optional active verification on exchange
      if verify_on_exchange:
        q_sl = await self.client.query_order(sym, client_order_id=sl_cid)
        q_tp = await self.client.query_order(sym, client_order_id=tp_cid)
        if q_sl.get("status") != OrderStatus.NEW.value:
          raise ProtectiveOrderError(
              f"SL order verification failed: status is {q_sl.get('status')}"
          )
        if q_tp.get("status") != OrderStatus.NEW.value:
          raise ProtectiveOrderError(
              f"TP order verification failed: status is {q_tp.get('status')}"
          )

      pair = ProtectiveOrderPair(
          symbol=sym,
          entry_side=entry_side,
          exit_side=exit_side,
          quantity=executed_qty,
          avg_entry_price=avg_entry_price,
          sl_order_id=sl_order_id,
          sl_client_order_id=sl_cid,
          sl_price=sl_rounded,
          tp_order_id=tp_order_id,
          tp_client_order_id=tp_cid,
          tp_price=tp_rounded,
          is_active=True,
      )

      async with self._lock:
        self._active_pairs[sym] = pair

      logger.info(
          "Protective orders verified for %s [SL: %s @ %.4f, TP: %s @ %.4f]",
          sym,
          sl_cid,
          sl_rounded,
          tp_cid,
          tp_rounded,
      )
      return pair

    except Exception as exc:
      logger.critical(
          "Failed to establish or verify protective orders for %s: %s."
          " Initiating emergency mitigation...",
          sym,
          exc,
      )
      # Cleanup any placed orders
      if sl_placed:
        try:
          await self.client.cancel_order(sym, client_order_id=sl_cid)
        except Exception as c_err:
          logger.warning("Error cancelling SL during emergency cleanup: %s", c_err)

      if tp_placed:
        try:
          await self.client.cancel_order(sym, client_order_id=tp_cid)
        except Exception as c_err:
          logger.warning("Error cancelling TP during emergency cleanup: %s", c_err)

      # Execute fail-safe emergency market close
      emg_res = await self.execute_emergency_market_close(
          sym, position_side=entry_side, quantity=executed_qty
      )
      raise ProtectiveOrderError(
          f"Failed to place protective orders for {sym} ({exc}). "
          f"Emergency market close executed (closed={emg_res.is_closed})."
      ) from exc

  async def cancel_sibling_order(
      self,
      triggered_client_order_id: Optional[str] = None,
      triggered_order_id: Optional[int] = None,
      active_pair: Optional[ProtectiveOrderPair] = None,
      symbol: Optional[str] = None,
      verify_position_closed: bool = True,
  ) -> ExitTriggerResult:
    """Cancels the sibling protective order when either SL or TP fills,

    and verifies net zero exposure.
    """
    pair = active_pair
    if pair is None and symbol:
      pair = self.get_active_pair(symbol)

    if pair is None:
      # Find pair by order ID
      for p in self._active_pairs.values():
        if p.is_sl_order(
            triggered_client_order_id, triggered_order_id
        ) or p.is_tp_order(triggered_client_order_id, triggered_order_id):
          pair = p
          break

    if pair is None:
      raise ValueError("No active protective order pair found for exit event.")

    is_sl = pair.is_sl_order(triggered_client_order_id, triggered_order_id)
    exit_reason = ExitReason.STOP_LOSS if is_sl else ExitReason.TAKE_PROFIT
    triggered_type = (
        OrderType.STOP_MARKET if is_sl else OrderType.TAKE_PROFIT_MARKET
    )

    sibling_cid, sibling_oid, _ = pair.get_sibling_order_details(
        triggered_client_order_id, triggered_order_id
    )

    logger.info(
        "Exit triggered by %s (%s) for %s. Cancelling sibling %s...",
        exit_reason.value,
        triggered_client_order_id or triggered_order_id,
        pair.symbol,
        sibling_cid,
    )

    # Issue cancellation for sibling
    cancellation_success = False
    try:
      await self.client.cancel_order(pair.symbol, client_order_id=sibling_cid)
      cancellation_success = True
      logger.info(
          "Successfully cancelled sibling order %s for %s",
          sibling_cid,
          pair.symbol,
      )
    except BinanceAPIError as api_err:
      if api_err.code in (-2011, -2013):
        # -2011: Unknown order (already filled or cancelled)
        # -2013: Order does not exist
        logger.info(
            "Sibling order %s already finished or absent (%s)",
            sibling_cid,
            api_err.message,
        )
        cancellation_success = True
      else:
        logger.error(
            "Failed to cancel sibling order %s: %s", sibling_cid, api_err
        )
    except Exception as exc:
      logger.error("Transport error cancelling sibling %s: %s", sibling_cid, exc)

    # Verify net position closed on exchange
    position_closed = False
    if verify_position_closed:
      position_closed = await self.verify_position_closed(pair.symbol)

    pair.is_active = False
    async with self._lock:
      self._active_pairs.pop(pair.symbol, None)

    return ExitTriggerResult(
        symbol=pair.symbol,
        triggered_client_order_id=triggered_client_order_id
        or str(triggered_order_id),
        triggered_type=triggered_type,
        exit_reason=exit_reason,
        cancelled_sibling_client_order_id=sibling_cid,
        sibling_cancellation_success=cancellation_success,
        position_closed_verified=position_closed,
        timestamp_ms=int(time.time() * 1000),
    )

  async def verify_position_closed(
      self,
      symbol: str,
      timeout_seconds: float = 5.0,
      poll_interval_seconds: float = 0.2,
  ) -> bool:
    """Verifies that the exchange position for symbol is zero."""
    start_time = time.monotonic()
    sym = symbol.upper()

    while time.monotonic() - start_time < timeout_seconds:
      try:
        positions = await self.client.get_positions(sym)
        for pos in positions:
          if pos.symbol == sym:
            if not pos.is_open:
              logger.info(
                  "Verified position for %s is closed (positionAmt=%.6f).",
                  sym,
                  pos.position_amt,
              )
              return True
            else:
              logger.debug(
                  "Position for %s still open (positionAmt=%.6f). Waiting...",
                  sym,
                  pos.position_amt,
              )
      except Exception as err:
        logger.warning(
            "Error querying position for %s during verification: %s", sym, err
        )

      await asyncio.sleep(poll_interval_seconds)

    logger.warning(
        "Position for %s could not be verified closed within %.1fs.",
        sym,
        timeout_seconds,
    )
    return False

  async def execute_emergency_market_close(
      self,
      symbol: str,
      position_side: OrderSide,
      quantity: float,
  ) -> EmergencyExitResult:
    """Submits a reduceOnly market order to close an unprotected or stuck position immediately."""
    sym = symbol.upper()
    exit_side = (
        OrderSide.SELL if position_side == OrderSide.BUY else OrderSide.BUY
    )
    emg_cid = generate_client_order_id(OrderPrefix.EMERGENCY)

    logger.critical(
        "EXECUTING EMERGENCY MARKET CLOSE for %s: side=%s, qty=%.6f [cid=%s]",
        sym,
        exit_side.value,
        quantity,
        emg_cid,
    )

    order_params = {
        "symbol": sym,
        "side": exit_side.value,
        "type": OrderType.MARKET.value,
        "quantity": quantity,
        "reduceOnly": "true",
        "newClientOrderId": emg_cid,
    }

    try:
      res = await self.client.place_order(order_params)
      logger.info(
          "Emergency market order submitted for %s: %s",
          sym,
          res.get("status"),
      )
      is_closed = await self.verify_position_closed(sym, timeout_seconds=3.0)
      return EmergencyExitResult(
          symbol=sym,
          emergency_client_order_id=emg_cid,
          quantity=quantity,
          is_closed=is_closed,
          timestamp_ms=int(time.time() * 1000),
      )
    except Exception as exc:
      logger.critical(
          "Emergency market close failed for %s: %s", sym, exc, exc_info=True
      )
      return EmergencyExitResult(
          symbol=sym,
          emergency_client_order_id=emg_cid,
          quantity=quantity,
          is_closed=False,
          error_message=str(exc),
          timestamp_ms=int(time.time() * 1000),
      )
