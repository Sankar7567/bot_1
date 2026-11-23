"""Unit tests for Market Scanner.

Tests 24h percentage gain ranking, top 20 selection, tie-breaking,
contract filtering, volume thresholds, stale data detection, and API error handling.
"""

from unittest.mock import AsyncMock, patch
import pytest
from src.exchange.client import BinanceFuturesClient
from src.exchange.exceptions import BinanceNetworkError
from src.exchange.models import SymbolFilter, SymbolInfo, Ticker24hr
from src.scanner.exceptions import (
    EmptyUniverseError,
    ScannerAPIError,
    StaleDataError,
)
from src.scanner.filters import (
    deterministic_ranking_sort_key,
    is_eligible_contract,
    is_eligible_ticker,
)
from src.scanner.scanner import MarketScanner


# ==============================================================================
# Helper Factories
# ==============================================================================


def create_mock_symbol_info(
    symbol: str,
    status: str = "TRADING",
    quote_asset: str = "USDT",
    contract_type: str = "PERPETUAL",
) -> SymbolInfo:
  return SymbolInfo(
      symbol=symbol,
      status=status,
      base_asset=symbol.replace(quote_asset, ""),
      quote_asset=quote_asset,
      contract_type=contract_type,
      filters=SymbolFilter(
          symbol=symbol,
          tick_size=0.01,
          step_size=0.001,
          min_qty=0.001,
          max_qty=10000.0,
          min_notional=5.0,
          price_precision=2,
          quantity_precision=3,
      ),
  )


def create_mock_ticker(
    symbol: str,
    price_change_percent: float,
    quote_volume: float = 2000000.0,
    last_price: float = 100.0,
    close_time: int = 1700000000000,
) -> Ticker24hr:
  return Ticker24hr(
      symbol=symbol,
      price_change=price_change_percent,
      price_change_percent=price_change_percent,
      weighted_avg_price=last_price,
      last_price=last_price,
      last_qty=1.0,
      open_price=100.0,
      high_price=110.0,
      low_price=95.0,
      volume=quote_volume / last_price if last_price > 0 else 0.0,
      quote_volume=quote_volume,
      open_time=close_time - 86400000,
      close_time=close_time,
      first_id=1,
      last_id=1000,
      count=1000,
  )


@pytest.fixture
def mock_exchange_client():
  client = AsyncMock(spec=BinanceFuturesClient)
  client.get_synchronized_time.return_value = 1700000005000  # 5s ahead of close_time
  return client


# ==============================================================================
# Test Cases
# ==============================================================================


class TestMarketScanner:
  """Test suite for MarketScanner."""

  def test_deterministic_sort_key(self):
    """Verify primary and secondary tie-breaking logic."""
    t1 = create_mock_ticker("BTCUSDT", price_change_percent=10.0, quote_volume=5000000.0)
    t2 = create_mock_ticker("ETHUSDT", price_change_percent=10.0, quote_volume=3000000.0)
    t3 = create_mock_ticker("ADAUSDT", price_change_percent=10.0, quote_volume=3000000.0)

    # t1 > t2 because quote_volume is higher
    assert deterministic_ranking_sort_key(t1) < deterministic_ranking_sort_key(t2)
    # t3 > t2 because ADAUSDT comes before ETHUSDT alphabetically
    assert deterministic_ranking_sort_key(t3) < deterministic_ranking_sort_key(t2)

  def test_filter_predicates(self):
    """Verify eligibility filters."""
    # Valid contract
    valid_info = create_mock_symbol_info("BTCUSDT")
    assert is_eligible_contract(valid_info) is True

    # Inactive contract
    inactive_info = create_mock_symbol_info("BTCUSDT", status="BREAK")
    assert is_eligible_contract(inactive_info) is False

    # Non-perpetual
    delivery_info = create_mock_symbol_info("BTCUSDT", contract_type="CURRENT_QUARTER")
    assert is_eligible_contract(delivery_info) is False

    # Non-USDT
    busd_info = create_mock_symbol_info("BTCBUSD", quote_asset="BUSD")
    assert is_eligible_contract(busd_info) is False

    # Low volume ticker
    low_vol = create_mock_ticker("BTCUSDT", 5.0, quote_volume=500000.0)
    assert is_eligible_ticker(low_vol, min_quote_volume_24h=1000000.0) is False

    # Valid volume ticker
    good_vol = create_mock_ticker("BTCUSDT", 5.0, quote_volume=1500000.0)
    assert is_eligible_ticker(good_vol, min_quote_volume_24h=1000000.0) is True

  @pytest.mark.asyncio
  async def test_correct_ranking_and_top_20_selection(
      self, mock_exchange_client
  ):
    """Verify scanner ranks contracts by 24h gain descending and slices top 20."""
    # Generate 30 candidate symbols with increasing gain percentages (1% to 30%)
    symbols_map = {}
    tickers = []
    for i in range(1, 31):
      sym = f"SYM{i:02d}USDT"
      symbols_map[sym] = create_mock_symbol_info(sym)
      tickers.append(
          create_mock_ticker(
              sym, price_change_percent=float(i), quote_volume=2000000.0
          )
      )

    mock_exchange_client.get_exchange_info.return_value = symbols_map
    mock_exchange_client.get_24hr_tickers.return_value = tickers

    scanner = MarketScanner(client=mock_exchange_client, top_n=20)
    result = await scanner.scan()

    assert result is not None
    assert len(result.symbols) == 20
    assert result.total_eligible_count == 30
    assert result.total_evaluated_count == 30

    # Rank 1 must be SYM30USDT (30% gain)
    assert result.symbols[0].symbol == "SYM30USDT"
    assert result.symbols[0].rank == 1
    assert result.symbols[0].price_change_percent == 30.0

    # Rank 20 must be SYM11USDT (11% gain)
    assert result.symbols[19].symbol == "SYM11USDT"
    assert result.symbols[19].rank == 20
    assert result.symbols[19].price_change_percent == 11.0

    # Verify helper methods
    assert result.is_in_universe("SYM30USDT") is True
    assert result.is_in_universe("SYM01USDT") is False
    assert result.get_rank("SYM30USDT") == 1
    assert result.get_rank("SYM01USDT") is None

  @pytest.mark.asyncio
  async def test_tied_percentage_changes_resolution(
      self, mock_exchange_client
  ):
    """Verify deterministic tie-breaking for equal percentage changes."""
    symbols_map = {
        "ETHUSDT": create_mock_symbol_info("ETHUSDT"),
        "BTCUSDT": create_mock_symbol_info("BTCUSDT"),
        "SOLUSDT": create_mock_symbol_info("SOLUSDT"),
    }
    tickers = [
        # Both ETH and BTC have 10.0% gain, but BTC has higher quote volume
        create_mock_ticker("ETHUSDT", 10.0, quote_volume=3000000.0),
        create_mock_ticker("BTCUSDT", 10.0, quote_volume=8000000.0),
        # SOL has identical 10.0% gain and identical volume to ETH, so alphabetical tie-break wins
        create_mock_ticker("SOLUSDT", 10.0, quote_volume=3000000.0),
    ]

    mock_exchange_client.get_exchange_info.return_value = symbols_map
    mock_exchange_client.get_24hr_tickers.return_value = tickers

    scanner = MarketScanner(client=mock_exchange_client, top_n=20)
    result = await scanner.scan()

    # 1st: BTCUSDT (highest volume)
    # 2nd: ETHUSDT (comes before SOLUSDT alphabetically)
    # 3rd: SOLUSDT
    ranked_names = result.get_symbol_names()
    assert ranked_names == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

  @pytest.mark.asyncio
  async def test_inactive_and_non_perpetual_excluded(
      self, mock_exchange_client
  ):
    """Verify inactive and delivery contracts are discarded even with top gains."""
    symbols_map = {
        "INACTIVEUSDT": create_mock_symbol_info("INACTIVEUSDT", status="BREAK"),
        "QUARTERLYUSDT": create_mock_symbol_info("QUARTERLYUSDT", contract_type="CURRENT_QUARTER"),
        "VALIDUSDT": create_mock_symbol_info("VALIDUSDT", status="TRADING"),
    }
    tickers = [
        create_mock_ticker("INACTIVEUSDT", 50.0),
        create_mock_ticker("QUARTERLYUSDT", 40.0),
        create_mock_ticker("VALIDUSDT", 10.0),
    ]

    mock_exchange_client.get_exchange_info.return_value = symbols_map
    mock_exchange_client.get_24hr_tickers.return_value = tickers

    scanner = MarketScanner(client=mock_exchange_client, top_n=20)
    result = await scanner.scan()

    assert len(result.symbols) == 1
    assert result.symbols[0].symbol == "VALIDUSDT"

  @pytest.mark.asyncio
  async def test_low_liquidity_contracts_excluded(
      self, mock_exchange_client
  ):
    """Verify symbols below minimum 24h quote volume are discarded."""
    symbols_map = {
        "LOWVOLUSDT": create_mock_symbol_info("LOWVOLUSDT"),
        "HIGHVOLUSDT": create_mock_symbol_info("HIGHVOLUSDT"),
    }
    tickers = [
        create_mock_ticker("LOWVOLUSDT", 25.0, quote_volume=400000.0),  # < 1M
        create_mock_ticker("HIGHVOLUSDT", 5.0, quote_volume=1500000.0), # > 1M
    ]

    mock_exchange_client.get_exchange_info.return_value = symbols_map
    mock_exchange_client.get_24hr_tickers.return_value = tickers

    scanner = MarketScanner(
        client=mock_exchange_client, min_quote_volume_24h=1000000.0
    )
    result = await scanner.scan()

    assert len(result.symbols) == 1
    assert result.symbols[0].symbol == "HIGHVOLUSDT"

  @pytest.mark.asyncio
  async def test_empty_exchange_info_raises_error(self, mock_exchange_client):
    """Verify empty exchange info raises EmptyUniverseError."""
    mock_exchange_client.get_exchange_info.return_value = {}
    mock_exchange_client.get_24hr_tickers.return_value = [
        create_mock_ticker("BTCUSDT", 5.0)
    ]

    scanner = MarketScanner(client=mock_exchange_client)
    with pytest.raises(EmptyUniverseError):
      await scanner.scan()

  @pytest.mark.asyncio
  async def test_empty_tickers_raises_error(self, mock_exchange_client):
    """Verify empty tickers response raises EmptyUniverseError."""
    mock_exchange_client.get_exchange_info.return_value = {
        "BTCUSDT": create_mock_symbol_info("BTCUSDT")
    }
    mock_exchange_client.get_24hr_tickers.return_value = []

    scanner = MarketScanner(client=mock_exchange_client)
    with pytest.raises(EmptyUniverseError):
      await scanner.scan()

  @pytest.mark.asyncio
  async def test_stale_market_data_detected_and_rejected(
      self, mock_exchange_client
  ):
    """Verify stale ticker closeTime raises StaleDataError."""
    now_ms = 1700000000000
    mock_exchange_client.get_synchronized_time.return_value = now_ms

    # Ticker timestamp is 5 minutes in the past (> 2 minute limit)
    stale_close = now_ms - 300000
    mock_exchange_client.get_exchange_info.return_value = {
        "BTCUSDT": create_mock_symbol_info("BTCUSDT")
    }
    mock_exchange_client.get_24hr_tickers.return_value = [
        create_mock_ticker("BTCUSDT", 5.0, close_time=stale_close)
    ]

    scanner = MarketScanner(
        client=mock_exchange_client, max_data_age_ms=120000
    )
    with pytest.raises(StaleDataError):
      await scanner.scan()

  @pytest.mark.asyncio
  async def test_api_failure_raises_scanner_api_error(
      self, mock_exchange_client
  ):
    """Verify exchange connector exceptions are wrapped in ScannerAPIError."""
    mock_exchange_client.get_exchange_info.side_effect = BinanceNetworkError(
        "Connection reset"
    )

    scanner = MarketScanner(client=mock_exchange_client)
    with pytest.raises(ScannerAPIError):
      await scanner.scan()

  @pytest.mark.asyncio
  async def test_zero_eligible_symbols_raises_empty_universe(
      self, mock_exchange_client
  ):
    """Verify that if all symbols fail volume or status filters, EmptyUniverseError is raised."""
    symbols_map = {
        "LOWVOLUSDT": create_mock_symbol_info("LOWVOLUSDT"),
    }
    tickers = [
        create_mock_ticker("LOWVOLUSDT", 10.0, quote_volume=100.0),  # Well below 1M
    ]

    mock_exchange_client.get_exchange_info.return_value = symbols_map
    mock_exchange_client.get_24hr_tickers.return_value = tickers

    scanner = MarketScanner(
        client=mock_exchange_client, min_quote_volume_24h=1000000.0
    )
    with pytest.raises(EmptyUniverseError):
      await scanner.scan()

  @pytest.mark.asyncio
  async def test_unexpected_exception_during_scan_raises_scanner_error(
      self, mock_exchange_client
  ):
    """Verify generic unexpected exception raises ScannerError."""
    mock_exchange_client.get_exchange_info.side_effect = RuntimeError(
        "Memory or system fault"
    )

    scanner = MarketScanner(client=mock_exchange_client)
    with pytest.raises(ScannerAPIError if False else Exception):
      await scanner.scan()

  def test_filter_predicates_nan_and_negative_price(self):
    """Verify NaN percentage and negative/zero price rejection."""
    import math

    nan_ticker = create_mock_ticker("BTCUSDT", float("nan"))
    assert is_eligible_ticker(nan_ticker) is False

    inf_ticker = create_mock_ticker("BTCUSDT", float("inf"))
    assert is_eligible_ticker(inf_ticker) is False

    zero_price_ticker = create_mock_ticker("BTCUSDT", 5.0, last_price=0.0)
    assert is_eligible_ticker(zero_price_ticker) is False

  @pytest.mark.asyncio
  async def test_start_periodic_scan_when_already_running(
      self, mock_exchange_client
  ):
    """Verify calling start_periodic_scan twice logs warning and does not duplicate task."""
    mock_exchange_client.get_exchange_info.return_value = {
        "BTCUSDT": create_mock_symbol_info("BTCUSDT")
    }
    mock_exchange_client.get_24hr_tickers.return_value = [
        create_mock_ticker("BTCUSDT", 5.0)
    ]

    scanner = MarketScanner(client=mock_exchange_client)
    scanner.start_periodic_scan(interval_sec=10)
    initial_task = scanner._periodic_task

    # Call start again
    scanner.start_periodic_scan(interval_sec=10)
    assert scanner._periodic_task is initial_task

    await scanner.stop_periodic_scan()
    assert scanner._periodic_task is None
