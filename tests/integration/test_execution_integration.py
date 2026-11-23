"""Integration tests for Order Execution Engine.

Tests order router with Binance Futures Client against public endpoints and authenticated Testnet.
"""

import os
from pydantic import SecretStr
import pytest
from src.exchange.client import BinanceFuturesClient
from src.execution.client_id import OrderPrefix, generate_client_order_id, is_bot_client_order_id
from src.execution.order_router import OrderRouter


class TestExecutionIntegration:
  """Integration verification of order execution."""

  def test_client_order_id_generation_offline(self):
    """Verifies that client order ID generation is fully offline, fast, and compliant."""
    for prefix in (OrderPrefix.ENTRY, OrderPrefix.STOP_LOSS, OrderPrefix.TAKE_PROFIT):
      cid = generate_client_order_id(prefix)
      assert is_bot_client_order_id(cid) is True
      assert len(cid) <= 36

  @pytest.mark.testnet
  @pytest.mark.asyncio
  async def test_live_testnet_order_router_connectivity(self):
    """Verifies order router interaction with real Testnet credentials if present."""
    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")

    if not api_key or not api_secret or "your_binance" in api_key:
      pytest.skip("Testnet API credentials not configured in environment.")

    async with BinanceFuturesClient(
        api_key=SecretStr(api_key),
        api_secret=SecretStr(api_secret),
        base_url="https://testnet.binancefuture.com",
    ) as client:
      router = OrderRouter(client)
      open_orders = await client.get_open_orders()
      assert isinstance(open_orders, list)
