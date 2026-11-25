"""Historical Exchange Adapter simulating Binance USD(S)-M Futures execution."""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from src.backtesting.clock import SimulatedClock
from src.backtesting.data_adapter import HistoricalMarketStore
from src.backtesting.ledger import AccountLedger
from src.backtesting.models import ExecutionAssumptions
from src.exchange.exceptions import BinanceAPIError
from src.exchange.models import (
    AccountInfo,
    OpenOrder,
    PositionRisk,
    SymbolFilter,
    SymbolInfo,
    SymbolLeverageBrackets,
    Ticker24hr,
)
from src.market_data.models import Candle


@dataclass
class SimulatedOrder:
  """In-memory exchange order representation inside HistoricalExchangeAdapter."""

  order_id: int
  client_order_id: str
  symbol: str
  side: str
  order_type: str
  status: str
  orig_qty: float
  executed_qty: float = 0.0
  avg_price: float = 0.0
  stop_price: float = 0.0
  reduce_only: bool = False
  update_time: int = 0


@dataclass
class SimulatedPosition:
  """In-memory active position inside HistoricalExchangeAdapter."""

  symbol: str
  side: str  # BUY or SELL
  quantity: float
  entry_price: float
  leverage: int
  isolated_margin: float
  liq_price: float
  entry_time_ms: int
  entry_fee: float
  entry_spread_cost: float
  entry_slippage_cost: float
  funding_paid: float = 0.0


@dataclass
class ExitSimulationResult:
  """Detailed outcome when a bar triggers SL, TP, or Liquidation."""

  symbol: str
  triggered_client_order_id: str
  triggered_order_id: int
  exit_reason: str
  trigger_price: float
  fill_price: float
  fill_timestamp_ms: int
  gross_pnl: float
  exit_fee: float
  spread_cost: float
  slippage_cost: float
  funding_paid: float
  is_liquidation: bool = False
  ambiguity_note: str = ""


class HistoricalExchangeAdapter:
  """Drop-in async adapter implementing BinanceFuturesClient methods against historical state."""

  def __init__(
      self,
      clock: SimulatedClock,
      store: HistoricalMarketStore,
      ledger: AccountLedger,
      assumptions: Optional[ExecutionAssumptions] = None,
      min_24h_coverage_ratio: float = 0.80,
  ):
    self.clock = clock
    self.store = store
    self.ledger = ledger
    self.assumptions = assumptions or ExecutionAssumptions()
    self.min_24h_coverage_ratio = min_24h_coverage_ratio

    self._next_order_id = 100_000
    self._orders_by_cid: Dict[str, SimulatedOrder] = {}
    self._leverage_by_symbol: Dict[str, int] = {}
    self._margin_type_by_symbol: Dict[str, str] = {}
    self._active_position: Optional[SimulatedPosition] = None

    # Last entry execution metadata for audit recording
    self.last_entry_meta: Optional[Dict[str, Any]] = None

  # --------------------------------------------------------------------------
  # Time & Market Data Methods (Read-Only up to SimulatedClock)
  # --------------------------------------------------------------------------

  def get_synchronized_time(self) -> int:
    return self.clock.now_ms()

  async def get_exchange_info(self) -> Dict[str, SymbolInfo]:
    return self.store.get_exchange_info_at(self.clock.now_ms())

  async def get_24hr_tickers(self) -> List[Ticker24hr]:
    return self.store.reconstruct_24h_tickers(
        at_ms=self.clock.now_ms(),
        min_coverage_ratio=self.min_24h_coverage_ratio,
    )

  async def get_klines(
      self,
      symbol: str,
      interval: str,
      limit: int = 100,
      start_time: Optional[int] = None,
      end_time: Optional[int] = None,
  ) -> List[List[Any]]:
    up_to = min(self.clock.now_ms(), end_time) if end_time else self.clock.now_ms()
    candles = self.store.get_klines_up_to(symbol, interval, up_to, limit=limit)
    return [
        [
            c.open_time,
            str(c.open),
            str(c.high),
            str(c.low),
            str(c.close),
            str(c.volume),
            c.close_time,
            str(c.quote_volume),
            c.trades_count,
            "0.0",
            "0.0",
            "0",
        ]
        for c in candles
    ]

  async def get_symbol_filters(self, symbol: str) -> SymbolFilter:
    meta = self.store.get_contract(symbol)
    if not meta or not meta.is_listed_at(self.clock.now_ms()):
      raise BinanceAPIError(f"Symbol {symbol} unavailable at {self.clock.now_ms()}", -1121)
    return meta.filters

  async def get_leverage_brackets(
      self, symbol: Optional[str] = None
  ) -> Dict[str, SymbolLeverageBrackets]:
    res: Dict[str, SymbolLeverageBrackets] = {}
    targets = [symbol.upper()] if symbol else self.store.get_all_symbols()
    for sym in targets:
      meta = self.store.get_contract(sym)
      if meta:
        res[sym] = meta.brackets
    return res

  # --------------------------------------------------------------------------
  # Account & Position Methods
  # --------------------------------------------------------------------------

  async def get_account_info(self) -> AccountInfo:
    return AccountInfo(
        fee_tier=0,
        can_trade=True,
        can_deposit=True,
        can_withdraw=True,
        total_margin_balance=self.ledger.wallet_balance,
        total_wallet_balance=self.ledger.wallet_balance,
        total_unrealized_profit=0.0,
        available_balance=self.ledger.available_balance,
    )

  async def get_positions(
      self, symbol: Optional[str] = None
  ) -> List[PositionRisk]:
    pos = self._active_position
    if pos is None:
      if symbol:
        return [
            PositionRisk(
                symbol=symbol.upper(),
                position_amt=0.0,
                entry_price=0.0,
                mark_price=0.0,
                unrealized_profit=0.0,
                liquidation_price=0.0,
                leverage=self._leverage_by_symbol.get(symbol.upper(), 1),
                margin_type="isolated",
                position_side="BOTH",
            )
        ]
      return []

    if symbol and pos.symbol != symbol.upper():
      return []

    amt = pos.quantity if pos.side == "BUY" else -pos.quantity
    return [
        PositionRisk(
            symbol=pos.symbol,
            position_amt=amt,
            entry_price=pos.entry_price,
            mark_price=pos.entry_price,
            unrealized_profit=0.0,
            liquidation_price=pos.liq_price,
            leverage=pos.leverage,
            margin_type="isolated",
            position_side="BOTH",
        )
    ]

  async def get_open_orders(
      self, symbol: Optional[str] = None
  ) -> List[OpenOrder]:
    res: List[OpenOrder] = []
    for o in self._orders_by_cid.values():
      if o.status == "NEW" and (symbol is None or o.symbol == symbol.upper()):
        res.append(
            OpenOrder(
                symbol=o.symbol,
                order_id=o.order_id,
                client_order_id=o.client_order_id,
                price=0.0,
                orig_qty=o.orig_qty,
                executed_qty=o.executed_qty,
                status=o.status,
                order_type=o.order_type,
                side=o.side,
                time_in_force="GTC",
                stop_price=o.stop_price,
                reduce_only=o.reduce_only,
                update_time=o.update_time,
            )
        )
    return res

  async def change_margin_type(
      self, symbol: str, margin_type: str = "ISOLATED"
  ) -> Dict[str, Any]:
    self._margin_type_by_symbol[symbol.upper()] = margin_type.upper()
    return {"code": 200, "msg": "success"}

  async def change_leverage(self, symbol: str, leverage: int) -> Dict[str, Any]:
    self._leverage_by_symbol[symbol.upper()] = leverage
    return {"leverage": leverage, "symbol": symbol.upper()}

  # --------------------------------------------------------------------------
  # Order Execution Simulation
  # --------------------------------------------------------------------------

  async def place_order(self, params: Dict[str, Any]) -> Dict[str, Any]:
    sym = str(params["symbol"]).upper()
    side = str(params["side"]).upper()
    otype = str(params["type"]).upper()
    qty = float(params["quantity"])
    cid = str(params["newClientOrderId"])
    reduce_only = str(params.get("reduceOnly", "false")).lower() == "true"
    now_ms = self.clock.now_ms()

    self._next_order_id += 1
    oid = self._next_order_id

    # Market Entry Order: executes on next available executable price strictly after signal
    if otype == "MARKET" and not reduce_only:
      next_bar = self.store.get_next_1m_candle_after(sym, now_ms)
      if next_bar is None:
        raise BinanceAPIError(
            f"No subsequent executable market bar available after {now_ms} for {sym}",
            -2010,
        )

      raw_price = next_bar.open
      half_spread = self.assumptions.half_spread_bps / 10000.0
      slip = self.assumptions.entry_slippage_bps / 10000.0
      if side == "BUY":
        fill_price = raw_price * (1.0 + half_spread + slip)
      else:
        fill_price = raw_price * (1.0 - half_spread - slip)

      spread_cost = qty * raw_price * half_spread
      slippage_cost = qty * raw_price * slip
      notional = qty * fill_price
      entry_fee = notional * self.assumptions.taker_fee_rate
      leverage = self._leverage_by_symbol.get(sym, 1)
      margin = notional / leverage

      # Estimate liquidation price from contract bracket
      meta = self.store.get_contract(sym)
      mmr = 0.005
      if meta and meta.brackets.brackets:
        mmr = meta.brackets.brackets[0].maint_margin_ratio
      if side == "BUY":
        liq_price = fill_price * (1.0 - (1.0 / leverage) + mmr)
      else:
        liq_price = fill_price * (1.0 + (1.0 / leverage) - mmr)

      self.ledger.lock_margin(margin)
      self.ledger.record_exposure(notional)
      self.ledger.charge_entry_fee(entry_fee, spread_cost, slippage_cost)

      fill_ts = max(now_ms + self.assumptions.signal_to_order_latency_ms, next_bar.open_time)
      self._active_position = SimulatedPosition(
          symbol=sym,
          side=side,
          quantity=qty,
          entry_price=fill_price,
          leverage=leverage,
          isolated_margin=margin,
          liq_price=liq_price,
          entry_time_ms=fill_ts,
          entry_fee=entry_fee,
          entry_spread_cost=spread_cost,
          entry_slippage_cost=slippage_cost,
      )
      self.last_entry_meta = {
          "signal_timestamp": now_ms,
          "order_submitted_at": now_ms + self.assumptions.signal_to_order_latency_ms,
          "fill_timestamp": fill_ts,
          "fill_price": fill_price,
          "entry_fee": entry_fee,
          "spread_cost": spread_cost,
          "slippage_cost": slippage_cost,
          "isolated_margin": margin,
      }

      order = SimulatedOrder(
          order_id=oid,
          client_order_id=cid,
          symbol=sym,
          side=side,
          order_type=otype,
          status="FILLED",
          orig_qty=qty,
          executed_qty=qty,
          avg_price=fill_price,
          reduce_only=False,
          update_time=fill_ts,
      )
      self._orders_by_cid[cid] = order
      return {
          "orderId": oid,
          "clientOrderId": cid,
          "symbol": sym,
          "status": "FILLED",
          "side": side,
          "type": otype,
          "origQty": str(qty),
          "executedQty": str(qty),
          "avgPrice": str(fill_price),
          "cumQuote": str(notional),
          "commission": str(entry_fee),
          "updateTime": fill_ts,
      }

    # Emergency Reduce-Only Market Close
    if otype == "MARKET" and reduce_only:
      bars = self.store.get_klines_up_to(sym, "1m", now_ms, limit=1)
      close_px = bars[-1].close if bars else (
          self._active_position.entry_price if self._active_position else 0.0
      )
      if self._active_position and self._active_position.symbol == sym:
        pos = self._active_position
        gross = (
            (close_px - pos.entry_price) * pos.quantity
            if pos.side == "BUY"
            else (pos.entry_price - close_px) * pos.quantity
        )
        fee = pos.quantity * close_px * self.assumptions.taker_fee_rate
        self.ledger.settle_trade_exit(now_ms, gross, fee, 0.0, 0.0)
        self._active_position = None

      order = SimulatedOrder(
          order_id=oid,
          client_order_id=cid,
          symbol=sym,
          side=side,
          order_type=otype,
          status="FILLED",
          orig_qty=qty,
          executed_qty=qty,
          avg_price=close_px,
          reduce_only=True,
          update_time=now_ms,
      )
      self._orders_by_cid[cid] = order
      return {
          "orderId": oid,
          "clientOrderId": cid,
          "symbol": sym,
          "status": "FILLED",
          "side": side,
          "type": otype,
          "origQty": str(qty),
          "executedQty": str(qty),
          "avgPrice": str(close_px),
          "updateTime": now_ms,
      }

    # Conditional Protective Order (STOP_MARKET / TAKE_PROFIT_MARKET)
    stop_price = float(params["stopPrice"])
    order = SimulatedOrder(
        order_id=oid,
        client_order_id=cid,
        symbol=sym,
        side=side,
        order_type=otype,
        status="NEW",
        orig_qty=qty,
        stop_price=stop_price,
        reduce_only=reduce_only,
        update_time=now_ms,
    )
    self._orders_by_cid[cid] = order
    return {
        "orderId": oid,
        "clientOrderId": cid,
        "symbol": sym,
        "status": "NEW",
        "side": side,
        "type": otype,
        "origQty": str(qty),
        "stopPrice": str(stop_price),
        "reduceOnly": reduce_only,
        "updateTime": now_ms,
    }

  async def query_order(
      self,
      symbol: str,
      client_order_id: Optional[str] = None,
      order_id: Optional[int] = None,
  ) -> Dict[str, Any]:
    order = self._orders_by_cid.get(client_order_id or "")
    if order is None:
      raise BinanceAPIError("Order does not exist", -2013)
    return {
        "orderId": order.order_id,
        "clientOrderId": order.client_order_id,
        "symbol": order.symbol,
        "status": order.status,
        "side": order.side,
        "type": order.order_type,
        "origQty": str(order.orig_qty),
        "executedQty": str(order.executed_qty),
        "avgPrice": str(order.avg_price),
        "stopPrice": str(order.stop_price),
    }

  async def cancel_order(
      self,
      symbol: str,
      client_order_id: Optional[str] = None,
      order_id: Optional[int] = None,
  ) -> Dict[str, Any]:
    order = self._orders_by_cid.get(client_order_id or "")
    if order is None or order.status != "NEW":
      raise BinanceAPIError("Unknown order sent.", -2011)
    order.status = "CANCELED"
    order.update_time = self.clock.now_ms()
    return {
        "orderId": order.order_id,
        "clientOrderId": order.client_order_id,
        "status": "CANCELED",
    }

  # --------------------------------------------------------------------------
  # Historical Bar Evaluation (Funding & SL/TP Triggers on Actual OHLC)
  # --------------------------------------------------------------------------

  def apply_funding_if_due(self, bar_1m: Candle) -> None:
    """Applies 8h funding payment if an 8-hour UTC boundary falls in this 1m bar."""
    pos = self._active_position
    if pos is None or pos.symbol != bar_1m.symbol:
      return
    if bar_1m.close_time <= pos.entry_time_ms:
      return

    # 8h boundaries: 00:00, 08:00, 16:00 UTC (28,800,000 ms)
    eight_h = 28_800_000
    prev_slot = (bar_1m.open_time - 1) // eight_h
    curr_slot = bar_1m.close_time // eight_h
    if curr_slot > prev_slot:
      notional = pos.quantity * bar_1m.close
      funding = notional * self.assumptions.funding_rate_8h
      pos.funding_paid += funding
      self.ledger.apply_funding(funding)

  def evaluate_bar_triggers(
      self,
      bar_1m: Candle,
      sub_bars: Optional[List[Candle]] = None,
  ) -> Optional[ExitSimulationResult]:
    """Checks active SL/TP/Liquidation against actual market OHLC prices."""
    pos = self._active_position
    if pos is None or pos.symbol != bar_1m.symbol:
      return None

    # Do not trigger before the entry fill bar itself
    if bar_1m.close_time < pos.entry_time_ms:
      return None

    sl_order: Optional[SimulatedOrder] = None
    tp_order: Optional[SimulatedOrder] = None
    for o in self._orders_by_cid.values():
      if o.symbol == pos.symbol and o.status == "NEW":
        if o.order_type == "STOP_MARKET":
          sl_order = o
        elif o.order_type == "TAKE_PROFIT_MARKET":
          tp_order = o

    if sl_order is None and tp_order is None:
      return None

    is_long = pos.side == "BUY"

    def _check_candle(c: Candle) -> tuple[bool, bool, bool]:
      if is_long:
        liq_hit = c.low <= pos.liq_price
        sl_hit = sl_order is not None and c.low <= sl_order.stop_price
        tp_hit = tp_order is not None and c.high >= tp_order.stop_price
      else:
        liq_hit = c.high >= pos.liq_price
        sl_hit = sl_order is not None and c.high >= sl_order.stop_price
        tp_hit = tp_order is not None and c.low <= tp_order.stop_price
      return liq_hit, sl_hit, tp_hit

    liq_hit, sl_hit, tp_hit = _check_candle(bar_1m)
    if not (liq_hit or sl_hit or tp_hit):
      return None

    ambiguity_note = ""
    chosen = ""  # "LIQ", "STOP_LOSS", "TAKE_PROFIT"

    # Disambiguate if both SL and TP touched in same 1m bar
    if sl_hit and tp_hit:
      if sub_bars:
        for sb in sub_bars:
          sb_liq, sb_sl, sb_tp = _check_candle(sb)
          if sb_liq:
            chosen = "LIQ"
            ambiguity_note = "RESOLVED_VIA_SUB_BAR: LIQ"
            break
          if sb_sl and not sb_tp:
            chosen = "STOP_LOSS"
            ambiguity_note = "RESOLVED_VIA_SUB_BAR: SL_FIRST"
            break
          if sb_tp and not sb_sl:
            chosen = "TAKE_PROFIT"
            ambiguity_note = "RESOLVED_VIA_SUB_BAR: TP_FIRST"
            break
          if sb_sl and sb_tp:
            chosen = "STOP_LOSS"
            ambiguity_note = "CONSERVATIVE_SL_FIRST (sub-bar ambiguity)"
            break
      if not chosen:
        chosen = "STOP_LOSS"
        ambiguity_note = "CONSERVATIVE_SL_FIRST (both SL and TP touched in 1m bar)"
    elif liq_hit and (sl_order is None or (is_long and pos.liq_price >= sl_order.stop_price) or (not is_long and pos.liq_price <= sl_order.stop_price)):
      chosen = "LIQ"
    elif sl_hit:
      chosen = "STOP_LOSS"
    else:
      chosen = "TAKE_PROFIT"

    half_spread = self.assumptions.half_spread_bps / 10000.0
    if chosen == "LIQ":
      trig_order = sl_order or tp_order
      trig_price = pos.liq_price
      fill_price = trig_price
      slip_rate = 0.0
      exit_reason = "LIQUIDATION"
      is_liq = True
    elif chosen == "STOP_LOSS":
      assert sl_order is not None
      trig_order = sl_order
      trig_price = sl_order.stop_price
      slip_rate = self.assumptions.stop_slippage_bps / 10000.0
      if is_long:
        fill_price = trig_price * (1.0 - half_spread - slip_rate)
      else:
        fill_price = trig_price * (1.0 + half_spread + slip_rate)
      exit_reason = "STOP_LOSS"
      is_liq = False
    else:
      assert tp_order is not None
      trig_order = tp_order
      trig_price = tp_order.stop_price
      slip_rate = self.assumptions.tp_slippage_bps / 10000.0
      if is_long:
        fill_price = trig_price * (1.0 - half_spread - slip_rate)
      else:
        fill_price = trig_price * (1.0 + half_spread + slip_rate)
      exit_reason = "TAKE_PROFIT"
      is_liq = False

    trig_order.status = "FILLED"
    trig_order.executed_qty = pos.quantity
    trig_order.avg_price = fill_price
    trig_order.update_time = bar_1m.close_time

    gross_pnl = (
        (fill_price - pos.entry_price) * pos.quantity
        if is_long
        else (pos.entry_price - fill_price) * pos.quantity
    )
    exit_fee = pos.quantity * fill_price * self.assumptions.taker_fee_rate
    spread_cost = pos.quantity * trig_price * half_spread
    slippage_cost = pos.quantity * trig_price * slip_rate

    self.ledger.settle_trade_exit(
        timestamp_ms=bar_1m.close_time,
        gross_pnl=gross_pnl,
        exit_fee=exit_fee,
        spread_cost=spread_cost,
        slippage_cost=slippage_cost,
        is_liquidation=is_liq,
    )

    funding_paid = pos.funding_paid
    self._active_position = None

    return ExitSimulationResult(
        symbol=pos.symbol,
        triggered_client_order_id=trig_order.client_order_id,
        triggered_order_id=trig_order.order_id,
        exit_reason=exit_reason,
        trigger_price=trig_price,
        fill_price=fill_price,
        fill_timestamp_ms=bar_1m.close_time,
        gross_pnl=gross_pnl,
        exit_fee=exit_fee,
        spread_cost=spread_cost,
        slippage_cost=slippage_cost,
        funding_paid=funding_paid,
        is_liquidation=is_liq,
        ambiguity_note=ambiguity_note,
    )
