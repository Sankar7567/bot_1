"""Execution models, schemas, and typed data transfer objects."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class OrderSide(str, Enum):
  BUY = "BUY"
  SELL = "SELL"


class OrderType(str, Enum):
  MARKET = "MARKET"
  LIMIT = "LIMIT"
  STOP_MARKET = "STOP_MARKET"
  TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"


class OrderStatus(str, Enum):
  NEW = "NEW"
  PARTIALLY_FILLED = "PARTIALLY_FILLED"
  FILLED = "FILLED"
  CANCELED = "CANCELED"
  REJECTED = "REJECTED"
  EXPIRED = "EXPIRED"


@dataclass(frozen=True)
class OrderExecutionRequest:
  """Request parameters for submitting an order."""

  symbol: str
  side: OrderSide
  order_type: OrderType
  quantity: float
  client_order_id: str
  reduce_only: bool = False
  stop_price: Optional[float] = None
  price: Optional[float] = None
  time_in_force: Optional[str] = None

  def to_binance_params(self) -> Dict[str, Any]:
    """Serializes to Binance Futures REST API query parameters."""
    params: Dict[str, Any] = {
        "symbol": self.symbol.upper(),
        "side": self.side.value,
        "type": self.order_type.value,
        "quantity": self.quantity,
        "newClientOrderId": self.client_order_id,
    }
    if self.reduce_only:
      params["reduceOnly"] = "true"
    if self.stop_price is not None:
      params["stopPrice"] = self.stop_price
    if self.price is not None:
      params["price"] = self.price
    if self.time_in_force is not None:
      params["timeInForce"] = self.time_in_force
    return params


@dataclass(frozen=True)
class OrderFill:
  """Confirmed fill information from order execution."""

  symbol: str
  client_order_id: str
  order_id: int
  status: OrderStatus
  side: OrderSide
  order_type: OrderType
  orig_qty: float
  executed_qty: float
  avg_price: float
  cum_quote: float
  commission: float = 0.0
  commission_asset: str = "USDT"
  transact_time: int = 0
  raw_response: Dict[str, Any] = field(default_factory=dict)

  @property
  def is_fully_filled(self) -> bool:
    return self.status == OrderStatus.FILLED

  @property
  def is_partially_filled(self) -> bool:
    return self.status == OrderStatus.PARTIALLY_FILLED

  @classmethod
  def from_raw_response(
      cls, raw: Dict[str, Any], default_type: OrderType = OrderType.MARKET
  ) -> "OrderFill":
    """Constructs an OrderFill from Binance /fapi/v1/order response."""
    status_str = raw.get("status", "NEW").upper()
    try:
      status = OrderStatus(status_str)
    except ValueError:
      status = OrderStatus.NEW

    side_str = raw.get("side", "BUY").upper()
    side = OrderSide(side_str) if side_str in OrderSide.__members__ else OrderSide.BUY

    type_str = raw.get("type", default_type.value).upper()
    order_type = (
        OrderType(type_str)
        if type_str in OrderType.__members__
        else default_type
    )

    orig_qty = float(raw.get("origQty", 0.0))
    executed_qty = float(raw.get("executedQty", 0.0))
    cum_quote = float(raw.get("cumQuote", 0.0))

    avg_price_raw = raw.get("avgPrice")
    if avg_price_raw is not None and float(avg_price_raw) > 0:
      avg_price = float(avg_price_raw)
    elif executed_qty > 0 and cum_quote > 0:
      avg_price = cum_quote / executed_qty
    else:
      avg_price = float(raw.get("price", 0.0))

    transact_time = int(raw.get("updateTime") or raw.get("transactTime") or 0)
    order_id = int(raw.get("orderId", 0))
    client_order_id = raw.get("clientOrderId", "")

    return cls(
        symbol=raw.get("symbol", ""),
        client_order_id=client_order_id,
        order_id=order_id,
        status=status,
        side=side,
        order_type=order_type,
        orig_qty=orig_qty,
        executed_qty=executed_qty,
        avg_price=avg_price,
        cum_quote=cum_quote,
        transact_time=transact_time,
        raw_response=raw,
    )
