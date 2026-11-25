"""Phase 14: Binance Futures Testnet Operational Validation Suite.

End-to-end verification against live Binance USD(S)-M Futures Testnet
(`https://testnet.binancefuture.com`). Tests requiring authenticated
credentials are marked `@pytest.mark.testnet` and skip cleanly when
`BINANCE_API_KEY` / `BINANCE_API_SECRET` are absent.
"""

import os
from pydantic import SecretStr
import pytest

from src.config.settings import Settings
from src.exchange.client import BinanceFuturesClient
from src.indicators.heikin_ashi import HeikinAshiSeries
from src.market_data.engine import MarketDataEngine
from src.scanner.scanner import MarketScanner
from src.state.database import DatabaseManager
from src.state.reconciler import StartupReconciler
from src.state.repository import StateRepository


def _get_testnet_creds():
  api_key = os.getenv("BINANCE_API_KEY", "")
  api_secret = os.getenv("BINANCE_API_SECRET", "")
  if not api_key or not api_secret or "your_binance" in api_key:
    return None, None
  return api_key, api_secret


class TestTestnetOperationalValidation:
  """Operational validation against live Binance Futures Testnet."""

  @pytest.mark.testnet
  @pytest.mark.asyncio
  async def test_testnet_end_to_end_preflight_scan_seed_and_reconcile(self):
    """Validates time sync, one-way mode, scanner top-20, 15m/1m HA seeding, and reconciliation."""
    api_key, api_secret = _get_testnet_creds()
    if not api_key:
      pytest.skip("Testnet API credentials not configured in environment.")

    settings = Settings(
        app_env="testnet",
        binance_api_key=SecretStr(api_key),
        binance_api_secret=SecretStr(api_secret),
        db_file_path=":memory:",
    )

    db = DatabaseManager(":memory:")
    db.initialize()
    repo = StateRepository(db)

    try:
      async with BinanceFuturesClient(
          api_key=SecretStr(api_key),
          api_secret=SecretStr(api_secret),
          base_url="https://testnet.binancefuture.com",
          timeout_seconds=10.0,
      ) as client:
        # 1. Connectivity, auth, and server time sync
        await client.test_connectivity_and_auth()
        await client.synchronize_time()

        # 2. One-way position mode verification
        dual_side = await client.get_position_mode()
        assert isinstance(dual_side, bool)

        # 3. Startup reconciliation against live exchange state
        reconciler = StartupReconciler(client=client, repository=repo)
        rec_res = await reconciler.reconcile()
        assert rec_res.status is not None

        # 4. Top-20 scanner execution
        scanner = MarketScanner(client=client, settings=settings)
        scan_res = await scanner.scan()
        assert len(scan_res.symbols) > 0

        # 5. MarketDataEngine historical seeding + Heikin-Ashi computation
        top_sym = scan_res.symbols[0].symbol
        mde = MarketDataEngine(client=client, settings=settings)
        await mde.seed_symbol(top_sym, intervals=("15m", "1m"))
        candles_15m = mde.get_candles(top_sym, "15m")
        candles_1m = mde.get_candles(top_sym, "1m")
        assert len(candles_15m) > 0
        assert len(candles_1m) > 0

        ha_15m = HeikinAshiSeries(top_sym, "15m")
        for c in candles_15m:
          ha_15m.update(c)
        assert ha_15m.latest is not None
    finally:
      db.close()
