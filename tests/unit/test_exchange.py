"""Unit tests for Binance USDⓈ-M Futures connector.

Tests request signing, time synchronization, model parsing, rate limiting,
error taxonomy, retries, and credential masking using mocked HTTP responses.
"""

import asyncio
import hashlib
import hmac
import json
from unittest.mock import AsyncMock, patch
import httpx
from pydantic import SecretStr
import pytest
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
    LeverageBracketTier,
    SymbolFilter,
    SymbolLeverageBrackets,
)
from src.exchange.rate_limiter import RateLimiter
from src.exchange.signer import calculate_signature, sign_parameters


# ==============================================================================
# 1. Signer & Cryptographic Tests
# ==============================================================================


class TestSigner:
  """Test suite verifying HMAC-SHA256 request signing and query parameter handling."""

  def test_calculate_signature(self):
    """Verify HMAC-SHA256 signature against manual hashlib computation."""
    secret = "test_binance_secret_key"
    query = "symbol=BTCUSDT&timestamp=1700000000000"
    expected = hmac.new(
        secret.encode(), query.encode(), hashlib.sha256
    ).hexdigest()

    result = calculate_signature(secret, query)
    assert result == expected
    assert len(result) == 64

  def test_sign_parameters(self):
    """Verify timestamp, recvWindow, and signature are appended in order."""
    params = {"symbol": "BTCUSDT", "side": "BUY"}
    secret = "dummy_secret_key_123"
    timestamp = 1700000005000

    signed = sign_parameters(
        params=params,
        secret=secret,
        timestamp_ms=timestamp,
        recv_window_ms=5000,
    )

    assert signed["symbol"] == "BTCUSDT"
    assert signed["side"] == "BUY"
    assert signed["timestamp"] == timestamp
    assert signed["recvWindow"] == 5000
    assert "signature" in signed
    assert len(signed["signature"]) == 64


# ==============================================================================
# 2. Rate Limiter Tests
# ==============================================================================


class TestRateLimiter:
  """Test suite verifying header-driven rate limit tracking and backoff."""

  def test_update_from_headers(self):
    limiter = RateLimiter(max_weight_per_minute=2400)
    assert limiter.current_used_weight_1m == 0

    headers = {"X-MBX-USED-WEIGHT-1M": "450", "Server": "nginx"}
    limiter.update_from_headers(headers)
    assert limiter.current_used_weight_1m == 450

  @pytest.mark.asyncio
  async def test_acquire_with_backoff(self):
    limiter = RateLimiter()
    limiter.set_backoff(0.05)  # 50ms pause
    start = asyncio.get_event_loop().time()
    await limiter.acquire()
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed >= 0.04


# ==============================================================================
# 3. Model & Filter Parsing Tests
# ==============================================================================


class TestModelsAndFilters:
  """Test suite verifying symbol filter parsing and leverage bracket models."""

  def test_symbol_filter_parsing(self):
    raw_filters = [
        {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
        {
            "filterType": "LOT_SIZE",
            "stepSize": "0.001",
            "minQty": "0.001",
            "maxQty": "1000.0",
        },
        {"filterType": "MIN_NOTIONAL", "notional": "5.0"},
    ]

    filters = SymbolFilter.from_raw_filters("BTCUSDT", raw_filters)
    assert filters.symbol == "BTCUSDT"
    assert filters.tick_size == 0.10
    assert filters.step_size == 0.001
    assert filters.min_qty == 0.001
    assert filters.max_qty == 1000.0
    assert filters.min_notional == 5.0
    assert filters.price_precision == 1  # log10(0.1) -> 1
    assert filters.quantity_precision == 3  # log10(0.001) -> 3

  def test_leverage_bracket_tiers(self):
    tiers = [
        LeverageBracketTier(
            bracket=1,
            initial_leverage=125,
            notional_cap=50000.0,
            notional_floor=0.0,
            maint_margin_ratio=0.004,
        ),
        LeverageBracketTier(
            bracket=2,
            initial_leverage=100,
            notional_cap=250000.0,
            notional_floor=50000.0,
            maint_margin_ratio=0.005,
        ),
    ]
    brackets = SymbolLeverageBrackets(symbol="BTCUSDT", brackets=tiers)

    assert brackets.max_leverage == 125
    tier1 = brackets.get_bracket_for_notional(10000.0)
    assert tier1 is not None and tier1.initial_leverage == 125

    tier2 = brackets.get_bracket_for_notional(60000.0)
    assert tier2 is not None and tier2.initial_leverage == 100


# ==============================================================================
# 4. Client Request, Auth & Error Handling Tests
# ==============================================================================


@pytest.fixture
def mock_http_client():
  client = AsyncMock(spec=httpx.AsyncClient)
  client.is_closed = False
  return client


@pytest.fixture
def test_client(mock_http_client):
  return BinanceFuturesClient(
      api_key=SecretStr("test_api_key_12345678"),
      api_secret=SecretStr("test_api_secret_87654321"),
      base_url="https://testnet.binancefuture.com",
      http_client=mock_http_client,
  )


class TestBinanceFuturesClient:
  """Test suite verifying REST client operations, error mapping, and safety."""

  def test_repr_masks_credentials(self, test_client):
    """Verify that repr(client) never exposes the secret key and masks the API key."""
    rep = repr(test_client)
    assert "test_api_secret_87654321" not in rep
    assert "5678" in rep  # Last 4 characters masked
    assert "testnet.binancefuture.com" in rep

  @pytest.mark.asyncio
  async def test_ping(self, test_client, mock_http_client):
    mock_response = httpx.Response(
        200, json={}, headers={"X-MBX-USED-WEIGHT-1M": "1"}
    )
    mock_http_client.get.return_value = mock_response

    result = await test_client.ping()
    assert result is True
    mock_http_client.get.assert_called_once()

  @pytest.mark.asyncio
  async def test_get_server_time_and_drift_sync(
      self, test_client, mock_http_client
  ):
    server_time = 1700000050000
    mock_response = httpx.Response(200, json={"serverTime": server_time})
    mock_http_client.get.return_value = mock_response

    with patch("time.time", return_value=1700000000.0):  # local: 1700000000000
      res = await test_client.get_server_time()
      assert res.server_time == server_time
      assert res.time_offset_ms == 50000
      assert test_client.time_offset_ms == 50000
      assert test_client.get_synchronized_time() == server_time

  @pytest.mark.asyncio
  async def test_get_exchange_info(self, test_client, mock_http_client):
    mock_payload = {
        "symbols": [{
            "symbol": "BTCUSDT",
            "status": "TRADING",
            "baseAsset": "BTC",
            "quoteAsset": "USDT",
            "contractType": "PERPETUAL",
            "pricePrecision": 2,
            "quantityPrecision": 3,
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                {"filterType": "LOT_SIZE", "stepSize": "0.001"},
            ],
        }]
    }
    mock_http_client.get.return_value = httpx.Response(200, json=mock_payload)

    info_map = await test_client.get_exchange_info()
    assert "BTCUSDT" in info_map
    btc = info_map["BTCUSDT"]
    assert btc.is_trading is True
    assert btc.is_usdt_perpetual is True
    assert btc.filters.tick_size == 0.10
    assert btc.filters.step_size == 0.001

    # Filter retrieval helper
    filters = await test_client.get_symbol_filters("BTCUSDT")
    assert filters.tick_size == 0.10

    # Nonexistent symbol raises BinanceFilterError
    with pytest.raises(BinanceFilterError):
      await test_client.get_symbol_filters("NONEXISTENT")

  @pytest.mark.asyncio
  async def test_missing_credentials_raises_auth_error(self, mock_http_client):
    client = BinanceFuturesClient(
        api_key=SecretStr(""),
        api_secret=SecretStr(""),
        http_client=mock_http_client,
    )
    with pytest.raises(BinanceAuthError):
      await client.get_account_info()

  @pytest.mark.asyncio
  async def test_get_account_info(self, test_client, mock_http_client):
    mock_payload = {
        "feeTier": 0,
        "canTrade": True,
        "canDeposit": True,
        "canWithdraw": False,
        "totalMarginBalance": "15000.50",
        "totalWalletBalance": "14500.00",
        "totalUnrealizedProfit": "500.50",
        "availableBalance": "10000.00",
    }
    mock_http_client.get.return_value = httpx.Response(200, json=mock_payload)

    account = await test_client.get_account_info()
    assert account.can_trade is True
    assert account.total_margin_balance == 15000.50
    assert account.available_balance == 10000.00

  @pytest.mark.asyncio
  async def test_get_balances(self, test_client, mock_http_client):
    mock_payload = [{
        "asset": "USDT",
        "balance": "10000.00",
        "crossUnPnl": "0.00",
        "crossWalletBalance": "10000.00",
        "availableBalance": "9500.00",
    }]
    mock_http_client.get.return_value = httpx.Response(200, json=mock_payload)

    balances = await test_client.get_balances()
    assert len(balances) == 1
    assert balances[0].asset == "USDT"
    assert balances[0].available_balance == 9500.00

  @pytest.mark.asyncio
  async def test_get_positions(self, test_client, mock_http_client):
    mock_payload = [{
        "symbol": "BTCUSDT",
        "positionAmt": "0.500",
        "entryPrice": "50000.0",
        "markPrice": "51000.0",
        "unRealizedProfit": "500.0",
        "liquidationPrice": "45000.0",
        "leverage": "20",
        "marginType": "isolated",
        "positionSide": "BOTH",
    }]
    mock_http_client.get.return_value = httpx.Response(200, json=mock_payload)

    positions = await test_client.get_positions("BTCUSDT")
    assert len(positions) == 1
    p = positions[0]
    assert p.symbol == "BTCUSDT"
    assert p.is_open is True
    assert p.is_isolated is True
    assert p.leverage == 20

  @pytest.mark.asyncio
  async def test_get_open_orders(self, test_client, mock_http_client):
    mock_payload = [{
        "symbol": "BTCUSDT",
        "orderId": 1234567,
        "clientOrderId": "HA_SL_1001",
        "price": "0.0",
        "origQty": "0.5",
        "executedQty": "0.0",
        "status": "NEW",
        "type": "STOP_MARKET",
        "side": "SELL",
        "timeInForce": "GTC",
        "stopPrice": "48000.0",
        "reduceOnly": True,
        "updateTime": 1700000000,
    }]
    mock_http_client.get.return_value = httpx.Response(200, json=mock_payload)

    orders = await test_client.get_open_orders("BTCUSDT")
    assert len(orders) == 1
    assert orders[0].client_order_id == "HA_SL_1001"
    assert orders[0].reduce_only is True

  @pytest.mark.asyncio
  async def test_auth_error_classification(self, test_client, mock_http_client):
    mock_response = httpx.Response(
        401, json={"code": -2015, "msg": "Invalid API-key, IP, or permissions."}
    )
    mock_http_client.get.return_value = mock_response

    with pytest.raises(BinanceAuthError) as exc_info:
      await test_client.get_account_info()
    assert exc_info.value.code == -2015

  @pytest.mark.asyncio
  async def test_rate_limit_error_classification(
      self, test_client, mock_http_client
  ):
    mock_response = httpx.Response(
        429,
        json={"code": -1003, "msg": "Too many requests."},
        headers={"Retry-After": "0.01"},
    )
    # Return 429 across all retry attempts
    mock_http_client.get.return_value = mock_response

    with pytest.raises(BinanceRateLimitError):
      await test_client.ping()

  @pytest.mark.asyncio
  async def test_retry_on_server_error_5xx(self, test_client, mock_http_client):
    # Fail twice with HTTP 500, then succeed on 3rd attempt
    resp_500 = httpx.Response(500, json={"code": -1000, "msg": "Internal error"})
    resp_200 = httpx.Response(200, json={"serverTime": 1700000000000})

    mock_http_client.get.side_effect = [resp_500, resp_500, resp_200]

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
      res = await test_client.get_server_time()
      assert res.server_time == 1700000000000
      assert mock_http_client.get.call_count == 3
      assert mock_sleep.call_count == 2

  @pytest.mark.asyncio
  async def test_network_transport_error_retries_and_raises(
      self, test_client, mock_http_client
  ):
    mock_http_client.get.side_effect = httpx.ConnectError("Connection refused")

    with patch("asyncio.sleep", new_callable=AsyncMock):
      with pytest.raises(BinanceNetworkError):
        await test_client.ping()
    # Retried max_retries + 1 times (4 attempts total)
    assert mock_http_client.get.call_count == 4

  @pytest.mark.asyncio
  async def test_malformed_json_response(self, test_client, mock_http_client):
    mock_response = httpx.Response(200, content=b"Non-JSON Bad Gateway HTML")
    mock_http_client.get.return_value = mock_response

    with pytest.raises(BinanceConnectorError):
      await test_client.ping()

  @pytest.mark.asyncio
  async def test_change_margin_type_already_set(
      self, test_client, mock_http_client
  ):
    # Code -4046 indicates "No need to change margin type"
    mock_response = httpx.Response(
        400, json={"code": -4046, "msg": "No need to change margin type."}
    )
    mock_http_client.post.return_value = mock_response

    res = await test_client.change_margin_type("BTCUSDT", "ISOLATED")
    assert res["code"] == 200
    assert "already ISOLATED" in res["msg"]

  @pytest.mark.asyncio
  async def test_change_margin_type_real_error(
      self, test_client, mock_http_client
  ):
    mock_response = httpx.Response(
        400, json={"code": -4047, "msg": "Margin type cannot be changed."}
    )
    mock_http_client.post.return_value = mock_response

    with pytest.raises(BinanceAPIError) as exc_info:
      await test_client.change_margin_type("BTCUSDT", "ISOLATED")
    assert exc_info.value.code == -4047

  @pytest.mark.asyncio
  async def test_get_leverage_brackets(self, test_client, mock_http_client):
    mock_payload = [{
        "symbol": "BTCUSDT",
        "brackets": [{
            "bracket": 1,
            "initialLeverage": 125,
            "notionalCap": 50000.0,
            "notionalFloor": 0.0,
            "maintMarginRatio": 0.004,
        }],
    }]
    mock_http_client.get.return_value = httpx.Response(200, json=mock_payload)

    brackets_map = await test_client.get_leverage_brackets("BTCUSDT")
    assert "BTCUSDT" in brackets_map
    assert brackets_map["BTCUSDT"].max_leverage == 125

  @pytest.mark.asyncio
  async def test_test_connectivity_and_auth(self, test_client, mock_http_client):
    mock_http_client.get.side_effect = [
        httpx.Response(200, json={}),  # ping
        httpx.Response(200, json={"canTrade": True, "feeTier": 0}),  # account
    ]
    res = await test_client.test_connectivity_and_auth()
    assert res is True

  @pytest.mark.asyncio
  async def test_context_manager_and_close(self, mock_http_client):
    async with BinanceFuturesClient(http_client=mock_http_client) as client:
      assert client is not None

  def test_exceptions_str_formatting(self):
    err = BinanceAPIError("Invalid symbol", code=-1121, status_code=400)
    assert str(err) == "[-1121] Invalid symbol"

    err_no_code = BinanceConnectorError("Unknown error")
    assert str(err_no_code) == "Unknown error"

  @pytest.mark.asyncio
  async def test_rate_limiter_retry_after_header_and_throttle(self):
    limiter = RateLimiter(max_weight_per_minute=100)
    limiter.update_from_headers({"Retry-After": "0.01"})
    assert limiter._backoff_until > 0

    limiter.current_used_weight_1m = 95
    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
      await limiter.acquire(1)
      assert mock_sleep.called
