"""
Multi-Timeframe Feature Engineering Module for Trend-Following Crypto Research.

Calculations:
- Momentum: RSI(14), MACD(12, 26, 9), ROC(5, 10, 20), Multi-period returns (1, 3, 5, 10, 20).
- Volume: Volume ROC (VROC), Volume MA Ratios (Volume / SMA(Volume, 20)), Distance from Rolling VWAP.
- Volatility & Trend: ATR(14), Bollinger Band Width & %B, Exponential Moving Averages (EMA 20, 50, 200) and crossovers.
- Multi-Timeframe Alignment: Merges higher timeframe features (15m, 1h) into execution timeframes (1m, 5m)
  strictly using backward-looking completion timestamps (pd.merge_asof) to guarantee zero look-ahead bias.
"""

import logging
from typing import List, Optional, Dict, Tuple
from pathlib import Path

import numpy as np
import pandas as pd

from data_pipeline import DataPipeline, sanitize_symbol_for_filename, TIMEFRAME_MS

logger = logging.getLogger("FeatureEngineering")


def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Computes Wilder's Relative Strength Index (RSI)."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    
    rs = avg_gain / (avg_loss + 1e-12)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(50.0)


def calculate_macd(
    series: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9
) -> Tuple_MACD:
    """Computes Moving Average Convergence Divergence (MACD)."""
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    macd_hist = macd_line - signal_line
    return macd_line, signal_line, macd_hist


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Computes Average True Range (ATR)."""
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)
    
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = true_range.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    return atr.bfill().fillna(0.0)


def calculate_bollinger_bands(
    series: pd.Series,
    period: int = 20,
    num_std: float = 2.0
) -> Tuple_BB:
    """Computes Bollinger Bands, Bandwidth, and %B position."""
    sma = series.rolling(window=period, min_periods=1).mean()
    std = series.rolling(window=period, min_periods=1).std().fillna(0.0)
    
    upper = sma + (std * num_std)
    lower = sma - (std * num_std)
    
    bb_width = (upper - lower) / (sma + 1e-12)
    percent_b = (series - lower) / (upper - lower + 1e-12)
    return upper, lower, sma, bb_width.fillna(0.0), percent_b.fillna(0.5)


def calculate_rolling_vwap(df: pd.DataFrame, window: int = 24) -> pd.Series:
    """Computes Rolling Volume-Weighted Average Price (VWAP)."""
    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    vol_price = typical_price * df["volume"]
    
    rolling_vol_price = vol_price.rolling(window=window, min_periods=1).sum()
    rolling_vol = df["volume"].rolling(window=window, min_periods=1).sum()
    
    vwap = rolling_vol_price / (rolling_vol + 1e-12)
    return vwap.fillna(df["close"])


Tuple_MACD = tuple[pd.Series, pd.Series, pd.Series]
Tuple_BB = tuple[pd.Series, pd.Series, pd.Series, pd.Series, pd.Series]


class FeatureEngineer:
    def __init__(self, pipeline: Optional[DataPipeline] = None):
        self.pipeline = pipeline or DataPipeline()

    def compute_single_timeframe_features(
        self,
        df: pd.DataFrame,
        prefix: str = ""
    ) -> pd.DataFrame:
        """
        Computes all trend-following, momentum, volume, and volatility indicators for a single timeframe.
        """
        if df.empty or len(df) < 50:
            return pd.DataFrame()

        df = df.copy().sort_values("timestamp").reset_index(drop=True)
        close = df["close"]
        volume = df["volume"]

        feat = pd.DataFrame(index=df.index)
        feat["timestamp"] = df["timestamp"]

        # 1. Momentum Features
        feat[f"{prefix}rsi_14"] = calculate_rsi(close, 14)
        
        macd, macd_sig, macd_hist = calculate_macd(close, 12, 26, 9)
        feat[f"{prefix}macd"] = macd / (close + 1e-12)
        feat[f"{prefix}macd_signal"] = macd_sig / (close + 1e-12)
        feat[f"{prefix}macd_hist"] = macd_hist / (close + 1e-12)
        
        # Rate of Change (ROC)
        for p in [5, 10, 20]:
            feat[f"{prefix}roc_{p}"] = ((close - close.shift(p)) / (close.shift(p) + 1e-12)) * 100.0

        # Multi-period Returns
        for p in [1, 3, 5, 10, 20]:
            feat[f"{prefix}return_{p}"] = (close / (close.shift(p) + 1e-12)) - 1.0

        # 2. Volume Features
        for p in [5, 14]:
            feat[f"{prefix}vroc_{p}"] = ((volume - volume.shift(p)) / (volume.shift(p) + 1e-12)) * 100.0
            
        vol_sma_20 = volume.rolling(window=20, min_periods=1).mean()
        feat[f"{prefix}vol_ratio_20"] = volume / (vol_sma_20 + 1e-12)

        vwap = calculate_rolling_vwap(df, window=24)
        feat[f"{prefix}dist_vwap"] = (close - vwap) / (vwap + 1e-12)

        # 3. Volatility & Trend Indicators
        atr = calculate_atr(df, 14)
        feat[f"{prefix}atr_14"] = atr
        feat[f"{prefix}natr_14"] = (atr / (close + 1e-12)) * 100.0

        upper_bb, lower_bb, mid_bb, bb_width, pct_b = calculate_bollinger_bands(close, 20, 2.0)
        feat[f"{prefix}bb_width"] = bb_width
        feat[f"{prefix}bb_pct_b"] = pct_b

        ema_20 = close.ewm(span=20, adjust=False).mean()
        ema_50 = close.ewm(span=50, adjust=False).mean()
        ema_200 = close.ewm(span=200, adjust=False).mean()

        feat[f"{prefix}ema_20_50_diff"] = (ema_20 - ema_50) / (ema_50 + 1e-12)
        feat[f"{prefix}ema_50_200_diff"] = (ema_50 - ema_200) / (ema_200 + 1e-12)
        feat[f"{prefix}trend_bullish"] = (
            (close > ema_20) & (ema_20 > ema_50) & (ema_50 > ema_200)
        ).astype(np.float32)

        return feat

    def build_aligned_multi_timeframe_features(
        self,
        symbol: str,
        base_timeframe: str = "5m",
        htf_timeframes: Optional[List[str]] = None
    ) -> pd.DataFrame:
        """
        Builds multi-timeframe features by merging higher timeframe (e.g. 15m, 1h) indicators
        into the base timeframe (e.g. 1m, 5m).
        
        Strict Look-Ahead Bias Prevention:
        A higher-timeframe candle is only available to the base timeframe once that HTF candle
        has completely closed. We map each HTF record to its closure timestamp (open_time + tf_duration)
        and perform a backward merge_asof so base candles strictly access past closed HTF metrics.
        """
        if htf_timeframes is None:
            htf_timeframes = ["15m", "1h"]

        # Load base timeframe raw data
        base_raw = self.pipeline.load_symbol_data(base_timeframe, symbol)
        if base_raw.empty or len(base_raw) < 50:
            logger.warning(f"Insufficient base raw data for {symbol} [{base_timeframe}]")
            return pd.DataFrame()

        # Compute base timeframe features
        base_features = self.compute_single_timeframe_features(base_raw, prefix="")
        
        # Retain original price columns for labeling and backtesting
        merged = base_raw.merge(base_features, on="timestamp", how="inner")

        # Merge each higher timeframe
        for htf in htf_timeframes:
            htf_raw = self.pipeline.load_symbol_data(htf, symbol)
            if htf_raw.empty or len(htf_raw) < 30:
                logger.warning(f"HTF data missing for {symbol} [{htf}], skipping alignment.")
                continue

            htf_features = self.compute_single_timeframe_features(htf_raw, prefix=f"htf_{htf}_")
            if htf_features.empty:
                continue

            # Align HTF timestamps to candle CLOSURE timestamp to guarantee 0 lookahead bias
            htf_duration_ms = TIMEFRAME_MS.get(htf, 60 * 60 * 1000)
            htf_aligned = htf_features.copy()
            htf_aligned["avail_timestamp"] = htf_aligned["timestamp"] + pd.to_timedelta(htf_duration_ms, unit="ms")
            htf_aligned = htf_aligned.drop(columns=["timestamp"]).sort_values("avail_timestamp")

            # Backward merge_asof: for every base bar at 'timestamp', grab latest HTF bar where avail_timestamp <= timestamp
            merged = pd.merge_asof(
                merged.sort_values("timestamp"),
                htf_aligned,
                left_on="timestamp",
                right_on="avail_timestamp",
                direction="backward"
            )
            merged = merged.drop(columns=["avail_timestamp"])

        # Forward fill any initial HTF warm-up gaps, then drop remaining uninitialized warmup rows
        merged = merged.ffill().dropna().reset_index(drop=True)
        return merged

    def save_feature_dataset(self, df: pd.DataFrame, timeframe: str, symbol: str, compression: str = "snappy") -> None:
        """Saves engineered features to data/features/{timeframe}/{safe_symbol}.parquet."""
        if df.empty:
            return
        safe_sym = sanitize_symbol_for_filename(symbol)
        out_path = self.pipeline.features_dir / timeframe / f"{safe_sym}.parquet"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(out_path, engine="pyarrow", compression=compression, index=False)

    def load_feature_dataset(self, timeframe: str, symbol: str) -> pd.DataFrame:
        """Loads engineered features from data/features/{timeframe}/{safe_symbol}.parquet."""
        safe_sym = sanitize_symbol_for_filename(symbol)
        out_path = self.pipeline.features_dir / timeframe / f"{safe_sym}.parquet"
        if not out_path.exists():
            return pd.DataFrame()
        return pd.read_parquet(out_path, engine="pyarrow")

    def batch_build_features(
        self,
        symbols: List[str],
        base_timeframe: str = "5m",
        htf_timeframes: Optional[List[str]] = None,
        max_workers: Optional[int] = None,
        progress_callback=None
    ) -> int:
        """
        Builds and caches multi-timeframe features concurrently across multiple symbols
        utilizing all Ryzen 5 CPU cores for parallelized speed.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        if htf_timeframes is None:
            htf_timeframes = ["15m", "1h"]

        if max_workers is None:
            max_workers = min(12, max(2, len(symbols)))

        count = 0
        completed = 0

        def _worker(sym: str) -> Tuple[str, bool]:
            try:
                feat_df = self.build_aligned_multi_timeframe_features(
                    symbol=sym,
                    base_timeframe=base_timeframe,
                    htf_timeframes=htf_timeframes
                )
                if not feat_df.empty:
                    self.save_feature_dataset(feat_df, base_timeframe, sym, compression="snappy")
                    return sym, True
            except Exception as e:
                logger.error(f"Error building features for {sym}: {e}")
            return sym, False

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_sym = {executor.submit(_worker, sym): sym for sym in symbols}
            for future in as_completed(future_to_sym):
                sym, success = future.result()
                completed += 1
                if success:
                    count += 1
                if progress_callback:
                    progress_callback(completed, len(symbols), sym)

        logger.info(f"Built and saved features for {count}/{len(symbols)} symbols in parallel.")
        return count
