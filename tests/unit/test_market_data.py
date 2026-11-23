"""Unit tests for Market Data Engine.

Tests 1m and 15m candle models, chronological buffer ordering, duplicate rejection,
incomplete candle filtering, gap detection, REST seeding, WebSocket ingestion,
and reconciliation.
"""

import asyncio
from unittest.mock import AsyncMock, patch
import pytest
from src.exchange.client import BinanceFuturesClient
from src.market_data.candle_buffer import CandleBuffer
from src.market_data.engine import MarketDataEngine
from src.market_data.exceptions import CandleDataError
from src.market_data.feed import BinanceKlineWebSocket
from src.market_data.models import (
    Candle,
    CandleCloseEvent,
    interval_to_ms,
)


# ==============================================================================
# Helper Fixtures & Factories
# ==============================================================================


def make_candle(
    symbol: str = "BTCUSDT",
    interval: str = "1m",
    open_time: int = 1700000000000,
    open_price: float = 100.0,
    high_price: float = 105.0,
    low_price: float = 95.0,
    close_price: float = 102.0,
    volume: float = 10.0,
    is_closed: bool = True,
) -> Candle:
  duration = interval_to_ms(interval)
  return Candle(
      symbol=symbol.upper(),
      interval=interval,
      open_time=open_time,
      close_time=open_time + duration - 1,
      open=open_price,
      high=high_price,
      low=low_price,
      close=close_price,
      volume=volume,
      quote_volume=volume * close_price,
      trades_count=100,
      is_closed=is_closed,
  )


# ==============================================================================
# 1. Candle Model Tests
# ==============================================================================


class TestCandleModels:
  """Test suite for Candle data model and interval conversions."""

  def test_interval_to_ms(self):
    assert interval_to_ms("1m") == 60000
    assert interval_to_ms("15m") == 900000
    assert interval_to_ms("1h") == 3600000
    assert interval_to_ms("1d") == 86400000

    with pytest.raises(ValueError):
      interval_to_ms("1w")

  def test_from_rest_kline(self):
    raw = [
        1700000000000,  # open time
        "100.5",  # open
        "110.0",  # high
        "99.0",  # low
        "105.0",  # close
        "500.0",  # volume
        1700000059999,  # close time
        "52500.0",  # quote volume
        120,  # trades count
        "250.0",  # taker buy base vol
        "26250.0",  # taker buy quote vol
        "0",  # ignore
    ]

    c = Candle.from_rest_kline("BTCUSDT", "1m", raw, is_closed=True)
    assert c.symbol == "BTCUSDT"
    assert c.interval == "1m"
    assert c.open_time == 1700000000000
    assert c.close_time == 1700000059999
    assert c.open == 100.5
    assert c.high == 110.0
    assert c.low == 99.0
    assert c.close == 105.0
    assert c.volume == 500.0
    assert c.trades_count == 120
    assert c.is_closed is True
    assert c.is_bullish is True
    assert c.is_bearish is False

    # Malformed array raises CandleDataError
    with pytest.raises(CandleDataError):
      Candle.from_rest_kline("BTCUSDT", "1m", ["bad", "data"])

  def test_from_ws_payload(self):
    ws_event = {
        "e": "kline",
        "E": 1700000060000,
        "s": "BTCUSDT",
        "k": {
            "t": 1700000000000,
            "T": 1700000059999,
            "s": "BTCUSDT",
            "i": "1m",
            "o": "100.0",
            "c": "95.0",
            "h": "102.0",
            "l": "94.0",
            "v": "10.0",
            "q": "950.0",
            "n": 50,
            "x": True,  # is_closed
        },
    }

    c = Candle.from_ws_payload(ws_event)
    assert c.symbol == "BTCUSDT"
    assert c.is_closed is True
    assert c.is_bearish is True
    assert c.duration_ms == 60000

    # Incomplete kline payload
    with pytest.raises(CandleDataError):
      Candle.from_ws_payload({"bad": "payload"})


# ==============================================================================
# 2. Candle Buffer Tests
# ==============================================================================


class TestCandleBuffer:
  """Test suite for CandleBuffer ordering, deduplication, and gap detection."""

  def test_chronological_ordering_and_append(self):
    buf = CandleBuffer("BTCUSDT", "1m", max_capacity=100)
    base_time = 1700000000000

    c1 = make_candle(open_time=base_time)
    c2 = make_candle(open_time=base_time + 60000)
    c3 = make_candle(open_time=base_time + 120000)

    assert buf.add_candle(c1) is True
    assert buf.add_candle(c2) is True
    assert buf.add_candle(c3) is True
    assert len(buf) == 3

    candles = buf.get_candles()
    assert [c.open_time for c in candles] == [
        base_time,
        base_time + 60000,
        base_time + 120000,
    ]
    assert buf.get_latest_candle() == c3

  def test_duplicate_candles_idempotent_update(self):
    buf = CandleBuffer("BTCUSDT", "1m", max_capacity=100)
    t0 = 1700000000000

    c1 = make_candle(open_time=t0, close_price=100.0)
    c1_update = make_candle(open_time=t0, close_price=105.0)

    assert buf.add_candle(c1) is True
    assert len(buf) == 1

    # Adding same open_time updates data without appending duplicate
    assert buf.add_candle(c1_update) is False
    assert len(buf) == 1
    assert buf.get_latest_candle().close == 105.0

  def test_out_of_order_insertion(self):
    buf = CandleBuffer("BTCUSDT", "1m", max_capacity=100)
    t0 = 1700000000000

    c1 = make_candle(open_time=t0)
    c3 = make_candle(open_time=t0 + 120000)
    c2 = make_candle(open_time=t0 + 60000)  # Arrives late

    buf.add_candle(c1)
    buf.add_candle(c3)
    assert len(buf) == 2

    # Insert c2 (out-of-order)
    assert buf.add_candle(c2) is True
    assert len(buf) == 3
    assert [c.open_time for c in buf.get_candles()] == [
        t0,
        t0 + 60000,
        t0 + 120000,
    ]

  def test_incomplete_candles_rejected_in_strict_mode(self):
    buf = CandleBuffer("BTCUSDT", "1m", strict_closed=True)
    c_open = make_candle(is_closed=False)

    assert buf.add_candle(c_open) is False
    assert len(buf) == 0

  def test_symbol_or_interval_mismatch_raises_error(self):
    buf = CandleBuffer("BTCUSDT", "1m")
    wrong_sym = make_candle(symbol="ETHUSDT", interval="1m")
    wrong_int = make_candle(symbol="BTCUSDT", interval="15m")

    with pytest.raises(CandleDataError):
      buf.add_candle(wrong_sym)

    with pytest.raises(CandleDataError):
      buf.add_candle(wrong_int)

  def test_detect_gaps(self):
    buf = CandleBuffer("BTCUSDT", "1m")
    t0 = 1700000000000

    c1 = make_candle(open_time=t0)
    # Skip t0 + 60000
    c3 = make_candle(open_time=t0 + 120000)

    buf.add_candle(c1)
    buf.add_candle(c3)

    gaps = buf.detect_gaps()
    assert len(gaps) == 1
    assert gaps[0] == (t0 + 60000, t0 + 120000)

  def test_capacity_eviction(self):
    buf = CandleBuffer("BTCUSDT", "1m", max_capacity=50)
    base_time = 1700000000000

    # Add 60 candles
    for i in range(60):
      buf.add_candle(make_candle(open_time=base_time + (i * 60000)))

    assert len(buf) == 50
    # First 10 candles evicted; oldest candle is now index 10
    assert buf.get_candles()[0].open_time == base_time + (10 * 60000)

  def test_buffer_clear_and_add_many(self):
    buf = CandleBuffer("BTCUSDT", "1m", max_capacity=100)
    candles = [make_candle(open_time=1700000000000 + (i * 60000)) for i in range(5)]
    added = buf.add_many(candles)
    assert added == 5
    assert len(buf) == 5
    assert buf.has_sufficient_history(5) is True
    assert buf.has_sufficient_history(10) is False

    buf.clear()
    assert len(buf) == 0
    assert buf.get_latest_candle() is None


# ==============================================================================
# 3. Market Data Engine Tests
# ==============================================================================


@pytest.fixture
def mock_client():
  client = AsyncMock(spec=BinanceFuturesClient)
  client.get_synchronized_time.return_value = 1700000065000
  return client


class TestMarketDataEngine:
  """Test suite for MarketDataEngine coordination and streaming."""

  @pytest.mark.asyncio
  async def test_seed_symbol_history(self, mock_client):
    # Mock REST klines: 2 closed candles
    mock_client.get_klines.return_value = [
        [
            1700000000000,
            "100",
            "105",
            "99",
            "102",
            "10",
            1700000059999,
            "1000",
            50,
            "5",
            "500",
            "0",
        ],
        [
            1700000060000,
            "102",
            "106",
            "101",
            "104",
            "15",
            1700000119999,
            "1500",
            60,
            "7",
            "700",
            "0",
        ],
    ]

    engine = MarketDataEngine(client=mock_client)
    # Current time is 1700000065000, so only candle 1 (closing at 1700000059999) is considered closed
    await engine.seed_symbol_history(
        symbol="BTCUSDT", intervals=("1m",), limits={"1m": 5}
    )

    candles = engine.get_candles("BTCUSDT", "1m")
    assert len(candles) == 1
    assert candles[0].open_time == 1700000000000

  @pytest.mark.asyncio
  async def test_process_kline_payload_closed_vs_unclosed(self, mock_client):
    engine = MarketDataEngine(client=mock_client)
    listener_called = []

    async def on_close(event: CandleCloseEvent):
      listener_called.append(event)

    engine.subscribe_candle_closed(on_close)

    # 1. Unclosed kline event (x: False)
    unclosed_event = {
        "k": {
            "t": 1700000000000,
            "T": 1700000059999,
            "s": "BTCUSDT",
            "i": "1m",
            "o": "100.0",
            "c": "102.0",
            "h": "103.0",
            "l": "99.0",
            "v": "5.0",
            "x": False,
        }
    }
    res_unclosed = await engine.process_kline_payload(unclosed_event)
    assert res_unclosed is None
    assert len(listener_called) == 0
    assert len(engine.get_candles("BTCUSDT", "1m")) == 0

    # 2. Closed kline event (x: True)
    closed_event = {
        "k": {
            "t": 1700000000000,
            "T": 1700000059999,
            "s": "BTCUSDT",
            "i": "1m",
            "o": "100.0",
            "c": "102.0",
            "h": "103.0",
            "l": "99.0",
            "v": "10.0",
            "x": True,
        }
    }
    res_closed = await engine.process_kline_payload(closed_event)
    assert res_closed is not None
    assert len(listener_called) == 1
    assert listener_called[0].candle.open_time == 1700000000000
    assert len(engine.get_candles("BTCUSDT", "1m")) == 1

    # 3. Duplicate closed kline event -> does not re-emit
    res_dup = await engine.process_kline_payload(closed_event)
    assert res_dup is None
    assert len(listener_called) == 1

  @pytest.mark.asyncio
  async def test_reconcile_rest_snapshot(self, mock_client):
    mock_client.get_klines.return_value = [
        [
            1700000000000,
            "100",
            "105",
            "99",
            "102",
            "10",
            1700000059999,
            "1000",
            50,
            "5",
            "500",
            "0",
        ],
        [
            1700000060000,
            "102",
            "106",
            "101",
            "104",
            "15",
            1700000119999,
            "1500",
            60,
            "7",
            "700",
            "0",
        ],
    ]
    # Current time past both candles
    mock_client.get_synchronized_time.return_value = 1700000200000

    engine = MarketDataEngine(client=mock_client)
    added = await engine.reconcile_rest_snapshot("BTCUSDT", "1m")
    assert added == 2
    assert len(engine.get_candles("BTCUSDT", "1m")) == 2

    # Second reconciliation adds 0 duplicates
    added_second = await engine.reconcile_rest_snapshot("BTCUSDT", "1m")
    assert added_second == 0

  @pytest.mark.asyncio
  async def test_is_data_stale(self, mock_client):
    engine = MarketDataEngine(client=mock_client)

    # Empty buffer is considered stale
    assert engine.is_data_stale("BTCUSDT", "1m") is True

    # Add candle with close_time 1700000059999
    c = make_candle(open_time=1700000000000)
    engine.get_buffer("BTCUSDT", "1m").add_candle(c)

    # If now_ms is only 30s after close, not stale
    mock_client.get_synchronized_time.return_value = 1700000089999
    assert engine.is_data_stale("BTCUSDT", "1m") is False

    # If now_ms is 5 minutes after close (> 2.5 * 60s = 150s), data is stale
    mock_client.get_synchronized_time.return_value = 1700000400000
    assert engine.is_data_stale("BTCUSDT", "1m") is True

  @pytest.mark.asyncio
  async def test_set_monitored_universe(self, mock_client):
    mock_client.get_klines.return_value = []
    engine = MarketDataEngine(client=mock_client)

    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    await engine.set_monitored_universe(symbols, intervals=("1m", "15m"))

    assert "btcusdt@kline_1m" in engine.ws._subscribed_streams
    assert "btcusdt@kline_15m" in engine.ws._subscribed_streams
    assert "ethusdt@kline_1m" in engine.ws._subscribed_streams
    assert "solusdt@kline_1m" in engine.ws._subscribed_streams
    assert len(engine.ws._subscribed_streams) == 6

    # Test start and stop
    with patch.object(engine.ws, "start") as mock_ws_start, patch.object(
        engine.ws, "stop", new_callable=AsyncMock
    ) as mock_ws_stop:
      engine.start()
      mock_ws_start.assert_called_once()
      await engine.stop()
      mock_ws_stop.assert_called_once()

  @pytest.mark.asyncio
  async def test_on_ws_reconnect_triggers_reconciliation(self, mock_client):
    engine = MarketDataEngine(client=mock_client)
    # Seed symbol buffer
    engine.get_buffer("BTCUSDT", "1m")
    mock_client.get_klines.return_value = []

    with patch.object(
        engine, "reconcile_rest_snapshot", new_callable=AsyncMock
    ) as mock_rec:
      await engine._on_ws_reconnect()
      # Should reconcile for 1m and 15m
      assert mock_rec.call_count == 2


# ==============================================================================
# 4. WebSocket Feed Tests
# ==============================================================================


class TestWebSocketFeed:
  """Test suite for BinanceKlineWebSocket client."""

  def test_stream_url_generation(self):
    ws = BinanceKlineWebSocket(ws_base_url="wss://stream.binancefuture.com")
    ws.subscribe_streams(["btcusdt@kline_1m", "ethusdt@kline_15m"])
    url = ws._build_stream_url()
    assert (
        url
        == "wss://stream.binancefuture.com/stream?streams=btcusdt@kline_1m/ethusdt@kline_15m"
    )

  def test_empty_streams_raises_value_error(self):
    ws = BinanceKlineWebSocket()
    with pytest.raises(ValueError):
      ws._build_stream_url()

  @pytest.mark.asyncio
  async def test_ws_handler_dispatch(self):
    ws = BinanceKlineWebSocket()
    received_msgs = []

    async def dummy_handler(data: dict):
      received_msgs.append(data)

    ws.register_handler(dummy_handler)
    ws.subscribe_streams(["btcusdt@kline_1m"])

    # Simulate message receiving using mock connection
    mock_ws = AsyncMock()
    mock_ws.recv.side_effect = [
        '{"stream": "btcusdt@kline_1m", "data": {"s": "BTCUSDT", "k": {"x": true}}}',
        asyncio.CancelledError(),  # break loop
    ]

    with patch("websockets.connect") as mock_connect:
      mock_connect.return_value.__aenter__.return_value = mock_ws
      ws.start()
      await asyncio.sleep(0.05)
      await ws.stop()

    assert len(received_msgs) == 1
    assert received_msgs[0]["s"] == "BTCUSDT"
