"""Unit tests for Phase 2: Historical data acquisition, caching, and BacktestService."""

import asyncio
from unittest.mock import AsyncMock, MagicMock
import pytest
from src.backtesting.historical_data import (
    FIFTEEN_MINUTES_MS,
    ONE_MINUTE_MS,
    HistoricalCacheDB,
    HistoricalDataManager,
    aggregate_1m_to_15m,
)
from src.backtesting.service import BacktestService, export_trades_csv
from src.config.settings import Settings
from src.exchange.models import SymbolFilter, SymbolInfo
from src.market_data.models import Candle


def _make_symbol_info(symbol: str = "BTCUSDT") -> SymbolInfo:
  return SymbolInfo(
      symbol=symbol,
      status="TRADING",
      base_asset=symbol.replace("USDT", ""),
      quote_asset="USDT",
      contract_type="PERPETUAL",
      filters=SymbolFilter(
          symbol=symbol,
          tick_size=0.1,
          step_size=0.001,
          min_qty=0.001,
          max_qty=1000.0,
          min_notional=5.0,
          price_precision=1,
          quantity_precision=3,
      ),
  )


def _make_raw_kline(open_time_ms: int, price: float = 100.0) -> list:
  return [
      open_time_ms,
      str(price),
      str(price + 1.0),
      str(price - 1.0),
      str(price + 0.5),
      "100.0",
      open_time_ms + ONE_MINUTE_MS - 1,
      "10000.0",
      50,
      "50.0",
      "5000.0",
      "0",
  ]


@pytest.mark.unit
class TestHistoricalAggregationAndCache:

  def test_aggregate_1m_to_15m_only_complete_buckets(self):
    base = 1_700_000_700_000  # aligned to 15m? Let's align explicitly
    base = (base // FIFTEEN_MINUTES_MS) * FIFTEEN_MINUTES_MS
    candles = [
        Candle(
            symbol="BTCUSDT",
            interval="1m",
            open_time=base + i * ONE_MINUTE_MS,
            close_time=base + (i + 1) * ONE_MINUTE_MS - 1,
            open=100.0 + i,
            high=105.0 + i,
            low=99.0,
            close=101.0 + i,
            volume=10.0,
            quote_volume=1000.0,
            trades_count=5,
            is_closed=True,
        )
        for i in range(15)
    ]
    # Add 3 bars of a second incomplete bucket
    for i in range(3):
      t = base + FIFTEEN_MINUTES_MS + i * ONE_MINUTE_MS
      candles.append(
          Candle(
              symbol="BTCUSDT",
              interval="1m",
              open_time=t,
              close_time=t + ONE_MINUTE_MS - 1,
              open=120.0,
              high=121.0,
              low=119.0,
              close=120.5,
              volume=5.0,
              quote_volume=500.0,
              trades_count=2,
              is_closed=True,
          )
      )

    bars_15m = aggregate_1m_to_15m(candles)
    assert len(bars_15m) == 1
    b = bars_15m[0]
    assert b.open_time == base
    assert b.close_time == base + FIFTEEN_MINUTES_MS - 1
    assert b.open == 100.0
    assert b.close == 115.0
    assert b.high == 119.0
    assert b.low == 99.0
    assert b.volume == pytest.approx(150.0)

  @pytest.mark.asyncio
  async def test_pagination_cache_reuse_and_checkpoint_resume(self, tmp_path):
    cache = HistoricalCacheDB(tmp_path / "hist.db")
    cache.initialize()

    start_ms = 1_700_000_040_000
    start_ms = (start_ms // ONE_MINUTE_MS) * ONE_MINUTE_MS
    # 1502 minutes -> forces 2 pages (1500 + 2)
    end_ms = start_ms + 1502 * ONE_MINUTE_MS

    mock_client = MagicMock()
    mock_client.base_url = "https://testnet.binancefuture.com"
    mock_client.get_exchange_info = AsyncMock(
        return_value={"BTCUSDT": _make_symbol_info("BTCUSDT")}
    )

    calls = []

    async def fake_get_klines(symbol, interval, limit=1500, start_time=None, end_time=None):
      calls.append((symbol, start_time, end_time, limit))
      cur = start_time
      out = []
      while cur < end_time and len(out) < limit:
        out.append(_make_raw_kline(cur, 100.0))
        cur += ONE_MINUTE_MS
      return out

    mock_client.get_klines = AsyncMock(side_effect=fake_get_klines)

    mgr = HistoricalDataManager(client=mock_client, cache_db=cache)
    saved = await mgr.download_symbol_1m_range("BTCUSDT", start_ms, end_ms)
    assert saved == 1502
    assert len(calls) == 2

    # Second run should reuse cache with 0 API calls
    calls.clear()
    saved2 = await mgr.download_symbol_1m_range("BTCUSDT", start_ms, end_ms)
    assert saved2 == 0
    assert len(calls) == 0

    # Simulate interrupted download: insert 2 bars without completed checkpoint,
    # then request an extended range; only the uncached bars should be fetched
    new_end = start_ms + 1505 * ONE_MINUTE_MS
    saved3 = await mgr.download_symbol_1m_range("BTCUSDT", start_ms, new_end)
    assert saved3 == 3
    assert len(calls) == 1
    assert calls[0][1] == start_ms + 1502 * ONE_MINUTE_MS
    assert calls[0][2] == new_end

    # Expanding backward (e.g. 2 months after 1 month) must only fetch the earlier missing range
    calls.clear()
    earlier_start = start_ms - 10 * ONE_MINUTE_MS
    saved4 = await mgr.download_symbol_1m_range("BTCUSDT", earlier_start, new_end)
    assert saved4 == 10
    assert len(calls) == 1
    assert calls[0][1] == earlier_start
    assert calls[0][2] == start_ms

    cache.close()


@pytest.mark.unit
class TestBacktestService:

  @pytest.mark.asyncio
  async def test_verified_metadata_rejected_without_credentials(self, tmp_path):
    cache = HistoricalCacheDB(tmp_path / "hist.db")
    settings = Settings(app_env="testnet", db_file_path=tmp_path / "bot.db")
    svc = BacktestService(settings=settings, cache_db=cache)

    with pytest.raises(ValueError, match="Verified Metadata Mode requires"):
      await svc.start_backtest(
          start_time_ms=1_700_000_000_000,
          end_time_ms=1_700_086_400_000,
          initial_balance=100.0,
          metadata_mode="VERIFIED_METADATA",
      )
    cache.close()

  @pytest.mark.asyncio
  async def test_research_mode_backtest_and_duplicate_prevention(self, tmp_path):
    cache = HistoricalCacheDB(tmp_path / "hist.db")
    settings = Settings(app_env="testnet", db_file_path=tmp_path / "bot.db")
    svc = BacktestService(settings=settings, cache_db=cache)

    start_ms = 1_700_086_400_000
    start_ms = (start_ms // FIFTEEN_MINUTES_MS) * FIFTEEN_MINUTES_MS
    end_ms = start_ms + 4 * FIFTEEN_MINUTES_MS

    mock_client = MagicMock()
    mock_client.base_url = "https://testnet.binancefuture.com"
    mock_client.get_exchange_info = AsyncMock(
        return_value={"BTCUSDT": _make_symbol_info("BTCUSDT")}
    )

    async def fake_get_klines(symbol, interval, limit=1500, start_time=None, end_time=None):
      cur = start_time
      out = []
      while cur < end_time and len(out) < limit:
        out.append(_make_raw_kline(cur, 100.0))
        cur += ONE_MINUTE_MS
      return out

    mock_client.get_klines = AsyncMock(side_effect=fake_get_klines)

    job_id = await svc.start_backtest(
        start_time_ms=start_ms,
        end_time_ms=end_ms,
        initial_balance=100.0,
        metadata_mode="RESEARCH_MODE",
        symbols_filter=["BTCUSDT"],
        client=mock_client,
    )
    assert job_id.startswith("bt_")

    # Duplicate start while running should raise RuntimeError
    if svc.is_running():
      with pytest.raises(RuntimeError, match="already in progress"):
        await svc.start_backtest(
            start_time_ms=start_ms,
            end_time_ms=end_ms,
            initial_balance=100.0,
            client=mock_client,
        )

    # Wait for completion
    for _ in range(100):
      if not svc.is_running():
        break
      await asyncio.sleep(0.05)

    status = svc.get_status()
    assert status["status"] == "completed", status
    report = svc.get_last_report()
    assert report is not None
    assert report["metadata"]["mode"] == "RESEARCH_MODE"
    assert report["equity"]["starting_balance"] == 100.0
    assert export_trades_csv(report["trades"]).startswith("trade_id,symbol,")
    cache.close()
