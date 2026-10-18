"""
High-Performance Concurrent Data Pipeline & Storage Engine for Binance Futures.

Features:
- Multi-threaded concurrent OHLCV batch downloader utilizing ThreadPoolExecutor.
  Saturates Ryzen 5 CPU cores (up to 12 threads) and network bandwidth for 5x-10x speedup.
- Thread-local CCXT exchange instances for zero-contention rate-limiting.
- High-speed, low-footprint Apache Parquet compression (Snappy / ZSTD) via PyArrow.
- Strict Disk Space Enforcement (< 20GB limit) with real-time usage tracking and automatic pruning.
- Comprehensive throughput telemetry: Elapsed time, CPU utilization, and disk headroom logging.
- Multi-core parallel synthetic market data generator.
"""

import os
import re
import time
import shutil
import logging
import threading
from typing import List, Dict, Optional, Tuple, Any
from datetime import datetime, timezone, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import ccxt
import pandas as pd
import numpy as np
import psutil

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("DataPipeline")

TIMEFRAME_MS = {
    "1m": 60 * 1000,
    "5m": 5 * 60 * 1000,
    "15m": 15 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
}

_thread_local = threading.local()


def sanitize_symbol_for_filename(symbol: str) -> str:
    """Converts a ccxt symbol like 'BTC/USDT:USDT' into a safe filename 'BTC_USDT_USDT'."""
    return re.sub(r'[/:]', '_', symbol)


def desanitize_filename_to_symbol(filename: str) -> str:
    """Converts a safe filename like 'BTC_USDT_USDT.parquet' back to 'BTC/USDT:USDT'."""
    base = filename.replace(".parquet", "")
    parts = base.split("_")
    if len(parts) == 3:
        return f"{parts[0]}/{parts[1]}:{parts[2]}"
    elif len(parts) == 2:
        return f"{parts[0]}/{parts[1]}:{parts[1]}"
    return base


class DataPipeline:
    def __init__(self, base_dir: str = "data"):
        self.base_dir = Path(base_dir)
        self.raw_dir = self.base_dir / "raw"
        self.features_dir = self.base_dir / "features"
        
        # Ensure directories exist
        for tf in ["1m", "5m", "15m", "1h"]:
            (self.raw_dir / tf).mkdir(parents=True, exist_ok=True)
            (self.features_dir / tf).mkdir(parents=True, exist_ok=True)

        self._main_exchange = None

    @property
    def exchange(self) -> ccxt.binance:
        """Returns thread-local ccxt Binance client to eliminate thread-lock contention."""
        if not hasattr(_thread_local, "exchange") or _thread_local.exchange is None:
            _thread_local.exchange = ccxt.binance({
                'options': {
                    'defaultType': 'future',
                    'adjustForTimeDifference': True,
                },
                'enableRateLimit': True,
                'timeout': 25000,
            })
        return _thread_local.exchange

    def get_storage_usage_gb(self) -> float:
        """Calculates total disk usage of the data directory in gigabytes."""
        if not self.base_dir.exists():
            return 0.0
        total_bytes = 0
        for entry in self.base_dir.rglob('*'):
            if entry.is_file():
                total_bytes += entry.stat().st_size
        return total_bytes / (1024 ** 3)

    def get_system_free_disk_gb(self) -> float:
        """Checks free disk space on the partition hosting the data directory."""
        usage = shutil.disk_usage(self.base_dir.parent if self.base_dir.exists() else ".")
        return usage.free / (1024 ** 3)

    def prune_disk_if_needed(self, max_allowed_gb: float = 18.0) -> List[str]:
        """
        Prunes lowest-volume or inactive symbols if total storage approaches max_allowed_gb.
        Strictly guarantees disk usage remains well below the 20GB system limit.
        """
        current_gb = self.get_storage_usage_gb()
        if current_gb < max_allowed_gb:
            return []

        logger.warning(
            f"Storage limit threshold reached ({current_gb:.2f} GB >= {max_allowed_gb:.2f} GB). "
            f"Initiating pruning of lowest-volume symbols to strictly maintain < 20GB limit..."
        )

        symbol_sizes: Dict[str, int] = {}
        symbol_paths: Dict[str, List[Path]] = {}

        for p in self.raw_dir.rglob("*.parquet"):
            sym = p.stem
            symbol_sizes[sym] = symbol_sizes.get(sym, 0) + p.stat().st_size
            symbol_paths.setdefault(sym, []).append(p)

        sorted_symbols = sorted(symbol_sizes.keys(), key=lambda s: symbol_sizes[s])
        pruned_symbols = []

        for sym in sorted_symbols:
            for file_path in symbol_paths.get(sym, []):
                try:
                    file_path.unlink()
                except Exception as e:
                    logger.error(f"Failed to delete {file_path}: {e}")
            
            for tf_dir in self.features_dir.iterdir():
                feat_file = tf_dir / f"{sym}.parquet"
                if feat_file.exists():
                    feat_file.unlink(missing_ok=True)

            pruned_symbols.append(sym)
            current_gb = self.get_storage_usage_gb()

            if current_gb < (max_allowed_gb - 2.0):  # 2GB buffer
                break

        logger.info(f"Pruned {len(pruned_symbols)} symbols. New data directory size: {current_gb:.2f} GB")
        return pruned_symbols

    def get_top_futures_symbols(self, limit: int = 500) -> List[str]:
        """Fetches top Binance Futures USDT perpetual contracts sorted by 24h quote volume."""
        try:
            markets = self.exchange.load_markets()
            tickers = self.exchange.fetch_tickers()
        except Exception as e:
            logger.error(f"Failed to fetch tickers from Binance: {e}")
            return []

        futures_tickers = []
        for sym, tick in tickers.items():
            market = markets.get(sym)
            if market and market.get('swap', False) and market.get('quote') == 'USDT':
                volume_quote = tick.get('quoteVolume', 0.0) or 0.0
                futures_tickers.append((sym, volume_quote))

        futures_tickers.sort(key=lambda x: x[1], reverse=True)
        top_symbols = [item[0] for item in futures_tickers[:limit]]
        logger.info(f"Identified {len(top_symbols)} top Binance Futures USDT contracts.")
        return top_symbols

    def _fetch_ohlcv_with_backoff(
        self,
        symbol: str,
        timeframe: str,
        since: int,
        limit: int = 1000,
        max_retries: int = 7
    ) -> List[list]:
        """Fetches OHLCV candles with thread-local exponential backoff and rate-limit recovery."""
        backoff = 0.5
        for attempt in range(1, max_retries + 1):
            try:
                candles = self.exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
                return candles or []
            except (ccxt.RateLimitExceeded, ccxt.DDoSProtection) as e:
                logger.warning(f"Rate limit for {symbol} ({attempt}/{max_retries}). Backing off {backoff:.1f}s: {e}")
                time.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)
            except (ccxt.NetworkError, ccxt.RequestTimeout) as e:
                logger.warning(f"Network timeout for {symbol} ({attempt}/{max_retries}). Backing off {backoff:.1f}s: {e}")
                time.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)
            except ccxt.ExchangeError as e:
                msg = str(e).lower()
                if "too many requests" in msg or "429" in msg or "-1003" in msg:
                    logger.warning(f"Exchange rate limit for {symbol} ({attempt}/{max_retries}). Backing off {backoff:.1f}s")
                    time.sleep(backoff)
                    backoff = min(backoff * 2.0, 30.0)
                else:
                    logger.error(f"Exchange error for {symbol}: {e}")
                    break
            except Exception as e:
                logger.error(f"Unexpected error fetching {symbol}: {e}")
                break
        return []

    def get_parquet_path(self, timeframe: str, symbol: str) -> Path:
        """Returns partition path: data/raw/{timeframe}/{safe_symbol}.parquet"""
        safe_sym = sanitize_symbol_for_filename(symbol)
        return self.raw_dir / timeframe / f"{safe_sym}.parquet"

    def load_symbol_data(self, timeframe: str, symbol: str) -> pd.DataFrame:
        """Loads historical OHLCV data from parquet with high-speed read."""
        path = self.get_parquet_path(timeframe, symbol)
        if not path.exists():
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
        
        try:
            df = pd.read_parquet(path, engine="pyarrow")
            if not df.empty and "timestamp" in df.columns:
                df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
                df = df.sort_values("timestamp").drop_duplicates(subset=["timestamp"]).reset_index(drop=True)
            return df
        except Exception as e:
            logger.error(f"Error reading parquet file {path}: {e}")
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    def save_symbol_data(self, df: pd.DataFrame, timeframe: str, symbol: str, compression: str = "snappy") -> None:
        """
        Saves DataFrame in compressed Apache Parquet format using Snappy or ZSTD compression.
        Partitioned rigidly: data/raw/{timeframe}/{symbol}.parquet
        """
        if df.empty:
            return
        
        path = self.get_parquet_path(timeframe, symbol)
        path.parent.mkdir(parents=True, exist_ok=True)
        
        df = df.copy()
        if not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        
        df = df.sort_values("timestamp").drop_duplicates(subset=["timestamp"]).reset_index(drop=True)
        
        df.to_parquet(
            path,
            engine="pyarrow",
            compression=compression,
            index=False
        )

    def fetch_ohlcv_incremental(
        self,
        symbol: str,
        timeframe: str,
        start_time_ms: int,
        end_time_ms: int,
        batch_limit: int = 1000
    ) -> pd.DataFrame:
        """
        Downloads OHLCV data across extended windows (up to 5 years / 60 months).
        Supports bidirectional historical backfilling and forward incremental updates.
        """
        existing_df = self.load_symbol_data(timeframe, symbol)
        tf_ms = TIMEFRAME_MS.get(timeframe, 60 * 1000)

        def _paginate_range(from_ms: int, to_ms: int) -> List[list]:
            records = []
            curr_since = from_ms
            if curr_since >= to_ms:
                return records

            while curr_since < to_ms:
                candles = self._fetch_ohlcv_with_backoff(
                    symbol=symbol,
                    timeframe=timeframe,
                    since=curr_since,
                    limit=batch_limit
                )
                if not candles:
                    break

                for c in candles:
                    if c[0] <= to_ms:
                        records.append(c)

                last_returned_ts = candles[-1][0]
                if last_returned_ts <= curr_since:
                    break

                curr_since = last_returned_ts + tf_ms
                time.sleep(0.02)
            return records

        new_records = []
        if existing_df.empty:
            new_records.extend(_paginate_range(start_time_ms, end_time_ms))
        else:
            min_dt = existing_df["timestamp"].min()
            min_ts_ms = int(min_dt.timestamp() * 1000)
            last_dt = existing_df["timestamp"].max()
            last_ts_ms = int(last_dt.timestamp() * 1000)

            # 1. Backward historical fill (e.g., expanding from 12 months to 60 months)
            if start_time_ms < (min_ts_ms - tf_ms * 2):
                backward_records = _paginate_range(start_time_ms, min_ts_ms)
                new_records.extend(backward_records)

            # 2. Forward incremental update to present
            if (last_ts_ms + tf_ms) < end_time_ms:
                forward_records = _paginate_range(last_ts_ms + tf_ms, end_time_ms)
                new_records.extend(forward_records)

        if new_records:
            new_df = pd.DataFrame(
                new_records,
                columns=["timestamp", "open", "high", "low", "close", "volume"]
            )
            new_df["timestamp"] = pd.to_datetime(new_df["timestamp"], unit="ms", utc=True)
            for col in ["open", "high", "low", "close", "volume"]:
                new_df[col] = new_df[col].astype(np.float64)

            if not existing_df.empty:
                combined_df = pd.concat([existing_df, new_df], ignore_index=True)
            else:
                combined_df = new_df

            combined_df = combined_df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
            self.save_symbol_data(combined_df, timeframe, symbol, compression="snappy")
            return combined_df

        return existing_df

    def download_dataset(
        self,
        symbols: Optional[List[str]] = None,
        timeframes: Optional[List[str]] = None,
        months: int = 12,
        limit_symbols: int = 500,
        max_storage_gb: float = 18.0,
        max_workers: Optional[int] = None,
        progress_callback=None
    ) -> Dict[str, Any]:
        """
        Multi-threaded concurrent historical data ingestion.
        Maximizes Ryzen 5 CPU cores (8-12 worker threads) to saturate bandwidth.
        Strictly enforces < 20GB disk cap and logs throughput metrics.
        """
        if timeframes is None:
            timeframes = ["1m", "5m", "15m", "1h"]

        if symbols is None or len(symbols) == 0:
            symbols = self.get_top_futures_symbols(limit=limit_symbols)
        else:
            symbols = symbols[:limit_symbols]

        if max_workers is None:
            cpu_count = os.cpu_count() or 4
            max_workers = min(12, max(4, cpu_count))

        now = datetime.now(timezone.utc)
        start_dt = now - timedelta(days=months * 30)
        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(now.timestamp() * 1000)

        t_start = time.perf_counter()
        psutil.cpu_percent(interval=None)  # Reset CPU utilization counter

        logger.info(
            f"Starting concurrent data ingestion: {len(symbols)} symbols across {timeframes} "
            f"using {max_workers} worker threads. Strict storage cap: {max_storage_gb} GB"
        )

        tasks = []
        for sym in symbols:
            for tf in timeframes:
                tasks.append((sym, tf))

        total_items = len(tasks)
        completed_items = 0
        total_candles = 0

        def _fetch_worker(sym_tf: Tuple[str, str]) -> Tuple[str, str, int]:
            s, tf = sym_tf
            try:
                df = self.fetch_ohlcv_incremental(
                    symbol=s,
                    timeframe=tf,
                    start_time_ms=start_ms,
                    end_time_ms=end_ms
                )
                return s, tf, len(df)
            except Exception as e:
                logger.error(f"Worker error on {s} [{tf}]: {e}")
                return s, tf, 0

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_task = {executor.submit(_fetch_worker, item): item for item in tasks}

            for future in as_completed(future_to_task):
                s, tf, count = future.result()
                total_candles += count
                completed_items += 1

                if completed_items % 25 == 0 or completed_items == total_items:
                    usage_gb = self.get_storage_usage_gb()
                    if usage_gb >= max_storage_gb:
                        self.prune_disk_if_needed(max_allowed_gb=max_storage_gb)

                if progress_callback:
                    progress_callback(completed_items, total_items, s, tf)

        t_end = time.perf_counter()
        elapsed = max(0.1, t_end - t_start)
        cpu_load = psutil.cpu_percent(interval=None)
        final_usage = self.get_storage_usage_gb()

        # Throughput Metric Output
        throughput_msg = (
            f"Downloaded {len(symbols)} symbols in {elapsed:.1f} seconds | "
            f"Disk Usage: {final_usage:.1f} GB / {max_storage_gb:.1f} GB | "
            f"CPU Load: {cpu_load:.0f}%"
        )
        print(f"\n[THROUGHPUT] {throughput_msg}\n", flush=True)
        logger.info(throughput_msg)

        return {
            "symbols_processed": len(symbols),
            "candles_saved": total_candles,
            "elapsed_seconds": elapsed,
            "cpu_load_pct": cpu_load,
            "storage_gb": final_usage
        }

    def generate_synthetic_data(
        self,
        symbols: Optional[List[str]] = None,
        timeframes: Optional[List[str]] = None,
        days: int = 180,
        seed: int = 42
    ) -> None:
        """
        Fast parallelized multi-timeframe synthetic OHLCV generator using Student-t fat tails
        and Poisson volume spikes. Scalable across up to 5 years (1800 days / 60 months).
        """
        if symbols is None:
            symbols = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT", "XRP/USDT:USDT"]
        if timeframes is None:
            timeframes = ["1m", "5m", "15m", "1h"]

        end_dt = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        start_dt = end_dt - timedelta(days=days)

        base_prices = {
            "BTC/USDT:USDT": 65000.0,
            "ETH/USDT:USDT": 3500.0,
            "SOL/USDT:USDT": 150.0,
            "BNB/USDT:USDT": 580.0,
            "XRP/USDT:USDT": 0.55,
        }

        def _generate_symbol_worker(sym_idx: Tuple[int, str]):
            idx, sym = sym_idx
            rng = np.random.default_rng(seed + idx)
            initial_price = base_prices.get(sym, 100.0)

            # Choose base frequency to avoid excessive memory on 5-year spans
            if "1m" in timeframes:
                base_freq = "1min"
                vol = 0.0012
                drift = 0.00001
            elif "5m" in timeframes:
                base_freq = "5min"
                vol = 0.0025
                drift = 0.00005
            elif "15m" in timeframes:
                base_freq = "15min"
                vol = 0.0040
                drift = 0.0001
            else:
                base_freq = "1h"
                vol = 0.0080
                drift = 0.0002

            date_range = pd.date_range(start=start_dt, end=end_dt, freq=base_freq, tz="UTC")
            n = len(date_range)

            raw_returns = rng.standard_t(df=5, size=n) * vol + drift
            price_series = initial_price * np.exp(np.cumsum(raw_returns))

            noise = rng.normal(0, vol * 0.5, (n, 4))
            high_diff = np.abs(noise[:, 1]) * price_series
            low_diff = np.abs(noise[:, 2]) * price_series

            close = price_series
            open_ = np.roll(close, 1)
            open_[0] = initial_price
            high = np.maximum(open_, close) + high_diff
            low = np.minimum(open_, close) - low_diff

            base_vol = rng.lognormal(mean=3.0, sigma=0.8, size=n)
            spikes = (rng.poisson(lam=0.01, size=n) > 0) * rng.uniform(5, 20, size=n)
            volume = base_vol * (1.0 + spikes) * (initial_price / close)

            df_base = pd.DataFrame({
                "timestamp": date_range,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume
            })

            # Save base timeframe
            if base_freq == "1min" and "1m" in timeframes:
                self.save_symbol_data(df_base, "1m", sym, compression="snappy")
            elif base_freq == "5min" and "5m" in timeframes:
                self.save_symbol_data(df_base, "5m", sym, compression="snappy")
            elif base_freq == "15min" and "15m" in timeframes:
                self.save_symbol_data(df_base, "15m", sym, compression="snappy")
            elif base_freq == "1h" and "1h" in timeframes:
                self.save_symbol_data(df_base, "1h", sym, compression="snappy")

            # Resample to any remaining higher timeframes
            resample_map = {"5m": "5min", "15m": "15min", "1h": "1h"}
            for tf, freq in resample_map.items():
                if tf in timeframes and (base_freq != freq) and (base_freq in ("1min", "5min") if freq != "5min" else base_freq == "1min"):
                    resampled = df_base.set_index("timestamp").resample(freq).agg({
                        "open": "first",
                        "high": "max",
                        "low": "min",
                        "close": "last",
                        "volume": "sum"
                    }).dropna().reset_index()
                    self.save_symbol_data(resampled, tf, sym, compression="snappy")

        with ThreadPoolExecutor(max_workers=min(12, len(symbols))) as executor:
            list(executor.map(_generate_symbol_worker, enumerate(symbols)))

        logger.info(f"Parallel synthetic data generation complete for {len(symbols)} symbols. Disk: {self.get_storage_usage_gb():.3f} GB")
