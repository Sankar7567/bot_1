"""Global Position Manager for Binance USD(S)-M Futures.

Coordinates:
1. Global 1-position lock enforcement across the trading system.
2. Market entry order execution and order status tracking.
3. Synchronous placement and verification of exchange-side SL/TP protective orders.
4. Active position lifecycle state transitions (IDLE -> PENDING_ENTRY -> OPEN -> PENDING_EXIT -> CLOSED).
5. Database persistence of positions, orders, and audit trail.
6. Coordinated exit fill processing with atomic sibling cancellation and zero-exposure verification.
7. Fail-safe emergency market closure on protection failures.
"""

import asyncio
import time
from typing import Optional
from src.exchange.client import BinanceFuturesClient
from src.execution.exceptions import ProtectiveOrderError
from src.execution.models import OrderSide
from src.execution.order_router import OrderRouter
from src.monitoring.logger import get_logger
from src.positions.exceptions import (
    PositionError,
    PositionLockError,
    PositionNotFoundError,
)
from src.positions.models import (
    EmergencyExitResult,
    ExitReason,
    ExitTriggerResult,
    Position,
    PositionState,
    ProtectiveOrderPair,
)
from src.positions.protective import ProtectiveOrderManager
from src.risk.models import SizingResult
from src.state.repository import OrderRecord, PositionRecord, StateRepository
from src.strategy.models import Direction, TradeSignal

logger = get_logger("positions.manager")


class PositionManager:
  """Manages the full lifecycle of bot positions, enforcing the global 1-position invariant."""

  def __init__(
      self,
      exchange_client: BinanceFuturesClient,
      repository: StateRepository,
      order_router: OrderRouter,
      protective_manager: ProtectiveOrderManager,
  ):
    self.client = exchange_client
    self.repository = repository
    self.order_router = order_router
    self.protective_manager = protective_manager

    self._current_position: Optional[Position] = None
    self._lock = asyncio.Lock()

  def has_open_position(self) -> bool:
    """Returns True if a position is currently open or pending.

    Used by the MultiSymbolStrategyEngine to reject new candidate confirmations.
    Checks in-memory state first, falling back to persistent database query.
    """
    if self._current_position is not None and self._current_position.is_active:
      return True

    open_rec = self.repository.get_open_position()
    return open_rec is not None

  def get_current_position(self) -> Optional[Position]:
    """Returns the currently active in-memory position if any."""
    return self._current_position

  def set_current_position(self, position: Optional[Position]) -> None:
    """Explicitly sets or clears the in-memory position (used by StartupReconciler)."""
    self._current_position = position

  async def open_position(
      self,
      signal: TradeSignal,
      sizing: SizingResult,
      tick_size: float,
      step_size: float,
  ) -> Position:
    """Opens a new position from a confirmed TradeSignal and validated SizingResult.

    Execution Flow:
    1. Enforce global 1-position lock invariant.
    2. Persist initial position record with status PENDING_ENTRY in database.
    3. Execute market entry order via OrderRouter.
    4. Save entry order record and update position record with actual fill metrics.
    5. Place exchange-side STOP_MARKET and TAKE_PROFIT_MARKET protective orders via ProtectiveOrderManager.
    6. Save protective order records in database.
    7. Update position status to OPEN in database and memory.
    8. Write audit log event.

    Args:
        signal: Confirmed winning trade signal.
        sizing: Friction-adjusted sizing and risk validation result.
        tick_size: Exchange PRICE_FILTER tickSize.
        step_size: Exchange LOT_SIZE stepSize.

    Returns:
        Position: The confirmed open position with active protective orders.

    Raises:
        PositionLockError: If a position is already open or pending.
        ProtectiveOrderError: If protective orders fail and emergency mitigation executes.
        PositionError: On fatal execution or database failures.
    """
    if self.has_open_position():
      raise PositionLockError(
          "Cannot open position: global 1-position lock is active."
      )

    async with self._lock:
      if self.has_open_position():
        raise PositionLockError(
            "Cannot open position: global 1-position lock is active."
        )

      now_ms = int(time.time() * 1000)
      position_id = f"pos_{signal.symbol}_{now_ms}"
      entry_side = (
          OrderSide.BUY if signal.direction == Direction.LONG else OrderSide.SELL
      )

      # 1. Create and persist initial position record
      pos_rec = PositionRecord(
          position_id=position_id,
          symbol=signal.symbol,
          side=entry_side.value,
          status=PositionState.PENDING_ENTRY.value,
          quantity=sizing.final_quantity,
          entry_price=sizing.entry_price,
          leverage=sizing.leverage,
          stop_loss_price=sizing.sl_price,
          take_profit_price=sizing.tp_price,
          created_at=now_ms,
          updated_at=now_ms,
      )
      self.repository.save_position(pos_rec)
      self.repository.log_audit(
          "POSITION_PENDING_ENTRY",
          "POSITION",
          position_id,
          f"Symbol={signal.symbol}, side={entry_side.value}, qty={sizing.final_quantity}",
      )

      self._current_position = Position(
          position_id=position_id,
          symbol=signal.symbol,
          side=entry_side,
          status=PositionState.PENDING_ENTRY,
          quantity=sizing.final_quantity,
          entry_price=sizing.entry_price,
          leverage=sizing.leverage,
          stop_loss_price=sizing.sl_price,
          take_profit_price=sizing.tp_price,
          created_at=now_ms,
          updated_at=now_ms,
      )

      # 2. Execute market entry order
      try:
        entry_fill = await self.order_router.execute_market_entry(
            symbol=signal.symbol,
            side=entry_side,
            quantity=sizing.final_quantity,
            leverage=sizing.leverage,
        )
      except Exception as exc:
        logger.error("Market entry failed for %s: %s", signal.symbol, exc)
        fail_ms = int(time.time() * 1000)
        pos_rec.status = PositionState.CLOSED.value
        pos_rec.exit_reason = f"ENTRY_FAILED: {exc}"
        pos_rec.closed_at = fail_ms
        pos_rec.updated_at = fail_ms
        self.repository.save_position(pos_rec)
        self.repository.log_audit("ENTRY_FAILED", "POSITION", position_id, str(exc))
        self._current_position = None
        raise

      # 3. Save entry order to database
      entry_order = OrderRecord(
          client_order_id=entry_fill.client_order_id,
          exchange_order_id=entry_fill.order_id,
          symbol=signal.symbol,
          position_id=position_id,
          order_type="MARKET",
          side=entry_side.value,
          status=entry_fill.status.value,
          orig_qty=entry_fill.orig_qty,
          executed_qty=entry_fill.executed_qty,
          avg_price=entry_fill.avg_price,
          reduce_only=False,
          created_at=entry_fill.transact_time or now_ms,
          updated_at=int(time.time() * 1000),
      )
      self.repository.save_order(entry_order)

      # Update position with actual fill metrics
      pos_rec.quantity = entry_fill.executed_qty
      pos_rec.entry_price = entry_fill.avg_price
      pos_rec.updated_at = int(time.time() * 1000)
      self.repository.save_position(pos_rec)

      # 4. Place and verify protective orders
      try:
        protective_pair = await self.protective_manager.place_protective_orders(
            symbol=signal.symbol,
            entry_side=entry_side,
            executed_qty=entry_fill.executed_qty,
            avg_entry_price=entry_fill.avg_price,
            stop_loss_price=sizing.sl_price,
            tick_size=tick_size,
            step_size=step_size,
            take_profit_price=sizing.tp_price,
        )
      except Exception as exc:
        logger.critical(
            "Protective orders failed for %s: %s. Position was emergency closed.",
            signal.symbol,
            exc,
        )
        fail_ms = int(time.time() * 1000)
        pos_rec.status = PositionState.CLOSED.value
        pos_rec.exit_reason = f"PROTECTION_FAILED_EMERGENCY: {exc}"
        pos_rec.closed_at = fail_ms
        pos_rec.updated_at = fail_ms
        self.repository.save_position(pos_rec)
        self.repository.log_audit(
            "PROTECTION_FAILED", "POSITION", position_id, str(exc)
        )
        self._current_position = None
        raise

      # 5. Save protective orders to database
      prot_now_ms = int(time.time() * 1000)
      sl_order = OrderRecord(
          client_order_id=protective_pair.sl_client_order_id,
          exchange_order_id=protective_pair.sl_order_id,
          symbol=signal.symbol,
          position_id=position_id,
          order_type="STOP_MARKET",
          side=protective_pair.exit_side.value,
          status="NEW",
          orig_qty=protective_pair.quantity,
          stop_price=protective_pair.sl_price,
          reduce_only=True,
          created_at=protective_pair.created_at,
          updated_at=prot_now_ms,
      )
      tp_order = OrderRecord(
          client_order_id=protective_pair.tp_client_order_id,
          exchange_order_id=protective_pair.tp_order_id,
          symbol=signal.symbol,
          position_id=position_id,
          order_type="TAKE_PROFIT_MARKET",
          side=protective_pair.exit_side.value,
          status="NEW",
          orig_qty=protective_pair.quantity,
          stop_price=protective_pair.tp_price,
          reduce_only=True,
          created_at=protective_pair.created_at,
          updated_at=prot_now_ms,
      )
      self.repository.save_order(sl_order)
      self.repository.save_order(tp_order)

      # 6. Update position to OPEN in database and memory
      pos_rec.status = PositionState.OPEN.value
      pos_rec.stop_loss_price = protective_pair.sl_price
      pos_rec.take_profit_price = protective_pair.tp_price
      pos_rec.sl_client_order_id = protective_pair.sl_client_order_id
      pos_rec.tp_client_order_id = protective_pair.tp_client_order_id
      pos_rec.updated_at = prot_now_ms
      self.repository.save_position(pos_rec)

      self._current_position = Position(
          position_id=position_id,
          symbol=signal.symbol,
          side=entry_side,
          status=PositionState.OPEN,
          quantity=entry_fill.executed_qty,
          entry_price=entry_fill.avg_price,
          leverage=sizing.leverage,
          stop_loss_price=protective_pair.sl_price,
          take_profit_price=protective_pair.tp_price,
          created_at=pos_rec.created_at,
          updated_at=prot_now_ms,
          protective_pair=protective_pair,
      )

      self.repository.log_audit(
          "POSITION_OPENED",
          "POSITION",
          position_id,
          f"Entry={entry_fill.avg_price:.4f}, SL={protective_pair.sl_price:.4f}, TP={protective_pair.tp_price:.4f}",
      )

      logger.info(
          "Position %s OPENED for %s: qty=%.6f, entry=%.4f, SL=%.4f, TP=%.4f",
          position_id,
          signal.symbol,
          entry_fill.executed_qty,
          entry_fill.avg_price,
          protective_pair.sl_price,
          protective_pair.tp_price,
      )
      return self._current_position

  async def handle_exit_fill(
      self,
      triggered_client_order_id: Optional[str] = None,
      triggered_order_id: Optional[int] = None,
      exit_price: Optional[float] = None,
      exit_qty: Optional[float] = None,
  ) -> ExitTriggerResult:
    """Handles an exit fill event (SL or TP triggering), cancels sibling, and closes position.

    Execution Flow:
    1. Identify current active position from memory or database.
    2. Transition status to PENDING_EXIT.
    3. Coordinate sibling cancellation via ProtectiveOrderManager.
    4. Calculate realized PnL based on actual exit price and fill quantity.
    5. Update position in database to CLOSED with exit metrics.
    6. Update order records (triggered -> FILLED, sibling -> CANCELED).
    7. Clear in-memory position and release lock.
    8. Write audit log event.

    Args:
        triggered_client_order_id: ClientOrderId of triggered SL/TP.
        triggered_order_id: Optional exchange orderId.
        exit_price: Fill price (defaults to order stop price if None).
        exit_qty: Fill quantity (defaults to position quantity if None).

    Returns:
        ExitTriggerResult: Detailed outcome of sibling cancellation and closure verification.

    Raises:
        PositionNotFoundError: If no active position exists to handle.
    """
    async with self._lock:
      pos = self._current_position
      if pos is None:
        open_rec = self.repository.get_open_position()
        if open_rec is None:
          raise PositionNotFoundError(
              "No active or open position found to handle exit fill."
          )
        side = OrderSide.BUY if open_rec.side.upper() == "BUY" else OrderSide.SELL
        pos = Position(
            position_id=open_rec.position_id,
            symbol=open_rec.symbol,
            side=side,
            status=PositionState(open_rec.status),
            quantity=open_rec.quantity,
            entry_price=open_rec.entry_price,
            leverage=open_rec.leverage,
            stop_loss_price=open_rec.stop_loss_price,
            take_profit_price=open_rec.take_profit_price,
            created_at=open_rec.created_at,
            updated_at=open_rec.updated_at,
            protective_pair=self.protective_manager.get_active_pair(
                open_rec.symbol
            ),
        )
        self._current_position = pos

      # Mark PENDING_EXIT
      pos.status = PositionState.PENDING_EXIT
      pos_rec = self.repository.get_position(pos.position_id)
      if pos_rec:
        pos_rec.status = PositionState.PENDING_EXIT.value
        pos_rec.updated_at = int(time.time() * 1000)
        self.repository.save_position(pos_rec)

      # Cancel sibling order
      exit_result = await self.protective_manager.cancel_sibling_order(
          triggered_client_order_id=triggered_client_order_id,
          triggered_order_id=triggered_order_id,
          active_pair=pos.protective_pair,
          symbol=pos.symbol,
      )

      # Determine actual exit price
      actual_exit_price = exit_price
      if actual_exit_price is None:
        if exit_result.exit_reason == ExitReason.STOP_LOSS:
          actual_exit_price = pos.stop_loss_price
        else:
          actual_exit_price = pos.take_profit_price

      actual_qty = exit_qty if (exit_qty and exit_qty > 0) else pos.quantity

      # Calculate realized PnL
      if pos.side == OrderSide.BUY:
        pnl = (actual_exit_price - pos.entry_price) * actual_qty
      else:
        pnl = (pos.entry_price - actual_exit_price) * actual_qty

      # Update database position
      self.repository.close_position(
          position_id=pos.position_id,
          exit_price=actual_exit_price,
          realized_pnl=pnl,
          exit_reason=exit_result.exit_reason.value,
          closed_at=exit_result.timestamp_ms,
      )

      # Update database order statuses
      if triggered_client_order_id:
        self.repository.update_order_status(
            client_order_id=triggered_client_order_id,
            status="FILLED",
            executed_qty=actual_qty,
            avg_price=actual_exit_price,
        )
      if exit_result.cancelled_sibling_client_order_id:
        self.repository.update_order_status(
            client_order_id=exit_result.cancelled_sibling_client_order_id,
            status="CANCELED",
        )

      # Log audit event
      self.repository.log_audit(
          "POSITION_CLOSED",
          "POSITION",
          pos.position_id,
          f"Exit={exit_result.exit_reason.value}, Price={actual_exit_price:.4f}, PnL={pnl:.4f}",
      )

      logger.info(
          "Position %s CLOSED: exit=%s @ %.4f, PnL=%.4f",
          pos.position_id,
          exit_result.exit_reason.value,
          actual_exit_price,
          pnl,
      )

      pos.status = PositionState.CLOSED
      pos.exit_price = actual_exit_price
      pos.realized_pnl = pnl
      pos.exit_reason = exit_result.exit_reason.value
      pos.closed_at = exit_result.timestamp_ms
      self._current_position = None

      return exit_result

  async def emergency_close_current_position(
      self, reason: str = "MANUAL_EMERGENCY"
  ) -> EmergencyExitResult:
    """Immediately submits a reduceOnly market order to close any active position."""
    async with self._lock:
      pos = self._current_position
      if pos is None:
        open_rec = self.repository.get_open_position()
        if open_rec is None:
          raise PositionNotFoundError("No open position to emergency close.")
        side = OrderSide.BUY if open_rec.side.upper() == "BUY" else OrderSide.SELL
        pos = Position(
            position_id=open_rec.position_id,
            symbol=open_rec.symbol,
            side=side,
            status=PositionState(open_rec.status),
            quantity=open_rec.quantity,
            entry_price=open_rec.entry_price,
            leverage=open_rec.leverage,
            stop_loss_price=open_rec.stop_loss_price,
            take_profit_price=open_rec.take_profit_price,
            created_at=open_rec.created_at,
            updated_at=open_rec.updated_at,
            protective_pair=self.protective_manager.get_active_pair(
                open_rec.symbol
            ),
        )
        self._current_position = pos

      emg_res = await self.protective_manager.execute_emergency_market_close(
          symbol=pos.symbol,
          position_side=pos.side,
          quantity=pos.quantity,
      )

      # Cancel protective orders if active
      pair = self.protective_manager.get_active_pair(pos.symbol)
      if pair:
        for cid in (pair.sl_client_order_id, pair.tp_client_order_id):
          try:
            await self.order_router.cancel_order(pos.symbol, client_order_id=cid)
          except Exception:
            pass

      now_ms = int(time.time() * 1000)
      self.repository.close_position(
          position_id=pos.position_id,
          exit_price=pos.entry_price,
          realized_pnl=0.0,
          exit_reason=reason,
          closed_at=now_ms,
      )
      self.repository.log_audit(
          "EMERGENCY_CLOSE",
          "POSITION",
          pos.position_id,
          f"Reason={reason}, Closed={emg_res.is_closed}",
      )
      pos.status = PositionState.CLOSED
      pos.closed_at = now_ms
      self._current_position = None
      return emg_res
