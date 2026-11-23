"""Asynchronous Binance USDⓈ-M Futures Testnet connector.

Provides signed authenticated requests, timestamp synchronization,
rate-limit tracking, exponential backoff retries, and typed data models.
"""

import asyncio
from typing import Any, Dict, List, Optional
import httpx
from pydantic import SecretStr
from src.config.settings import Settings
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
from src.exchange.signer import sign_parameters
from src.monitoring.logger import get_logger

logger = get_logger("exchange.client")


class BinanceFuturesClient:
  """Asynchronous exchange connector for Binance USDⓈ-M Futures."""

  def __init__(
      self,
      settings: Optional[Settings] = None,
      api_key: Optional[SecretStr] = None,
      api_secret: Optional[SecretStr] = None,
      base_url: Optional[str] = None,
      timeout_seconds: float = 10.0,
      max_retries: int = 3,
      retry_backoff_factor: float = 1.5,
      http_client: Optional[httpx.AsyncClient] = None,
  ):
    cfg = settings or Settings()
    self.api_key = api_key or cfg.binance_api_key
    self.api_secret = api_secret or cfg.binance_api_secret
    self.base_url = (base_url or cfg.binance_rest_base_url).rstrip("/")
    self.timeout = timeout_seconds
    self.max_retries = max_retries
    self.backoff_factor = retry_backoff_factor
    self.time_offset_ms: int = 0
    self.rate_limiter = RateLimiter()

    self._custom_client = http_client is not None
    self._client = http_client or httpx.AsyncClient(
        base_url=self.base_url,
        timeout=httpx.Timeout(self.timeout),
        headers={"User-Agent": "Binance-HA-Bot/1.0"},
    )

  def __repr__(self) -> str:
    masked_key = (
        "***" + self.api_key.get_secret_value()[-4:]
        if len(self.api_key.get_secret_value()) >= 4
        else "***"
    )
    return (
        f"BinanceFuturesClient(base_url='{self.base_url}', api_key='{masked_key}',"
        f" timeout={self.timeout})"
    )

  async def __aenter__(self) -> "BinanceFuturesClient":
    return self

  async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
    await self.close()

  async def close(self) -> None:
    """Closes the underlying HTTP client session if owned."""
    if not self._custom_client and not self._client.is_closed:
      await self._client.aclose()

  def get_synchronized_time(self) -> int:
    """Returns local UTC timestamp adjusted by Binance server offset."""
    import time

    local_ms = int(time.time() * 1000)
    return local_ms + self.time_offset_ms

  async def _request(
      self,
      method: str,
      endpoint: str,
      params: Optional[Dict[str, Any]] = None,
      is_signed: bool = False,
      weight: int = 1,
  ) -> Any:
    """Executes an HTTP request with rate-limiting, signing, error classification, and retry logic."""
    await self.rate_limiter.acquire(weight)

    request_params = {**(params or {})}
    headers: Dict[str, str] = {}

    if is_signed:
      key_val = self.api_key.get_secret_value()
      sec_val = self.api_secret.get_secret_value()
      if not key_val or not sec_val:
        raise BinanceAuthError(
            "API Key and Secret must be configured for signed requests."
        )

      headers["X-MBX-APIKEY"] = key_val
      synced_time = self.get_synchronized_time()
      request_params = sign_parameters(
          params=request_params,
          secret=sec_val,
          timestamp_ms=synced_time,
      )

    url = endpoint if endpoint.startswith("/") else f"/{endpoint}"
    attempt = 0

    while True:
      attempt += 1
      try:
        if method.upper() == "GET":
          response = await self._client.get(
              url, params=request_params, headers=headers
          )
        elif method.upper() == "POST":
          response = await self._client.post(
              url, params=request_params, headers=headers
          )
        elif method.upper() == "DELETE":
          response = await self._client.delete(
              url, params=request_params, headers=headers
          )
        else:
          raise ValueError(f"Unsupported HTTP method: {method}")

        self.rate_limiter.update_from_headers(response.headers)

        # Handle successful HTTP status
        if response.status_code == 200:
          try:
            return response.json()
          except Exception as exc:
            raise BinanceConnectorError(
                f"Failed to parse JSON response: {response.text}"
            ) from exc

        # Handle rate limits
        if response.status_code in (429, 418):
          retry_after = float(response.headers.get("Retry-After", 2.0))
          self.rate_limiter.set_backoff(retry_after)
          if attempt <= self.max_retries:
            logger.warning(
                "Rate limit encountered on %s. Backing off for %.2fs"
                " (attempt %d/%d)",
                endpoint,
                retry_after,
                attempt,
                self.max_retries,
            )
            await asyncio.sleep(retry_after)
            continue
          raise BinanceRateLimitError(
              f"Rate limit exceeded (HTTP {response.status_code}):"
              f" {response.text}",
              code=-1003,
              retry_after_seconds=retry_after,
          )

        # Parse structured Binance API error
        error_json = {}
        try:
          error_json = response.json()
        except Exception:
          pass

        error_code = error_json.get("code")
        error_msg = error_json.get("msg", response.text)

        # Classify authentication errors
        if response.status_code in (401, 403) or error_code in (
            -1021,
            -1022,
            -2014,
            -2015,
        ):
          raise BinanceAuthError(
              f"Authentication failure: {error_msg}", code=error_code
          )

        # Check for retryable server errors (5xx)
        if response.status_code in (500, 502, 503, 504):
          if attempt <= self.max_retries:
            backoff_sleep = self.backoff_factor * (2 ** (attempt - 1))
            logger.warning(
                "Server error HTTP %d on %s. Retrying in %.2fs (attempt %d/%d)",
                response.status_code,
                endpoint,
                backoff_sleep,
                attempt,
                self.max_retries,
            )
            await asyncio.sleep(backoff_sleep)
            continue

        raise BinanceAPIError(
            message=error_msg, code=error_code or -1, status_code=response.status_code
        )

      except (httpx.TransportError, httpx.TimeoutException) as net_err:
        if attempt <= self.max_retries:
          backoff_sleep = self.backoff_factor * (2 ** (attempt - 1))
          logger.warning(
              "Network error on %s: %s. Retrying in %.2fs (attempt %d/%d)",
              endpoint,
              str(net_err),
              backoff_sleep,
              attempt,
              self.max_retries,
          )
          await asyncio.sleep(backoff_sleep)
          continue
        raise BinanceNetworkError(
            f"Network transport error after {attempt} attempts: {net_err}"
        ) from net_err

  # --------------------------------------------------------------------------
  # Public Endpoints
  # --------------------------------------------------------------------------

  async def ping(self) -> bool:
    """Tests basic connectivity to Binance Futures REST API."""
    res = await self._request("GET", "/fapi/v1/ping", weight=1)
    return res == {}

  async def get_server_time(self) -> ServerTimeResponse:
    """Retrieves Binance server time and synchronizes client time offset."""
    import time

    local_time = int(time.time() * 1000)
    data = await self._request("GET", "/fapi/v1/time", weight=1)
    server_time = int(data["serverTime"])
    self.time_offset_ms = server_time - local_time
    return ServerTimeResponse(
        server_time=server_time,
        local_time=local_time,
        time_offset_ms=self.time_offset_ms,
    )

  async def get_exchange_info(self) -> Dict[str, SymbolInfo]:
    """Retrieves and parses current exchange trading rules and symbol filters."""
    data = await self._request("GET", "/fapi/v1/exchangeInfo", weight=1)
    symbols_map: Dict[str, SymbolInfo] = {}

    for s in data.get("symbols", []):
      sym_name = s.get("symbol")
      filters = SymbolFilter.from_raw_filters(
          symbol=sym_name,
          filters=s.get("filters", []),
          price_precision=int(s.get("pricePrecision", 2)),
          quantity_precision=int(s.get("quantityPrecision", 3)),
      )
      symbols_map[sym_name] = SymbolInfo(
          symbol=sym_name,
          status=s.get("status", "UNKNOWN"),
          base_asset=s.get("baseAsset", ""),
          quote_asset=s.get("quoteAsset", ""),
          contract_type=s.get("contractType", ""),
          filters=filters,
      )

    return symbols_map

  async def get_symbol_filters(self, symbol: str) -> SymbolFilter:
    """Retrieves parsed filters for a specific symbol."""
    exchange_info = await self.get_exchange_info()
    if symbol not in exchange_info:
      raise BinanceFilterError(
          f"Symbol '{symbol}' not found in exchange information."
      )
    return exchange_info[symbol].filters

  # --------------------------------------------------------------------------
  # Signed Account & Position Endpoints
  # --------------------------------------------------------------------------

  async def test_connectivity_and_auth(self) -> bool:
    """Verifies both network connectivity and API credential validity."""
    await self.ping()
    # Query account information to verify HMAC credentials
    await self.get_account_info()
    return True

  async def get_account_info(self) -> AccountInfo:
    """Retrieves account equity, wallet balances, and trading permissions."""
    data = await self._request("GET", "/fapi/v2/account", is_signed=True, weight=5)
    return AccountInfo(
        fee_tier=int(data.get("feeTier", 0)),
        can_trade=bool(data.get("canTrade", False)),
        can_deposit=bool(data.get("canDeposit", False)),
        can_withdraw=bool(data.get("canWithdraw", False)),
        total_margin_balance=float(data.get("totalMarginBalance", 0.0)),
        total_wallet_balance=float(data.get("totalWalletBalance", 0.0)),
        total_unrealized_profit=float(data.get("totalUnrealizedProfit", 0.0)),
        available_balance=float(data.get("availableBalance", 0.0)),
    )

  async def get_balances(self) -> List[AssetBalance]:
    """Retrieves asset balance breakdown."""
    data = await self._request("GET", "/fapi/v2/balance", is_signed=True, weight=5)
    balances: List[AssetBalance] = []
    for item in data:
      balances.append(
          AssetBalance(
              asset=item.get("asset", ""),
              wallet_balance=float(item.get("balance", 0.0)),
              unrealized_profit=float(item.get("crossUnPnl", 0.0)),
              margin_balance=float(
                  item.get("crossWalletBalance", item.get("balance", 0.0))
              ),
              available_balance=float(item.get("availableBalance", 0.0)),
          )
      )
    return balances

  async def get_positions(
      self, symbol: Optional[str] = None
  ) -> List[PositionRisk]:
    """Retrieves real-time position details, leverage, and liquidation prices."""
    params = {"symbol": symbol} if symbol else {}
    data = await self._request(
        "GET", "/fapi/v2/positionRisk", params=params, is_signed=True, weight=5
    )

    positions: List[PositionRisk] = []
    for item in data:
      positions.append(
          PositionRisk(
              symbol=item.get("symbol", ""),
              position_amt=float(item.get("positionAmt", 0.0)),
              entry_price=float(item.get("entryPrice", 0.0)),
              mark_price=float(item.get("markPrice", 0.0)),
              unrealized_profit=float(item.get("unRealizedProfit", 0.0)),
              liquidation_price=float(item.get("liquidationPrice", 0.0)),
              leverage=int(item.get("leverage", 1)),
              margin_type=item.get("marginType", "cross"),
              position_side=item.get("positionSide", "BOTH"),
          )
      )
    return positions

  async def get_open_orders(
      self, symbol: Optional[str] = None
  ) -> List[OpenOrder]:
    """Retrieves all currently active open orders."""
    params = {"symbol": symbol} if symbol else {}
    weight = 1 if symbol else 40
    data = await self._request(
        "GET", "/fapi/v1/openOrders", params=params, is_signed=True, weight=weight
    )

    orders: List[OpenOrder] = []
    for o in data:
      orders.append(
          OpenOrder(
              symbol=o.get("symbol", ""),
              order_id=int(o.get("orderId", 0)),
              client_order_id=o.get("clientOrderId", ""),
              price=float(o.get("price", 0.0)),
              orig_qty=float(o.get("origQty", 0.0)),
              executed_qty=float(o.get("executedQty", 0.0)),
              status=o.get("status", ""),
              order_type=o.get("type", ""),
              side=o.get("side", ""),
              time_in_force=o.get("timeInForce", ""),
              stop_price=float(o.get("stopPrice", 0.0)),
              reduce_only=bool(o.get("reduceOnly", False)),
              update_time=int(o.get("updateTime", 0)),
          )
      )
    return orders

  async def get_leverage_brackets(
      self, symbol: Optional[str] = None
  ) -> Dict[str, SymbolLeverageBrackets]:
    """Retrieves leverage brackets and notional limits."""
    params = {"symbol": symbol} if symbol else {}
    data = await self._request(
        "GET", "/fapi/v1/leverageBracket", params=params, is_signed=True, weight=1
    )

    # If single symbol queried, Binance returns list or dict
    items = data if isinstance(data, list) else [data]
    bracket_map: Dict[str, SymbolLeverageBrackets] = {}

    for item in items:
      sym = item.get("symbol", "")
      tiers: List[LeverageBracketTier] = []
      for b in item.get("brackets", []):
        tiers.append(
            LeverageBracketTier(
                bracket=int(b.get("bracket", 1)),
                initial_leverage=int(b.get("initialLeverage", 20)),
                notional_cap=float(b.get("notionalCap", 0.0)),
                notional_floor=float(b.get("notionalFloor", 0.0)),
                maint_margin_ratio=float(b.get("maintMarginRatio", 0.0)),
            )
        )
      bracket_map[sym] = SymbolLeverageBrackets(symbol=sym, brackets=tiers)

    return bracket_map

  async def get_position_mode(self) -> Dict[str, Any]:
    """Inspects account position mode (dualSidePosition: false = One-Way mode)."""
    return await self._request(
        "GET", "/fapi/v1/positionSide/dual", is_signed=True, weight=30
    )

  async def set_position_mode(self, dual_side: bool = False) -> Dict[str, Any]:
    """Configures account position mode. Bot strictly uses One-Way mode (dualSidePosition=false)."""
    params = {"dualSidePosition": "true" if dual_side else "false"}
    return await self._request(
        "POST", "/fapi/v1/positionSide/dual", params=params, is_signed=True, weight=1
    )

  async def change_margin_type(
      self, symbol: str, margin_type: str = "ISOLATED"
  ) -> Dict[str, Any]:
    """Sets symbol margin type to ISOLATED or CROSSED."""
    params = {"symbol": symbol, "marginType": margin_type.upper()}
    try:
      return await self._request(
          "POST", "/fapi/v1/marginType", params=params, is_signed=True, weight=1
      )
    except BinanceAPIError as err:
      # Code -4046 means margin type is already set to the requested value
      if err.code == -4046:
        return {"code": 200, "msg": f"Margin type already {margin_type.upper()}"}
      raise

  async def change_leverage(self, symbol: str, leverage: int) -> Dict[str, Any]:
    """Adjusts leverage for a symbol."""
    params = {"symbol": symbol, "leverage": leverage}
    return await self._request(
        "POST", "/fapi/v1/leverage", params=params, is_signed=True, weight=1
    )
