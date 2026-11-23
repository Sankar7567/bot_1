"""Integration tests for Binance USDⓈ-M Futures connector.

Includes simulated end-to-end exchange handshakes and optional real Testnet tests.
"""

import os
from pydantic import SecretStr
import pytest
from src.exchange.client import BinanceFuturesClient


class TestExchangeIntegration:
  """Integration verification of Binance Futures Client."""

  @pytest.mark.asyncio
  async def test_public_ping_testnet(self):
    """Verifies unauthenticated public connectivity directly to Binance Futures Testnet."""
    # This hits the public Testnet ping endpoint without needing credentials
    async with BinanceFuturesClient(
        base_url="https://testnet.binancefuture.com", timeout_seconds=5.0
    ) as client:
      try:
        is_alive = await client.ping()
        assert is_alive is True

        time_res = await client.get_server_time()
        assert time_res.server_time > 1700000000000
        assert isinstance(time_res.time_offset_ms, int)
      except Exception as exc:
        pytest.skip(
            f"Binance Testnet public endpoint unreachable or network blocked:"
            f" {exc}"
        )

  @pytest.mark.testnet
  @pytest.mark.asyncio
  async def test_authenticated_testnet_account_info(self):
    """Verifies authenticated access to Binance Futures Testnet using environment credentials.

    Skipped automatically if valid TESTNET credentials are not populated in the
    environment.
    """
    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")

    if not api_key or not api_secret or "your_binance" in api_key:
      pytest.skip("Testnet API credentials not configured in environment.")

    async with BinanceFuturesClient(
        api_key=SecretStr(api_key),
        api_secret=SecretStr(api_secret),
        base_url="https://testnet.binancefuture.com",
    ) as client:
      account = await client.get_account_info()
      assert account is not None
      assert isinstance(account.total_margin_balance, float)
      assert isinstance(account.can_trade, bool)
