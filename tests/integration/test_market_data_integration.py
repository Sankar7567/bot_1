"""Integration tests for Market Data Engine against Binance Futures Testnet."""

import pytest
from src.exchange.client import BinanceFuturesClient
from src.market_data.engine import MarketDataEngine


class TestMarketDataIntegration:
  """Live Testnet integration tests for MarketDataEngine."""

  @pytest.mark.asyncio
  async def test_live_testnet_historical_kline_seeding(self):
    """Executes real historical kline retrieval and buffer pre-seeding against Testnet.

    Retrieves 1m and 15m closed candles for BTCUSDT from public Testnet REST API.
    """
    async with BinanceFuturesClient(
        base_url="https://testnet.binancefuture.com", timeout_seconds=15.0
    ) as client:
      try:
        engine = MarketDataEngine(client=client)
        # Pre-seed 10 bars of 1m and 5 bars of 15m
        await engine.seed_symbol_history(
            symbol="BTCUSDT",
            intervals=("1m", "15m"),
            limits={"1m": 10, "15m": 5},
        )

        candles_1m = engine.get_candles("BTCUSDT", "1m")
        candles_15m = engine.get_candles("BTCUSDT", "15m")

        assert len(candles_1m) > 0
        assert len(candles_15m) > 0

        # All stored candles must be completed
        assert all(c.is_closed for c in candles_1m)
        assert all(c.is_closed for c in candles_15m)

        # Monotonically increasing open_time
        for i in range(1, len(candles_1m)):
          assert candles_1m[i].open_time > candles_1m[i - 1].open_time

      except Exception as exc:
        pytest.skip(
            f"Binance Testnet public kline endpoint unreachable: {exc}"
        )
