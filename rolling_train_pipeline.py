"""
Rolling-Batch Multi-Symbol Ingestion and Training Pipeline.

Features:
- Processes 500+ Binance Futures pairs in configurable rolling batches (e.g., 50-100 symbols).
- Iteratively accumulates patterns into a global LightGBM model using incremental booster updates.
- Strict Disk Space Enforcement (< 20GB limit): Automatically purges raw Parquet files after
  each batch is absorbed by the model, guaranteeing disk usage never exceeds 18GB.
- Full audit logging to logs/platform_audit.log.
"""

import os
import gc
import json
import logging
from typing import List, Optional, Dict, Any
from pathlib import Path
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import lightgbm as lgb

from data_pipeline import DataPipeline, sanitize_symbol_for_filename
from features import FeatureEngineer
from model import apply_triple_barrier_labeling, ModelTrainer

# Set up audit and platform logger
logger = logging.getLogger("RollingTrainPipeline")
AUDIT_LOG_PATH = Path("logs/platform_audit.log")
AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


def log_audit(message: str, level: str = "INFO") -> None:
    """Appends an event to logs/platform_audit.log with ISO timestamp."""
    now_iso = datetime.now(timezone.utc).isoformat()
    log_line = f"[{now_iso}] [{level}] {message}\n"
    with open(AUDIT_LOG_PATH, "a") as f:
        f.write(log_line)
    logger.info(message)


class RollingBatchPipeline:
    def __init__(
        self,
        base_dir: str = "data",
        models_dir: str = "models",
        max_storage_gb: float = 18.0
    ):
        self.pipeline = DataPipeline(base_dir=base_dir)
        self.fe = FeatureEngineer(self.pipeline)
        self.models_dir = Path(models_dir)
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self.max_storage_gb = max_storage_gb

    def purge_raw_batch_data(self, symbols: List[str], timeframes: Optional[List[str]] = None) -> int:
        """
        Deletes raw Parquet files for the specified batch of symbols across timeframes.
        Ensures disk usage is reclaimed immediately after model training.
        """
        if timeframes is None:
            timeframes = ["1m", "5m", "15m", "1h"]

        deleted_count = 0
        for sym in symbols:
            safe_sym = sanitize_symbol_for_filename(sym)
            for tf in timeframes:
                raw_file = self.pipeline.raw_dir / tf / f"{safe_sym}.parquet"
                if raw_file.exists():
                    try:
                        raw_file.unlink()
                        deleted_count += 1
                    except Exception as e:
                        logger.error(f"Failed to delete {raw_file}: {e}")

        logger.info(f"Purged {deleted_count} raw parquet files for {len(symbols)} symbols.")
        return deleted_count

    def purge_feature_batch_data(self, symbols: List[str], timeframes: Optional[List[str]] = None) -> int:
        """Deletes cached feature Parquet files for batch symbols to conserve disk."""
        if timeframes is None:
            timeframes = ["5m"]

        deleted_count = 0
        for sym in symbols:
            safe_sym = sanitize_symbol_for_filename(sym)
            for tf in timeframes:
                feat_file = self.pipeline.features_dir / tf / f"{safe_sym}.parquet"
                if feat_file.exists():
                    try:
                        feat_file.unlink()
                        deleted_count += 1
                    except Exception as e:
                        logger.error(f"Failed to delete feature file {feat_file}: {e}")
        return deleted_count

    def run_rolling_training(
        self,
        symbols: Optional[List[str]] = None,
        batch_size: int = 50,
        months: int = 12,
        timeframes: Optional[List[str]] = None,
        base_timeframe: str = "5m",
        htf_timeframes: Optional[List[str]] = None,
        rounds_per_batch: int = 40,
        sl_atr_mult: float = 1.0,
        tp_atr_mult: float = 3.0,
        max_horizon_bars: int = 24,
        synthetic: bool = False,
        progress_callback=None
    ) -> Dict[str, Any]:
        """
        Executes end-to-end rolling-batch training:
        1. Slices symbol universe into batches of size `batch_size`.
        2. Ingests data for batch symbols.
        3. Builds multi-timeframe aligned features.
        4. Applies Triple-Barrier (1:3 RR) labeling.
        5. Updates global LightGBM booster incrementally.
        6. Purges raw files for the batch to strictly prevent disk overflow (< 20GB).
        """
        if timeframes is None:
            timeframes = ["1m", "5m", "15m", "1h"]
        if htf_timeframes is None:
            htf_timeframes = ["15m", "1h"]

        # If no symbols provided, fetch top Binance futures pairs
        if not symbols or len(symbols) == 0:
            symbols = self.pipeline.get_top_futures_symbols(limit=500)
            if not symbols:
                # Fallback to default high-volume symbols
                symbols = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT", "XRP/USDT:USDT"]

        num_symbols = len(symbols)
        num_batches = int(np.ceil(num_symbols / batch_size))

        log_audit(
            f"Initiating rolling-batch training for {num_symbols} symbols in {num_batches} batches "
            f"(Batch Size: {batch_size}, Storage Cap: {self.max_storage_gb} GB)"
        )

        model_path = self.models_dir / "lightgbm_crypto_model.txt"
        meta_path = self.models_dir / "lightgbm_crypto_model_metadata.json"

        booster = None
        feature_cols: List[str] = []

        # Load existing booster if present to continue training
        if model_path.exists() and meta_path.exists():
            try:
                booster = lgb.Booster(model_file=str(model_path))
                with open(meta_path, "r") as f:
                    meta = json.load(f)
                    feature_cols = meta.get("feature_cols", [])
                log_audit(f"Loaded existing model booster with {booster.current_iteration()} iterations.")
            except Exception as e:
                logger.warning(f"Could not load existing booster, starting fresh: {e}")
                booster = None

        lgb_params = {
            "objective": "multiclass",
            "num_class": 3,
            "boosting_type": "gbdt",
            "learning_rate": 0.03,
            "num_leaves": 31,
            "max_depth": 6,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "min_child_samples": 50,
            "random_state": 42,
            "n_jobs": -1,
            "verbose": -1
        }

        batch_summaries = []

        for b_idx in range(num_batches):
            batch_syms = symbols[b_idx * batch_size : (b_idx + 1) * batch_size]
            log_audit(f"Processing Batch {b_idx + 1}/{num_batches} ({len(batch_syms)} symbols)...")

            # Check and prune disk if approaching limit
            current_gb = self.pipeline.get_storage_usage_gb()
            if current_gb >= self.max_storage_gb:
                log_audit(f"Approaching storage ceiling ({current_gb:.2f} GB). Triggering prune.", "WARNING")
                self.pipeline.prune_disk_if_needed(max_allowed_gb=self.max_storage_gb)

            # 1. Ingest Data for Batch
            if synthetic:
                self.pipeline.generate_synthetic_data(
                    symbols=batch_syms,
                    timeframes=timeframes,
                    days=months * 30
                )
            else:
                self.pipeline.download_dataset(
                    symbols=batch_syms,
                    timeframes=timeframes,
                    months=months,
                    limit_symbols=len(batch_syms),
                    max_storage_gb=self.max_storage_gb
                )

            # 2. Build Multi-Timeframe Features for Batch
            batch_dfs = []
            for sym in batch_syms:
                try:
                    feat_df = self.fe.build_aligned_multi_timeframe_features(
                        symbol=sym,
                        base_timeframe=base_timeframe,
                        htf_timeframes=htf_timeframes
                    )
                    if not feat_df.empty and len(feat_df) >= 100:
                        labeled_df = apply_triple_barrier_labeling(
                            feat_df,
                            sl_atr_mult=sl_atr_mult,
                            tp_atr_mult=tp_atr_mult,
                            max_holding_bars=max_horizon_bars
                        )
                        # Keep a representative sample per symbol for memory safety
                        if len(labeled_df) > 10000:
                            labeled_df = labeled_df.iloc[-10000:]
                        batch_dfs.append(labeled_df)
                except Exception as e:
                    logger.error(f"Error processing features for {sym}: {e}")

            if not batch_dfs:
                log_audit(f"Batch {b_idx + 1} produced no valid training data. Moving to next batch.", "WARNING")
                self.purge_raw_batch_data(batch_syms, timeframes)
                continue

            batch_dataset = pd.concat(batch_dfs, ignore_index=True)
            log_audit(f"Batch {b_idx + 1} assembled {len(batch_dataset)} training samples.")

            # Identify features
            if not feature_cols:
                exclude = {
                    "timestamp", "open", "high", "low", "close", "volume",
                    "symbol", "label", "exit_bar", "barrier_return", "fold"
                }
                feature_cols = [c for c in batch_dataset.columns if c not in exclude and not c.startswith("_")]

            label_map = {-1: 0, 0: 1, 1: 2}
            y_batch = batch_dataset["label"].map(label_map).to_numpy()
            X_batch = batch_dataset[feature_cols].replace([np.inf, -np.inf], np.nan).fillna(0.0)

            # Create LightGBM Dataset
            train_data = lgb.Dataset(X_batch, label=y_batch, free_raw_data=True)

            # 3. Train Booster Incrementally
            if booster is None:
                booster = lgb.train(
                    lgb_params,
                    train_data,
                    num_boost_round=rounds_per_batch
                )
            else:
                booster = lgb.train(
                    lgb_params,
                    train_data,
                    num_boost_round=rounds_per_batch,
                    init_model=booster,
                    keep_training_booster=True
                )

            # 4. Save updated model
            booster.save_model(str(model_path))
            with open(meta_path, "w") as f:
                json.dump({
                    "feature_cols": feature_cols,
                    "label_map": {-1: 0, 0: 1, 1: 2},
                    "inv_label_map": {0: -1, 1: 0, 2: 1},
                    "total_iterations": booster.current_iteration(),
                    "batches_completed": b_idx + 1
                }, f, indent=2)

            # 5. STRICT DISK ENFORCEMENT: Purge raw parquet files for this batch immediately!
            purged_files = self.purge_raw_batch_data(batch_syms, timeframes)
            
            # Clean memory
            del batch_dfs, batch_dataset, X_batch, y_batch, train_data
            gc.collect()

            post_gb = self.pipeline.get_storage_usage_gb()
            progress_msg = (
                f"[Batch {b_idx + 1}/{num_batches}] Processing {len(batch_syms)} symbols | "
                f"Features extracted: {len(batch_dataset):,} rows | "
                f"Model trees updated: {booster.num_trees()} total trees | "
                f"Current Disk Usage: {post_gb:.1f} GB / {self.max_storage_gb:.1f} GB"
            )
            print(f"\n{progress_msg}\n", flush=True)
            log_audit(progress_msg)
            log_audit(f"Purged {purged_files} raw files for batch {b_idx + 1}. Disk usage: {post_gb:.3f} GB")

            batch_summaries.append({
                "batch": b_idx + 1,
                "symbols_count": len(batch_syms),
                "model_iterations": booster.current_iteration(),
                "storage_gb": post_gb
            })

            if progress_callback:
                progress_callback(b_idx + 1, num_batches, post_gb)

        log_audit(
            f"Rolling-batch training completed successfully for all {num_symbols} symbols. "
            f"Final model iterations: {booster.current_iteration() if booster else 0}."
        )

        return {
            "status": "SUCCESS",
            "total_symbols": num_symbols,
            "total_batches": num_batches,
            "final_iterations": booster.current_iteration() if booster else 0,
            "final_storage_gb": self.pipeline.get_storage_usage_gb(),
            "batch_details": batch_summaries
        }
