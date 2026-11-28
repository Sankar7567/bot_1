"""Unit tests for Early-Momentum Breakout indicators, MomentumScanner, and UI DB persistence."""

import asyncio
from unittest.mock import AsyncMock, MagicMock
import pytest
from src.backtesting.historical_data import (
    FIFTEEN_MINUTES_MS,
    ONE_MINUTE_MS,
    HistoricalCacheDB,
)
from src.backtesting.service import BacktestService
from src.config.settings import Settings
from src.exchange.models import SymbolFilter, SymbolInfo, Ticker24hr
from src.indicators.momentum import evaluate_early_momentum
from src.market_data.models import Candle
from src.scanner.momentum_scanner import MomentumScanner


def _make_symbol_info(symbol: str) -> SymbolInfo:
  return SymbolInfo(
      symbol=symbol,
      status="TRADING",
      base_asset=symbol.replace("USDT", ""),
      quote_asset="USDT",
      contract_type="PERPETUAL",
      filters=SymbolFilter(
          symbol=symbol,
          tick_size=0.01,
          step_size=0.01,
          min_qty=0.01,
          max_qty=10000.0,
          min_notional=5.0,
          price_precision=2,
          quantity_precision=2,
      ),
  )


def _make_15m_series(
    symbol: str,
    count: int = 35,
    base_price: float = 100.0,
    breakout_last: bool = True,
    exhausted: bool = False,
) -> list[Candle]:
  candles: list[Candle] = []
  base_t = 1_700_000_000_000 - (1_700_000_000_000 % FIFTEEN_MINUTES_MS)
  price = base_price
  for i in range(count):
    t = base_t + i * FIFTEEN_MINUTES_MS
    if exhausted:
      # Parabolic rally for all 35 bars -> RSI > 80 exhausted
      step = 2.0
      vol = 100.0
    elif breakout_last and i >= count - 2:
      # Fresh 2-bar breakout after realistic range oscillation
      step = 0.55 if i == count - 2 else 0.80
      vol = 350.0 if i == count - 1 else 200.0
    else:
      # Realistic sideways oscillation
      step = 0.35 if i % 2 == 0 else -0.32
      vol = 100.0
    o = price
    c = price + step
    h = max(o, c) + 0.15
    l = min(o, c) - 0.15
    price = c
    candles.append(
        Candle(
            symbol=symbol,
            interval="15m",
            open_time=t,
            close_time=t + FIFTEEN_MINUTES_MS - 1,
            open=o,
            high=h,
            low=l,
            close=c,
            volume=vol,
            quote_volume=vol * c,
            trades_count=20,
            is_closed=True,
        )
    )
  return candles


@pytest.mark.unit
class TestMomentumIndicatorsAndScanner:

  def test_early_momentum_scores_fresh_breakout_and_filters_exhausted(self):
    fresh = evaluate_early_momentum(
        "SOLUSDT", _make_15m_series("SOLUSDT", breakout_last=True)
    )
    exhausted = evaluate_early_momentum(
        "DOGEUSDT", _make_15m_series("DOGEUSDT", exhausted=True)
    )
    assert fresh is not None
    assert fresh.direction_bias == "LONG"
    assert fresh.score > 50.0
    assert fresh.volume_surge > 2.0

    assert exhausted is not None
    assert exhausted.rsi_14 > 78.0
    assert exhausted.score == 0.0

  @pytest.mark.asyncio
  async def test_momentum_scanner_ranks_early_breakout_above_flat(self):
    now_ms = 1_700_032_400_000
    client = MagicMock()
    client.get_synchronized_time.return_value = now_ms
    client.get_exchange_info = AsyncMock(
        return_value={
            "SOLUSDT": _make_symbol_info("SOLUSDT"),
            "ADAUSDT": _make_symbol_info("ADAUSDT"),
        }
    )
    client.get_24hr_tickers = AsyncMock(
        return_value=[
            Ticker24hr(
                symbol="ADAUSDT",
                price_change=1.0,
                price_change_percent=12.0,  # higher 24h gain but flat/exhausted now
                weighted_avg_price=1.0,
                last_price=1.0,
                last_qty=10.0,
                open_price=0.9,
                high_price=1.05,
                low_price=0.89,
                volume=10_000_000.0,
                quote_volume=10_000_000.0,
                open_time=0,
                close_time=now_ms - 1000,
                first_id=1,
                last_id=10,
                count=10,
            ),
            Ticker24hr(
                symbol="SOLUSDT",
                price_change=2.0,
                price_change_percent=2.0,  # lower 24h gain, fresh breakout!
                weighted_avg_price=100.0,
                last_price=103.0,
                last_qty=1.0,
                open_price=100.0,
                high_price=104.0,
                low_price=99.0,
                volume=100_000.0,
                quote_volume=10_000_000.0,
                open_time=0,
                close_time=now_ms - 1000,
                first_id=1,
                last_id=10,
                count=10,
            ),
        ]
    )

    sol_bars = _make_15m_series("SOLUSDT", breakout_last=True)
    ada_bars = _make_15m_series("ADAUSDT", breakout_last=False)

    async def fake_klines(symbol, interval, limit=45):
      bars = sol_bars if symbol == "SOLUSDT" else ada_bars
      return [
          [
              b.open_time,
              str(b.open),
              str(b.high),
              str(b.low),
              str(b.close),
              str(b.volume),
              b.close_time,
              str(b.quote_volume),
              b.trades_count,
              "0",
              "0",
              "0",
          ]
          for b in bars
      ]

    client.get_klines = AsyncMock(side_effect=fake_klines)
    scanner = MomentumScanner(client=client, settings=Settings(app_env="testnet"))
    res = await scanner.scan()
    assert len(res.symbols) == 2
    assert res.symbols[0].symbol == "SOLUSDT"
    assert res.symbols[0].rank == 1


@pytest.mark.unit
class TestBacktestPersistenceAcrossReload:

  @pytest.mark.asyncio
  async def test_backtest_run_persists_and_restores_in_new_service(self, tmp_path):
    cache_path = tmp_path / "hist.db"
    cache1 = HistoricalCacheDB(cache_path)
    settings = Settings(app_env="testnet", db_file_path=tmp_path / "bot.db")
    svc1 = BacktestService(settings=settings, cache_db=cache1)

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
        out.append([
            cur, "100", "101", "99", "100.5", "100",
            cur + ONE_MINUTE_MS - 1, "10000", 10, "50", "5000", "0"
        ])
        cur += ONE_MINUTE_MS
      return out

    mock_client.get_klines = AsyncMock(side_effect=fake_get_klines)

    job_id = await svc1.start_backtest(
        start_time_ms=start_ms,
        end_time_ms=end_ms,
        initial_balance=100.0,
        strategy_name="momentum_breakout",
        symbols_filter=["BTCUSDT"],
        client=mock_client,
    )
    for _ in range(100):
      if not svc1.is_running():
        break
      await asyncio.sleep(0.05)

    assert svc1.get_status()["status"] == "completed"
    cache1.close()

    # Simulate UI / server restart: new BacktestService reading the same SQLite DB
    cache2 = HistoricalCacheDB(cache_path)
    svc2 = BacktestService(settings=settings, cache_db=cache2)
    restored_status = svc2.get_status()
    restored_report = svc2.get_last_report()
    assert restored_status["job_id"] == job_id
    assert restored_status["status"] == "completed"
    assert restored_status["strategy_name"] == "momentum_breakout"
    assert restored_report is not None
    assert restored_report["config"]["strategy_name"] == "momentum_breakout"
    cache2.close()
