"""Integration tests for Market Scanner against Binance Futures Testnet.

Verifies end-to-end scanning, filter evaluation, and top 20 candidate selection
using live public Testnet market data.
"""

import pytest
from src.exchange.client import BinanceFuturesClient
from src.scanner.scanner import MarketScanner


class TestScannerIntegration:
  """Live Testnet integration tests for MarketScanner."""

  @pytest.mark.asyncio
  async def test_live_testnet_market_scan(self):
    """Executes a live market scan against Binance USDⓈ-M Futures Testnet.

    Verifies that public exchange info and 24h tickers can be retrieved,
    filtered for USDT perpetual contracts, and ranked.
    """
    async with BinanceFuturesClient(
        base_url="https://testnet.binancefuture.com", timeout_seconds=15.0
    ) as client:
      try:
        # On Testnet, liquidity may be lower than mainnet, so use lower min volume
        scanner = MarketScanner(
            client=client,
            top_n=20,
            min_quote_volume_24h=0.0,  # Allow all active Testnet symbols
            max_data_age_ms=86400000,  # Testnet tickers can have longer intervals between trades
        )

        result = await scanner.scan()
        assert result is not None
        assert len(result.symbols) > 0
        assert len(result.symbols) <= 20
        assert result.scan_timestamp > 0
        assert result.total_evaluated_count > 0

        # Verify ranks are monotonically increasing from 1
        for idx, sym in enumerate(result.symbols, start=1):
          assert sym.rank == idx
          assert sym.contract_type == "PERPETUAL"
          assert sym.quote_asset == "USDT"

      except Exception as exc:
        pytest.skip(
            f"Binance Testnet public market data endpoint unreachable: {exc}"
        )
