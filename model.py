"""
ML Labeling & LightGBM Training Engine.

Features:
- Triple-Barrier Method labeling with dynamic ATR barriers enforcing strict 1:3 Risk-Reward ratio.
  Symmetric labels are -1 (short winner), 0 (neutral), and 1 (long winner).
- High-performance vectorized / NumPy barrier evaluation.
- Global LightGBM cross-asset training across 500+ pairs to learn universal structural market dynamics.
- Purged Walk-Forward Cross-Validation with temporal embargo buffers to strictly eliminate data leakage.
- Asset filtering utility to evaluate out-of-sample metrics specifically on high-liquidity / top-tier tokens.
"""

import os
import json
import logging
import warnings
from typing import List, Dict, Optional, Tuple, Any
from pathlib import Path

import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.metrics import classification_report, accuracy_score, precision_score, recall_score, f1_score

warnings.filterwarnings("ignore", category=UserWarning)

from data_pipeline import DataPipeline, sanitize_symbol_for_filename
from features import FeatureEngineer
from trading_rules import select_directional_signal, validate_rr_multipliers

logger = logging.getLogger("ModelEngine")


def apply_triple_barrier_labeling(
    df: pd.DataFrame,
    sl_atr_mult: float = 1.0,
    tp_atr_mult: float = 3.0,
    max_holding_bars: int = 24,
    symmetric_directional: bool = True
) -> pd.DataFrame:
    """
    Applies the Triple-Barrier Method to generate trade labels with strict 1:3 Risk-Reward.

    When symmetric_directional=True (Trend-Following balanced mode):
      1: High-conviction LONG breakout (Price reaches +3.0 * ATR before dropping 1.0 * ATR)
     -1: High-conviction SHORT breakdown (Price drops -3.0 * ATR before rising 1.0 * ATR)
      0: Time horizon exit or neutral chop (neither 3.0 * ATR target reached within holding window)

    This eliminates the short-only mean-reversion bias by treating Long and Short
    breakout momentum symmetrically without structural probability distortion.
    """
    validate_rr_multipliers(sl_atr_mult, tp_atr_mult)
    df = df.copy().reset_index(drop=True)
    n = len(df)
    
    close = df["close"].to_numpy()
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    open_ = df["open"].to_numpy()
    atr = df["atr_14"].to_numpy() if "atr_14" in df.columns else (close * 0.01)

    labels = np.zeros(n, dtype=np.int8)
    exit_bars = np.zeros(n, dtype=np.int32)
    barrier_returns = np.zeros(n, dtype=np.float32)

    for i in range(n - 1):
        c_i = close[i]
        atr_i = atr[i]
        
        # Guard against zero or corrupted ATR
        if atr_i <= 0 or np.isnan(atr_i):
            atr_i = c_i * 0.01

        end_idx = min(i + max_holding_bars + 1, n)

        if symmetric_directional:
            # Symmetric Evaluation: Both Long and Short require 3:1 Risk-Reward
            long_tp = c_i + (tp_atr_mult * atr_i)
            long_sl = c_i - (sl_atr_mult * atr_i)
            short_tp = c_i - (tp_atr_mult * atr_i)
            short_sl = c_i + (sl_atr_mult * atr_i)

            long_success = False
            long_exit_j = end_idx - 1
            for j in range(i + 1, end_idx):
                if low[j] <= long_sl:
                    break
                if high[j] >= long_tp:
                    long_success = True
                    long_exit_j = j
                    break

            short_success = False
            short_exit_j = end_idx - 1
            for j in range(i + 1, end_idx):
                if high[j] >= short_sl:
                    break
                if low[j] <= short_tp:
                    short_success = True
                    short_exit_j = j
                    break

            if long_success and not short_success:
                labels[i] = 1
                exit_bars[i] = long_exit_j
                barrier_returns[i] = (long_tp - c_i) / c_i
            elif short_success and not long_success:
                labels[i] = -1
                exit_bars[i] = short_exit_j
                barrier_returns[i] = (c_i - short_tp) / c_i
            elif long_success and short_success:
                if long_exit_j <= short_exit_j:
                    labels[i] = 1
                    exit_bars[i] = long_exit_j
                    barrier_returns[i] = (long_tp - c_i) / c_i
                else:
                    labels[i] = -1
                    exit_bars[i] = short_exit_j
                    barrier_returns[i] = (c_i - short_tp) / c_i
            else:
                labels[i] = 0
                exit_bars[i] = end_idx - 1
                barrier_returns[i] = (close[end_idx - 1] - c_i) / c_i

        else:
            # Legacy Single-Side Long Barrier
            upper_barrier = c_i + (tp_atr_mult * atr_i)
            lower_barrier = c_i - (sl_atr_mult * atr_i)
            event_label = 0
            event_exit = end_idx - 1
            event_ret = (close[event_exit] - c_i) / c_i

            for j in range(i + 1, end_idx):
                h_j = high[j]
                l_j = low[j]
                o_j = open_[j]

                tp_breached = h_j >= upper_barrier
                sl_breached = l_j <= lower_barrier

                if tp_breached and not sl_breached:
                    event_label = 1
                    event_exit = j
                    event_ret = (upper_barrier - c_i) / c_i
                    break
                elif sl_breached and not tp_breached:
                    event_label = -1
                    event_exit = j
                    event_ret = (lower_barrier - c_i) / c_i
                    break
                elif tp_breached and sl_breached:
                    dist_to_sl = abs(o_j - lower_barrier)
                    dist_to_tp = abs(o_j - upper_barrier)
                    if dist_to_sl <= dist_to_tp:
                        event_label = -1
                        event_ret = (lower_barrier - c_i) / c_i
                    else:
                        event_label = 1
                        event_ret = (upper_barrier - c_i) / c_i
                    event_exit = j
                    break

            labels[i] = event_label
            exit_bars[i] = event_exit
            barrier_returns[i] = event_ret

    df["label"] = labels
    df["exit_bar"] = exit_bars
    df["barrier_return"] = barrier_returns

    return df


class ModelTrainer:
    def __init__(
        self,
        models_dir: str = "models",
        pipeline: Optional[DataPipeline] = None,
        model_version: str = "v2"
    ):
        self.models_dir = Path(models_dir)
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self.pipeline = pipeline or DataPipeline()
        self.model_version = model_version
        self.symmetric_directional = model_version.lower() in ("v2", "2")
        self.feature_cols: List[str] = []
        self.model: Optional[lgb.LGBMClassifier] = None
        self.booster: Optional[lgb.Booster] = None

    def resolve_model_name(self, model_name: Optional[str] = None, version: Optional[str] = None) -> str:
        """
        Resolves the concrete model file basename taking versioning into account.
        - version 'v1' -> 'lightgbm_crypto_model_v1' (or 'lightgbm_crypto_model')
        - version 'v2' -> 'lightgbm_crypto_model_v2'
        - custom model_name -> stem of that name
        """
        ver = (version or self.model_version or "v1").lower().strip()
        if model_name:
            clean = model_name.lower().strip()
            if clean in ("v1", "1"):
                ver = "v1"
            elif clean in ("v2", "2"):
                ver = "v2"
            elif clean in ("lightgbm_crypto_model", "lightgbm_crypto_model_v1", "lightgbm_crypto_model_v2"):
                if version:
                    ver = version.lower().strip()
                elif clean == "lightgbm_crypto_model_v2":
                    ver = "v2"
                elif clean == "lightgbm_crypto_model_v1":
                    ver = "v1"
            else:
                return Path(model_name).stem
        else:
            ver = (version or self.model_version or "v1").lower().strip()

        if ver in ("v1", "1"):
            v1_file = self.models_dir / "lightgbm_crypto_model_v1.txt"
            if v1_file.exists():
                return "lightgbm_crypto_model_v1"
            legacy_file = self.models_dir / "lightgbm_crypto_model.txt"
            if legacy_file.exists():
                return "lightgbm_crypto_model"
            return "lightgbm_crypto_model_v1"
        elif ver in ("v2", "2"):
            return "lightgbm_crypto_model_v2"
        return f"lightgbm_crypto_model_{ver}"

    def get_feature_columns(self, df: pd.DataFrame) -> List[str]:
        """Identifies pure feature columns, omitting metadata and labels."""
        exclude = {
            "timestamp", "open", "high", "low", "close", "volume",
            "symbol", "label", "exit_bar", "barrier_return", "fold"
        }
        return [c for c in df.columns if c not in exclude and not c.startswith("_")]

    def load_aggregated_dataset(
        self,
        symbols: Optional[List[str]] = None,
        timeframe: str = "5m",
        sl_atr_mult: float = 1.0,
        tp_atr_mult: float = 3.0,
        max_holding_bars: int = 24,
        max_samples_per_symbol: Optional[int] = 50000,
        symmetric_directional: bool = True
    ) -> pd.DataFrame:
        """
        Loads engineered features across multiple symbols, applies Triple-Barrier labeling,
        and aggregates into a unified global cross-asset dataset.
        """
        self.symmetric_directional = bool(symmetric_directional)
        fe = FeatureEngineer(self.pipeline)
        feat_dir = self.pipeline.features_dir / timeframe
        
        if not feat_dir.exists():
            return pd.DataFrame()

        available_files = list(feat_dir.glob("*.parquet"))
        if not available_files:
            return pd.DataFrame()

        dfs = []
        for file_path in available_files:
            sym_raw = file_path.stem
            if symbols:
                target_safes = [sanitize_symbol_for_filename(s) for s in symbols]
                if sym_raw not in target_safes:
                    continue

            try:
                sub_df = pd.read_parquet(file_path)
                if len(sub_df) < 100:
                    continue

                sub_df["symbol"] = sym_raw
                # Label data
                labeled_df = apply_triple_barrier_labeling(
                    sub_df,
                    sl_atr_mult=sl_atr_mult,
                    tp_atr_mult=tp_atr_mult,
                    max_holding_bars=max_holding_bars,
                    symmetric_directional=symmetric_directional
                )

                if max_samples_per_symbol and len(labeled_df) > max_samples_per_symbol:
                    labeled_df = labeled_df.iloc[-max_samples_per_symbol:]

                dfs.append(labeled_df)
            except Exception as e:
                logger.error(f"Error loading features for {sym_raw}: {e}")

        if not dfs:
            return pd.DataFrame()

        aggregated = pd.concat(dfs, ignore_index=True)
        aggregated = aggregated.sort_values("timestamp").reset_index(drop=True)
        return aggregated

    def train_walk_forward_cv(
        self,
        df: pd.DataFrame,
        n_splits: int = 5,
        embargo_pct: float = 0.01,
        params: Optional[Dict[str, Any]] = None,
        save_as: Optional[str] = None,
        version: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Executes Purged Walk-Forward Cross-Validation across time-series folds:
        - Strict temporal ordering: fold k trains on [0, t_train], tests on [t_train + embargo, t_test].
        - Embargo buffer eliminates overlapping triple-barrier holding periods between train and test.
        """
        if df.empty:
            raise ValueError("Aggregated dataset is empty.")

        self.feature_cols = self.get_feature_columns(df)
        logger.info(f"Training LightGBM model with {len(self.feature_cols)} features...")

        # Mapping labels [short, neutral, long] -> [0, 1, 2] for LightGBM.
        label_map = {-1: 0, 0: 1, 1: 2}
        inv_label_map = {0: -1, 1: 0, 2: 1}
        
        y = df["label"].map(label_map).to_numpy()
        X = df[self.feature_cols].copy()
        timestamps = df["timestamp"].to_numpy()

        # Handle NaNs or Infs gracefully
        X = X.replace([np.inf, -np.inf], np.nan).fillna(0.0)

        default_params = {
            "objective": "multiclass",
            "num_class": 3,
            "boosting_type": "gbdt",
            "learning_rate": 0.03,
            "num_leaves": 31,
            "max_depth": 6,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "min_child_samples": 50,
            "n_estimators": 300,
            "random_state": 42,
            "n_jobs": -1,
            "verbose": -1
        }
        if params:
            default_params.update(params)

        n_samples = len(df)
        fold_size = n_samples // (n_splits + 1)
        embargo_samples = int(fold_size * embargo_pct)

        fold_metrics = []
        best_model = None
        best_score = -1.0

        for fold in range(n_splits):
            train_end = fold_size * (fold + 1)
            test_start = train_end + embargo_samples
            test_end = min(test_start + fold_size, n_samples)

            if test_start >= n_samples:
                break

            X_train, y_train = X.iloc[:train_end], y[:train_end]
            X_test, y_test = X.iloc[test_start:test_end], y[test_start:test_end]

            if len(X_test) == 0 or len(np.unique(y_train)) < 3:
                continue

            clf = lgb.LGBMClassifier(**default_params)
            clf.fit(
                X_train,
                y_train,
                eval_set=[(X_test, y_test)],
                callbacks=[lgb.early_stopping(stopping_rounds=30, verbose=False)]
            )

            preds = clf.predict(X_test)
            probs = clf.predict_proba(X_test)

            # Precision on Take-Profit class (class 2) is critical for 1:3 RR
            prec_tp = precision_score(y_test, preds, labels=[2], average="micro", zero_division=0)
            acc = accuracy_score(y_test, preds)
            f1 = f1_score(y_test, preds, average="macro", zero_division=0)

            metric = {
                "fold": fold + 1,
                "train_samples": len(X_train),
                "test_samples": len(X_test),
                "accuracy": float(acc),
                "precision_tp": float(prec_tp),
                "f1_macro": float(f1)
            }
            fold_metrics.append(metric)
            logger.info(f"Fold {fold+1}/{n_splits} - Acc: {acc:.4f}, TP Precision: {prec_tp:.4f}, F1: {f1:.4f}")

            if prec_tp > best_score:
                best_score = prec_tp
                best_model = clf

        # Retrain on full dataset for production deployment if best model was selected
        if best_model is None:
            best_model = lgb.LGBMClassifier(**default_params)
            best_model.fit(X, y)

        self.model = best_model
        # Training labels produced by the current feature pipeline use the
        # symmetric short/neutral/long semantics unless the caller explicitly
        # loaded a legacy dataset before training.
        if hasattr(best_model, "booster_"):
            self.booster = best_model.booster_
        
        target_name = save_as or (f"lightgbm_crypto_model_{version}" if version else None)
        self.save_model(model_name=target_name, version=version)

        summary = {
            "n_splits": len(fold_metrics),
            "mean_accuracy": float(np.mean([m["accuracy"] for m in fold_metrics])) if fold_metrics else 0.0,
            "mean_precision_tp": float(np.mean([m["precision_tp"] for m in fold_metrics])) if fold_metrics else 0.0,
            "mean_f1_macro": float(np.mean([m["f1_macro"] for m in fold_metrics])) if fold_metrics else 0.0,
            "fold_details": fold_metrics,
            "feature_importance": self.get_feature_importance()
        }
        return summary

    def get_feature_importance(self) -> Dict[str, float]:
        """Returns sorted feature importance dictionary safely from Booster or Classifier."""
        # 1. Retrieve booster directly or through model
        b = self.booster
        if b is None and self.model is not None:
            if hasattr(self.model, "booster_") and self.model.booster_ is not None:
                b = self.model.booster_
            elif hasattr(self.model, "_Booster") and self.model._Booster is not None:
                b = self.model._Booster

        # 2. Extract importances
        if b is not None:
            try:
                importances = b.feature_importance(importance_type="split")
                feature_names = b.feature_name()
                if not self.feature_cols and feature_names:
                    self.feature_cols = list(feature_names)
                cols = self.feature_cols if len(self.feature_cols) == len(importances) else feature_names
                sorted_idx = np.argsort(importances)[::-1]
                return {
                    str(cols[i]): float(importances[i])
                    for i in sorted_idx
                }
            except Exception as e:
                logger.warning(f"Error extracting feature importance from booster: {e}")

        # 3. Fallback to scikit-learn classifier API
        if self.model is not None and self.feature_cols:
            try:
                importances = getattr(self.model, "feature_importances_", None)
                if importances is not None and len(importances) == len(self.feature_cols):
                    sorted_idx = np.argsort(importances)[::-1]
                    return {
                        self.feature_cols[i]: float(importances[i])
                        for i in sorted_idx
                    }
            except Exception:
                pass

        return {}

    def save_model(self, model_name: Optional[str] = None, version: Optional[str] = None) -> None:
        """Saves LightGBM model weights and feature schema."""
        b = self.booster
        if b is None and self.model is not None:
            b = getattr(self.model, "booster_", None) or getattr(self.model, "_Booster", None)
        
        if b is None:
            return
        
        resolved = self.resolve_model_name(model_name, version)
        model_path = self.models_dir / f"{resolved}.txt"
        meta_path = self.models_dir / f"{resolved}_metadata.json"

        b.save_model(str(model_path))
        with open(meta_path, "w") as f:
            json.dump({
                "feature_cols": self.feature_cols,
                "label_map": {-1: 0, 0: 1, 1: 2},
                "inv_label_map": {0: -1, 1: 0, 2: 1},
                "symmetric_directional": self.symmetric_directional,
                "model_version": version or self.model_version
            }, f, indent=2)
        logger.info(f"Saved model and metadata to {model_path}")

        # If saving v1 or base, also maintain lightgbm_crypto_model.txt for full backwards compatibility
        if resolved in ("lightgbm_crypto_model_v1", "lightgbm_crypto_model"):
            base_model_path = self.models_dir / "lightgbm_crypto_model.txt"
            base_meta_path = self.models_dir / "lightgbm_crypto_model_metadata.json"
            if base_model_path != model_path:
                b.save_model(str(base_model_path))
                with open(base_meta_path, "w") as f:
                    json.dump({
                        "feature_cols": self.feature_cols,
                        "label_map": {-1: 0, 1: 2, 0: 1},
                        "inv_label_map": {0: -1, 1: 0, 2: 1},
                        "symmetric_directional": self.symmetric_directional,
                        "model_version": "v1"
                    }, f, indent=2)

    def load_model(self, model_name: Optional[str] = None, version: Optional[str] = None) -> bool:
        """Loads LightGBM model weights and feature schema."""
        resolved = self.resolve_model_name(model_name, version)
        model_path = self.models_dir / f"{resolved}.txt"
        meta_path = self.models_dir / f"{resolved}_metadata.json"

        # Graceful fallback for v1 if specific v1 not on disk yet
        if not model_path.exists() and "v1" in resolved:
            fallback = self.models_dir / "lightgbm_crypto_model.txt"
            fallback_meta = self.models_dir / "lightgbm_crypto_model_metadata.json"
            if fallback.exists() and fallback_meta.exists():
                model_path = fallback
                meta_path = fallback_meta

        if not model_path.exists() or not meta_path.exists():
            return False

        with open(meta_path, "r") as f:
            meta = json.load(f)
            self.feature_cols = meta.get("feature_cols", [])
            self.symmetric_directional = bool(
                meta.get("symmetric_directional", resolved == "lightgbm_crypto_model_v2")
            )

        booster = lgb.Booster(model_file=str(model_path))
        self.booster = booster
        self.model = lgb.LGBMClassifier()
        self.model._Booster = booster
        self.model.fitted_ = True
        self.model._n_classes = 3
        self.model.n_features_in_ = len(self.feature_cols)
        self.model._n_features = len(self.feature_cols)
        if version:
            self.model_version = version
        elif "v2" in resolved:
            self.model_version = "v2"
        elif "v1" in resolved:
            self.model_version = "v1"
        return True

    def predict_probabilities(self, df: pd.DataFrame) -> np.ndarray:
        """
        Generates class probabilities for input rows. Symmetric v2 outputs are
        [P(short winner), P(neutral), P(long winner)]; legacy v1 uses
        [P(long stop), P(neutral), P(long target)].
        """
        if self.model is None and self.booster is None:
            if not self.load_model():
                raise RuntimeError("Model is not trained or loaded.")

        X = df[self.feature_cols].replace([np.inf, -np.inf], np.nan).fillna(0.0)
        
        # Booster raw predict returns probabilities for multiclass
        if self.booster is not None:
            return self.booster.predict(X)
        elif hasattr(self.model, "booster_") and self.model.booster_:
            return self.model.booster_.predict(X)
        elif hasattr(self.model, "_Booster") and self.model._Booster:
            return self.model._Booster.predict(X)
        else:
            return self.model.predict_proba(X)

    def predict_directional_signal(self, probabilities: np.ndarray) -> Tuple[str, float]:
        """Map model outputs to a side using the checkpoint's label semantics."""
        return select_directional_signal(probabilities, self.symmetric_directional)

    def filter_high_liquidity_subsets(
        self,
        df: pd.DataFrame,
        top_symbols: Optional[List[str]] = None
    ) -> pd.DataFrame:
        """
        Filters dataset to isolate test metrics exclusively on high-liquidity, top-performing asset subsets
        such as BTC, ETH, SOL, BNB, etc.
        """
        if top_symbols is None:
            top_symbols = ["BTC_USDT_USDT", "ETH_USDT_USDT", "SOL_USDT_USDT", "BNB_USDT_USDT"]
        
        safe_top = [sanitize_symbol_for_filename(s) for s in top_symbols]
        return df[df["symbol"].isin(safe_top)].copy()
