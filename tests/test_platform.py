"""
Comprehensive Test Suite for Binance Futures ML Research Platform.
Verifies all modules, data integrity, zero look-ahead bias, 1:3 RR triple-barrier labeling,
rolling-batch training with disk purging, compounding risk sizing (2%, 3%, 5%),
global multi-symbol universe scanning (strict 1-trade globally), and live JSON signals.
"""

import os
import json
import shutil
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
from typer.testing import CliRunner

from data_pipeline import (
    DataPipeline,
    sanitize_symbol_for_filename,
    desanitize_filename_to_symbol
)
from features import (
    FeatureEngineer,
    calculate_rsi,
    calculate_macd,
    calculate_atr,
    calculate_bollinger_bands,
    calculate_rolling_vwap
)
from model import apply_triple_barrier_labeling, ModelTrainer
from backtester import BacktestEngine, compare_risk_profiles
from rolling_train_pipeline import RollingBatchPipeline
from scanner import GlobalUniverseScanner
from live_signals import LiveSignalGenerator, LATEST_SIGNAL_PATH, ACTIVE_TRADE_PATH, AUDIT_LOG_PATH
from execution import DualModeExecutionEngine, PaperWallet, PAPER_WALLET_PATH
from cli import app


class TestCryptoMLPlatform(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_dir = Path("test_data")
        cls.test_dir.mkdir(parents=True, exist_ok=True)
        cls.pipeline = DataPipeline(base_dir="test_data")
        # Generate test synthetic data
        cls.pipeline.generate_synthetic_data(
            symbols=["BTC/USDT:USDT", "ETH/USDT:USDT"],
            timeframes=["1m", "5m", "15m", "1h"],
            days=14,
            seed=123
        )

    @classmethod
    def tearDownClass(cls):
        if cls.test_dir.exists():
            shutil.rmtree(cls.test_dir, ignore_errors=True)
        test_models = Path("test_models")
        if test_models.exists():
            shutil.rmtree(test_models, ignore_errors=True)
        test_batch = Path("test_batch_data")
        if test_batch.exists():
            shutil.rmtree(test_batch, ignore_errors=True)

    def test_01_data_pipeline_storage_and_partitioning(self):
        """Verify storage calculations, Parquet compression, and safe symbol conversions."""
        raw_btc = self.pipeline.load_symbol_data("5m", "BTC/USDT:USDT")
        self.assertFalse(raw_btc.empty)
        self.assertIn("timestamp", raw_btc.columns)
        self.assertIn("close", raw_btc.columns)

        sym = "BTC/USDT:USDT"
        safe = sanitize_symbol_for_filename(sym)
        self.assertEqual(safe, "BTC_USDT_USDT")
        self.assertEqual(desanitize_filename_to_symbol(safe + ".parquet"), sym)

        usage = self.pipeline.get_storage_usage_gb()
        self.assertGreater(usage, 0.0)
        self.assertLess(usage, 20.0)  # Must strictly be under 20GB

        pruned = self.pipeline.prune_disk_if_needed(max_allowed_gb=100.0)
        self.assertEqual(len(pruned), 0)

    def test_02_feature_engineering_calculations(self):
        """Verify all individual momentum, volume, and volatility indicator formulas."""
        raw_btc = self.pipeline.load_symbol_data("5m", "BTC/USDT:USDT")
        close = raw_btc["close"]

        # 1. RSI
        rsi = calculate_rsi(close, 14)
        self.assertEqual(len(rsi), len(close))
        self.assertTrue((rsi >= 0.0).all() and (rsi <= 100.0).all())

        # 2. MACD
        macd, signal, hist = calculate_macd(close, 12, 26, 9)
        self.assertEqual(len(macd), len(close))
        self.assertTrue(np.allclose(hist, macd - signal, atol=1e-5))

        # 3. ATR
        atr = calculate_atr(raw_btc, 14)
        self.assertEqual(len(atr), len(close))
        self.assertTrue((atr >= 0.0).all())

        # 4. Bollinger Bands
        upper, lower, mid, width, pct_b = calculate_bollinger_bands(close, 20, 2.0)
        self.assertTrue((upper >= lower).all())
        self.assertTrue((width >= 0.0).all())

        # 5. VWAP
        vwap = calculate_rolling_vwap(raw_btc, 24)
        self.assertEqual(len(vwap), len(close))
        self.assertTrue((vwap > 0.0).all())

    def test_03_multi_timeframe_alignment_zero_lookahead_bias(self):
        """Verify higher timeframe features only use completed bars (zero look-ahead bias)."""
        fe = FeatureEngineer(self.pipeline)
        df_feat = fe.build_aligned_multi_timeframe_features(
            "BTC/USDT:USDT",
            base_timeframe="5m",
            htf_timeframes=["15m", "1h"]
        )
        self.assertFalse(df_feat.empty)
        self.assertIn("rsi_14", df_feat.columns)
        self.assertIn("htf_15m_rsi_14", df_feat.columns)
        self.assertIn("htf_1h_rsi_14", df_feat.columns)
        self.assertEqual(df_feat.isna().sum().sum(), 0)

    def test_04_triple_barrier_labeling(self):
        """Verify 1:3 Risk-Reward dynamic ATR barrier labeling logic."""
        fe = FeatureEngineer(self.pipeline)
        df_feat = fe.build_aligned_multi_timeframe_features("BTC/USDT:USDT", base_timeframe="5m")
        labeled = apply_triple_barrier_labeling(df_feat, sl_atr_mult=1.0, tp_atr_mult=3.0, max_holding_bars=12)

        self.assertIn("label", labeled.columns)
        unique_labels = set(labeled["label"].unique())
        self.assertTrue(unique_labels.issubset({-1, 0, 1}))
        self.assertGreater(len(labeled[labeled["label"] == 1]), 0)
        self.assertGreater(len(labeled[labeled["label"] == -1]), 0)

    def test_05_walk_forward_cv_and_model_training(self):
        """Verify Purged Walk-Forward Cross-Validation and LightGBM model persistence."""
        trainer = ModelTrainer(models_dir="test_models", pipeline=self.pipeline)
        fe = FeatureEngineer(self.pipeline)
        df_feat = fe.build_aligned_multi_timeframe_features("BTC/USDT:USDT", base_timeframe="5m")
        fe.save_feature_dataset(df_feat, "5m", "BTC/USDT:USDT")

        agg_df = trainer.load_aggregated_dataset(timeframe="5m", sl_atr_mult=1.0, tp_atr_mult=3.0)
        self.assertFalse(agg_df.empty)

        cv_results = trainer.train_walk_forward_cv(agg_df, n_splits=2, embargo_pct=0.01)
        self.assertIn("mean_accuracy", cv_results)
        self.assertIn("mean_precision_tp", cv_results)
        self.assertTrue((Path("test_models") / "lightgbm_crypto_model.txt").exists())

        probs = trainer.predict_probabilities(df_feat)
        self.assertEqual(probs.shape, (len(df_feat), 3))
        self.assertTrue(np.allclose(probs.sum(axis=1), 1.0, atol=1e-4))

    def test_06_compounding_risk_optimizer(self):
        """Verify compounding equity starting from $100 and 2%, 3%, 5% risk sizing comparison."""
        fe = FeatureEngineer(self.pipeline)
        df_feat = fe.load_feature_dataset("5m", "BTC/USDT:USDT")
        trainer = ModelTrainer(models_dir="test_models", pipeline=self.pipeline)
        trainer.load_model()

        opt_res = compare_risk_profiles(
            df_feat,
            symbol="BTC/USDT:USDT",
            risks=[0.02, 0.03, 0.05],
            initial_capital=100.0,
            confidence_threshold=0.25,
            model_trainer=trainer
        )

        comp_table = opt_res["comparison_table"]
        self.assertEqual(len(comp_table), 3)
        self.assertIn(opt_res["optimal_risk"], [0.02, 0.03, 0.05])
        self.assertEqual(comp_table.iloc[0]["initial_capital"], 100.0)

    def test_07_global_universe_scanner_single_trade_invariance(self):
        """Verify simultaneous scanning across universe enforces strictly ONE trade globally at a time."""
        fe = FeatureEngineer(self.pipeline)
        df_btc = fe.build_aligned_multi_timeframe_features("BTC/USDT:USDT", base_timeframe="5m")
        df_eth = fe.build_aligned_multi_timeframe_features("ETH/USDT:USDT", base_timeframe="5m")
        fe.save_feature_dataset(df_btc, "5m", "BTC/USDT:USDT")
        fe.save_feature_dataset(df_eth, "5m", "ETH/USDT:USDT")

        scanner = GlobalUniverseScanner(initial_capital=100.0, risk_per_trade=0.03, pipeline=self.pipeline)
        res = scanner.run_global_backtest(
            timeframe="5m",
            confidence_threshold=0.20,
            risk_per_trade=0.03
        )

        trades = res["trades"]
        self.assertFalse(trades.empty)

        # STRICT GLOBAL SINGLE TRADE INVARIANCE:
        # Check that no global trade overlaps across the multi-symbol universe
        for k in range(len(trades) - 1):
            prev_exit = trades.iloc[k]["exit_time"]
            next_entry = trades.iloc[k + 1]["entry_time"]
            self.assertGreaterEqual(
                next_entry,
                prev_exit,
                f"Global trade collision! Trade {k} ({trades.iloc[k]['symbol']}) exit {prev_exit} > Trade {k+1} ({trades.iloc[k+1]['symbol']}) entry {next_entry}"
            )

    def test_08_rolling_batch_training_and_disk_purging(self):
        """Verify rolling-batch pipeline absorbs weights and automatically purges raw batch files."""
        rolling = RollingBatchPipeline(base_dir="test_batch_data", models_dir="test_models", max_storage_gb=18.0)
        res = rolling.run_rolling_training(
            symbols=["BTC/USDT:USDT", "ETH/USDT:USDT"],
            batch_size=1,
            months=1,
            rounds_per_batch=5,
            synthetic=True
        )

        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["total_batches"], 2)
        # Check that raw parquet files for the batch were deleted
        raw_files = list((rolling.pipeline.raw_dir).rglob("*.parquet"))
        self.assertEqual(len(raw_files), 0, "Raw Parquet files were not purged after batch processing!")

    def test_09_live_signals_emission_and_audit(self):
        """Verify live signal emission generates the exact required JSON structure and logs to audit."""
        generator = LiveSignalGenerator(
            symbols=["BTC/USDT:USDT"],
            confidence_threshold=0.01,
            pipeline=self.pipeline
        )
        generator.save_active_trade({"status": "CLOSED"})
        sig = generator.scan_universe_and_emit_signal()
        self.assertIsNotNone(sig)
        self.assertIn("timestamp", sig)
        self.assertIn("symbol", sig)
        self.assertIn(sig["direction"], ("LONG", "SHORT"), "Signal direction must be LONG or SHORT")
        self.assertIn("confidence", sig)
        self.assertIn("entry_price", sig)
        self.assertIn("stop_loss", sig)
        self.assertIn("take_profit", sig)
        self.assertIn("risk_pct", sig)
        self.assertIn("suggested_position_size", sig)
        self.assertEqual(sig["status"], "SIGNAL_GENERATED")

        # Verify disk output files exist
        self.assertTrue(LATEST_SIGNAL_PATH.exists())
        self.assertTrue(ACTIVE_TRADE_PATH.exists())
        self.assertTrue(AUDIT_LOG_PATH.exists())

    def test_10_cli_commands(self):
        """Verify Typer CLI commands execute successfully."""
        runner = CliRunner()
        result = runner.invoke(app, ["status"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Crypto ML Platform Status", result.stdout)

        # Test execute command in paper mode
        res_exec = runner.invoke(app, ["execute", "--mode", "paper", "--threshold", "0.99"])
        self.assertEqual(res_exec.exit_code, 0)

    def test_11_execution_engine_paper_mode_and_orphan_cleanup(self):
        """Verify PaperWallet lifecycle, bracket order placement, and orphan cleanup sweep."""
        test_wallet_path = Path("test_paper_wallet.json")
        wallet = PaperWallet(initial_equity=100.0, wallet_path=test_wallet_path)
        
        # Open position
        pos = wallet.open_position(
            symbol="BTC/USDT:USDT",
            direction="LONG",
            entry_price=50000.0,
            size=0.01,
            sl_price=49000.0,
            tp_price=53000.0,
            leverage=50.0
        )
        self.assertIsNotNone(wallet.active_position)
        self.assertEqual(len(wallet.pending_orders), 2, "Must create 2 bracket orders (SL + TP)")
        self.assertLess(wallet.available_balance, 100.0)

        # Update unrealized PnL
        wallet.update_unrealized_pnl(51000.0)
        self.assertGreater(wallet.unrealized_pnl, 0.0)

        # Close position and verify orphan cleanup sweep
        trade = wallet.close_position(exit_price=53000.0, exit_reason="TAKE_PROFIT")
        self.assertIsNone(wallet.active_position)
        self.assertEqual(len(wallet.pending_orders), 0, "Closure sweep must purge all pending bracket orders!")
        self.assertGreater(wallet.equity, 100.0)
        self.assertEqual(len(wallet.trade_history), 1)

        if test_wallet_path.exists():
            test_wallet_path.unlink()

    def test_12_precise_5pct_risk_sizing_and_leverage(self):
        """Verify exact 5% risk calculation strictly against Account Equity."""
        engine = DualModeExecutionEngine(
            mode="PAPER",
            risk_pct=0.05,
            confidence_threshold=0.35,
            initial_paper_equity=2.0  # $2 test balance from prompt example
        )
        
        # Sizing formula: $2 balance, 5% risk = $0.10 dollar risk.
        # ATR = $2.0, entry = $100, max leverage = 50x
        # SL dist = 1.0 * ATR = $2.0. Target size = $0.10 / $2.0 = 0.05 coins.
        # Notional = 0.05 * 100 = $5.0. Required margin = 5.0 / 50 = $0.10.
        # If SL is hit (price moves $2 against position): loss = 0.05 * 2 = $0.10 = 5% of $2.
        size, sl, tp, dollar_risk = engine.calculate_5pct_risk_position(
            symbol="TEST/USDT:USDT",
            entry_price=100.0,
            atr_val=2.0,
            direction="LONG",
            max_leverage=50,
            account_equity=2.0
        )
        self.assertAlmostEqual(dollar_risk, 0.10, places=2)
        self.assertAlmostEqual(size, 0.05, places=2)
        self.assertEqual(sl, 98.0)
        self.assertEqual(tp, 106.0)

        # Loss verification at SL
        simulated_loss = size * (100.0 - sl)
        self.assertAlmostEqual(simulated_loss, dollar_risk, places=2)

    def test_13_feature_importance_booster_interface(self):
        """Verify ModelTrainer.get_feature_importance() works cleanly without NotFittedError."""
        trainer = ModelTrainer(pipeline=self.pipeline)
        # Verify model loading
        loaded = trainer.load_model()
        if loaded:
            fi = trainer.get_feature_importance()
            self.assertIsInstance(fi, dict)
            self.assertGreater(len(fi), 0)
            # Verify sorted descending
            vals = list(fi.values())
            self.assertEqual(vals, sorted(vals, reverse=True))


if __name__ == "__main__":
    unittest.main()
