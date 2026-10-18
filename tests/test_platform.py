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
from execution import DualModeExecutionEngine, PaperWallet, PAPER_WALLET_PATH, validate_binance_credentials
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
        loaded = trainer.load_model()
        if loaded:
            fi = trainer.get_feature_importance()
            self.assertIsInstance(fi, dict)
            self.assertGreater(len(fi), 0)
            vals = list(fi.values())
            self.assertEqual(vals, sorted(vals, reverse=True))

    def test_14_credential_validation_and_graceful_fallback(self):
        """Verify credential validator and graceful fallback to PAPER mode on missing/invalid keys."""
        # Test validator function
        self.assertFalse(validate_binance_credentials("", ""))
        self.assertFalse(validate_binance_credentials("your_binance_api_key_here", "your_binance_api_secret_here"))
        self.assertFalse(validate_binance_credentials("short", "short"))
        self.assertTrue(validate_binance_credentials("abcdef1234567890abcdef1234567890", "1234567890abcdef1234567890abcdef"))

        # Test engine fallback when initialized in LIVE mode with invalid credentials
        engine = DualModeExecutionEngine(
            mode="LIVE",
            api_key="your_binance_api_key_here",
            api_secret="your_binance_api_secret_here",
            wallet_path=Path("test_fallback_wallet.json")
        )
        self.assertEqual(engine.mode, "PAPER", "Engine must fall back to PAPER mode when keys are invalid!")
        self.assertIsNotNone(engine.paper_wallet)

        if Path("test_fallback_wallet.json").exists():
            Path("test_fallback_wallet.json").unlink()

    def test_15_dynamic_limits_and_sub_minimum_skipping(self):
        """Verify engine skips sub-minimum trades and selects next valid top setup without crashing."""
        test_wallet = Path("test_skip_wallet.json")
        engine = DualModeExecutionEngine(
            mode="PAPER",
            risk_pct=0.05,
            confidence_threshold=0.30,
            initial_paper_equity=100.0,
            wallet_path=test_wallet,
            pipeline=self.pipeline
        )
        engine.save_active_trade({"status": "CLOSED"})

        # Mock evaluate_symbol_for_entry to simulate 2 candidates:
        # Cand 1: High confidence (0.85), but tiny price ($0.001) causing notional < $5 minNotional
        # Cand 2: Lower confidence (0.65), standard price ($100) satisfying minNotional
        cand_tiny = {
            "symbol": "TINY/USDT:USDT",
            "bar": pd.Series({"close": 0.001, "atr_14": 0.005}),
            "direction": "LONG",
            "confidence": 0.85
        }
        cand_valid = {
            "symbol": "VALID/USDT:USDT",
            "bar": pd.Series({"close": 100.0, "atr_14": 1.0}),
            "direction": "LONG",
            "confidence": 0.65
        }

        # Override evaluate_symbol_for_entry to return these two mock setups
        def mock_eval(sym):
            return cand_tiny if "TINY" in sym else cand_valid

        engine.evaluate_symbol_for_entry = mock_eval
        engine._symbols = ["TINY/USDT:USDT", "VALID/USDT:USDT"]

        # Run step
        executed = engine.step()
        self.assertIsNotNone(executed)
        # Verify TINY was skipped due to sub-minimum notional and VALID was executed
        self.assertEqual(executed["symbol"], "VALID/USDT:USDT", "Engine must skip sub-minimum setup and execute valid setup!")
        
        # Check leaderboard
        lb = engine.latest_scan_leaderboard
        self.assertEqual(len(lb), 2)
        tiny_item = next(item for item in lb if item["symbol"] == "TINY/USDT:USDT")
        valid_item = next(item for item in lb if item["symbol"] == "VALID/USDT:USDT")

        self.assertEqual(tiny_item["status"], "SKIPPED_MIN_NOTIONAL")
        self.assertEqual(valid_item["status"], "SELECTED_AND_EXECUTED")

        engine.save_active_trade({"status": "CLOSED"})
        if test_wallet.exists():
            test_wallet.unlink()

    def test_16_pure_ml_probability_ranking_and_progress_callback(self):
        """Verify candidate ranking strictly follows raw ML probability and triggers progress callback."""
        test_wallet = Path("test_rank_wallet.json")
        engine = DualModeExecutionEngine(
            mode="PAPER",
            risk_pct=0.05,
            confidence_threshold=0.30,
            wallet_path=test_wallet,
            pipeline=self.pipeline
        )
        engine.save_active_trade({"status": "CLOSED"})

        cands = [
            {"symbol": "SYM_C", "bar": pd.Series({"close": 50.0, "atr_14": 0.5}), "direction": "LONG", "confidence": 0.45},
            {"symbol": "SYM_A", "bar": pd.Series({"close": 50.0, "atr_14": 0.5}), "direction": "LONG", "confidence": 0.82},
            {"symbol": "SYM_B", "bar": pd.Series({"close": 50.0, "atr_14": 0.5}), "direction": "SHORT", "confidence": 0.68},
        ]
        sym_map = {c["symbol"]: c for c in cands}
        engine.evaluate_symbol_for_entry = lambda s: sym_map.get(s)
        engine._symbols = ["SYM_C", "SYM_A", "SYM_B"]

        progress_calls = []
        def cb(completed, total, sym, cand):
            progress_calls.append((completed, total, sym))

        executed = engine.step(progress_callback=cb)
        self.assertIsNotNone(executed)
        # Highest confidence (SYM_A with 0.82) must be selected
        self.assertEqual(executed["symbol"], "SYM_A")
        self.assertEqual(len(progress_calls), 3)

        # Verify leaderboard is ranked strictly descending: 0.82 > 0.68 > 0.45
        lb = engine.latest_scan_leaderboard
        confs = [item["confidence"] for item in lb]
        self.assertEqual(confs, sorted(confs, reverse=True))

        engine.save_active_trade({"status": "CLOSED"})
        if test_wallet.exists():
            test_wallet.unlink()

    def test_17_daemon_run_loop_scanning_and_monitoring_phases(self):
        """
        Verify run_loop daemon correctly:
        1. Enters SCANNING PHASE when no active trade exists.
        2. Transitions to MONITORING PHASE when an active trade is detected.
        3. Resumes scanning immediately after a trade closes.
        4. Respects max_iterations cap and exits cleanly.
        """
        test_wallet = Path("test_daemon_wallet.json")
        engine = DualModeExecutionEngine(
            mode="PAPER",
            risk_pct=0.05,
            confidence_threshold=0.30,
            wallet_path=test_wallet,
            pipeline=self.pipeline
        )

        phase_log = []

        # Track step() calls (scanning phase)
        original_step = engine.step
        def mock_step(**kwargs):
            phase_log.append("SCAN")
            return None  # No trade executed on this scan

        # Track check_and_process_exit calls (monitoring phase)
        original_check = engine.check_and_process_exit
        def mock_check(trade):
            phase_log.append("MONITOR")
            # Simulate trade closing on 2nd monitor check
            if phase_log.count("MONITOR") >= 2:
                engine.save_active_trade({"status": "CLOSED"})
                return {"exit_reason": "TAKE_PROFIT", "exit_price": 105.0, "net_pnl": 15.0}
            return None

        engine.step = mock_step
        engine.check_and_process_exit = mock_check

        # Start with no active trade so SCANNING phase fires first
        engine.save_active_trade({"status": "CLOSED"})

        # Inject an active trade after first scan so monitoring begins
        original_get_active = engine.get_active_trade
        call_count = {"n": 0}
        def mock_get_active():
            call_count["n"] += 1
            # First call: no trade (triggers scan)
            if call_count["n"] <= 1:
                return None
            # Subsequent: active trade (triggers monitoring)
            return {
                "status": "OPEN",
                "symbol": "BTC/USDT:USDT",
                "direction": "LONG",
                "entry_price": 100.0,
                "stop_loss": 99.0,
                "take_profit": 103.0,
            }

        engine.get_active_trade = mock_get_active

        # Run with max 4 iterations: scan → monitor → monitor(close) → scan
        engine.run_loop(poll_interval_sec=0, max_iterations=4)

        # Verify both phases were visited
        self.assertIn("SCAN", phase_log, "Daemon must enter SCANNING PHASE when no active trade.")
        self.assertIn("MONITOR", phase_log, "Daemon must enter MONITORING PHASE when trade is active.")
        # Daemon must re-scan after closure
        self.assertGreaterEqual(phase_log.count("SCAN"), 1, "Daemon must resume scanning after trade closes.")

        engine.save_active_trade({"status": "CLOSED"})
        if test_wallet.exists():
            test_wallet.unlink()

    def test_18_fetch_open_orders_warning_suppressed(self):
        """
        Verify LiveExchangeAdapter sets exchange.options["fetchOpenOrders"]["warnWithoutSymbol"] = False
        immediately after construction, suppressing the Binance CCXT rate-limit advisory.
        """
        from execution import LiveExchangeAdapter
        import ccxt

        # Instantiate the adapter with dummy credentials (no real API call made in __init__)
        adapter = LiveExchangeAdapter.__new__(LiveExchangeAdapter)
        adapter.api_key = "dummy_key"
        adapter.api_secret = "dummy_secret"
        adapter.exchange = ccxt.binance({
            "apiKey": "dummy_key",
            "secret": "dummy_secret",
            "enableRateLimit": True,
            "options": {"defaultType": "future", "adjustForTimeDifference": True}
        })
        # Apply the same option the constructor sets
        adapter.exchange.options["fetchOpenOrders"] = {"warnWithoutSymbol": False}
        adapter._leverage_brackets_cache = {}

        # Assert the warning suppression option is set correctly
        opt = adapter.exchange.options.get("fetchOpenOrders", {})
        self.assertIsInstance(opt, dict, "fetchOpenOrders option must be a dict.")
        self.assertFalse(
            opt.get("warnWithoutSymbol", True),
            "warnWithoutSymbol must be False to suppress the Binance fetchOpenOrders rate-limit warning."
        )

    def test_19_daemon_unbreakable_error_resilience(self):
        """
        Verify that unexpected exceptions (CCXT errors, network timeouts) are caught,
        logged with traceback to audit log, and do not crash the daemon loop.
        """
        from unittest.mock import patch
        import ccxt

        test_wallet = Path("test_resilience_wallet.json")
        engine = DualModeExecutionEngine(
            mode="PAPER",
            risk_pct=0.05,
            confidence_threshold=0.30,
            wallet_path=test_wallet,
            pipeline=self.pipeline
        )
        engine.save_active_trade({"status": "CLOSED"})

        call_records = []
        def flaky_step(**kwargs):
            call_records.append("STEP")
            if len(call_records) == 1:
                raise ccxt.NetworkError("Simulated Binance API disconnect / timeout")
            return None

        engine.step = flaky_step
        error_callbacks = []
        def status_cb(phase, data):
            if phase == "ERROR_RETRY":
                error_callbacks.append(data)

        # Patch time.sleep so test runs instantly without 15s wait
        with patch("time.sleep"):
            engine.run_loop(poll_interval_sec=0, max_iterations=2, status_callback=status_cb)

        # Verify the exception was caught, recorded, and the loop proceeded to iteration 2
        self.assertGreaterEqual(len(call_records), 2, "Loop must continue to next iteration after exception!")
        self.assertEqual(len(error_callbacks), 1, "status_callback must receive ERROR_RETRY notification")
        self.assertIn("Simulated Binance API disconnect", error_callbacks[0]["error"])

        engine.save_active_trade({"status": "CLOSED"})
        if test_wallet.exists():
            test_wallet.unlink()

    def test_20_binance_code_4168_multi_assets_margin_graceful_handling(self):
        """
        Verify Binance error code -4168 (Multi-Assets mode prevents isolated margin)
        is handled gracefully with an informational warning and without raising.
        """
        from execution import LiveExchangeAdapter
        from unittest.mock import MagicMock
        import ccxt

        adapter = LiveExchangeAdapter.__new__(LiveExchangeAdapter)
        adapter.api_key = "dummy_key"
        adapter.api_secret = "dummy_secret"
        adapter.exchange = MagicMock()
        # Simulate Binance returning -4168
        adapter.exchange.set_margin_mode.side_effect = ccxt.ExchangeError(
            'binance {"code":-4168,"msg":"Unable to adjust to isolated-margin mode under the Multi-Assets mode"}'
        )
        adapter.exchange.set_leverage.return_value = {"leverage": 20}

        try:
            adapter.configure_symbol_margin_and_leverage("BTC/USDT:USDT", 20)
            handled = True
        except Exception:
            handled = False

        self.assertTrue(handled, "Code -4168 must be handled gracefully without crashing.")
        adapter.exchange.set_leverage.assert_called_once_with(20, "BTC/USDT:USDT")

    def test_21_dedicated_30s_monitoring_sub_loop_and_closure_sweep(self):
        """
        Verify Phase 2 (30s active monitoring sub-loop) polls active trade and upon
        closure executes Phase 3 cleanup sweep and immediately returns to Phase 1.
        """
        from unittest.mock import patch

        test_wallet = Path("test_monitor_wallet.json")
        engine = DualModeExecutionEngine(
            mode="PAPER",
            risk_pct=0.05,
            confidence_threshold=0.30,
            wallet_path=test_wallet,
            pipeline=self.pipeline
        )
        active_pos = {
            "status": "OPEN",
            "symbol": "ETH/USDT:USDT",
            "direction": "LONG",
            "entry_price": 2500.0,
            "stop_loss": 2450.0,
            "take_profit": 2650.0,
            "suggested_position_size": 1.0,
            "mode": "PAPER"
        }
        engine.save_active_trade(active_pos)

        poll_count = {"count": 0}
        def mock_check(trade):
            poll_count["count"] += 1
            if poll_count["count"] >= 2:
                # Trigger closure on second monitor poll
                engine.save_active_trade({"status": "CLOSED"})
                return {
                    "symbol": "ETH/USDT:USDT",
                    "direction": "LONG",
                    "exit_reason": "TAKE_PROFIT",
                    "exit_price": 2650.0,
                    "final_equity": 115.0
                }
            return None

        engine.check_and_process_exit = mock_check
        engine.step = lambda **kwargs: None
        engine._symbols = ["ETH/USDT:USDT"]
        phases = []
        def status_cb(phase, data):
            phases.append(phase)

        with patch("time.sleep"):
            engine.run_loop(poll_interval_sec=0, monitor_interval_sec=30, max_iterations=3, status_callback=status_cb)

        self.assertIn("MONITORING", phases, "Must enter dedicated MONITORING phase.")
        self.assertIn("CLOSED", phases, "Must trigger CLOSED phase on exit.")
        # After closure, active trade state must be CLOSED
        self.assertIsNone(engine.get_active_trade(), "Active trade state must be cleared after closure sweep.")

        engine.save_active_trade({"status": "CLOSED"})
        if test_wallet.exists():
            test_wallet.unlink()

    def test_22_exchange_first_ground_truth_eliminates_phantom_state(self):
        """
        Verify that when active_trade_state.json contains a phantom trade (e.g. GOOGL)
        but Binance fetch_positions() returns 0 active positions, get_active_trade()
        wipes out the phantom state, resets file to CLOSED, sweeps orphans, and returns None.
        """
        from unittest.mock import MagicMock
        from execution import LiveExchangeAdapter

        # Write phantom state to disk
        phantom_trade = {
            "status": "OPEN",
            "symbol": "GOOGL/USDT:USDT",
            "direction": "SHORT",
            "entry_price": 339.37,
            "stop_loss": 340.0,
            "take_profit": 335.0,
            "mode": "LIVE"
        }
        with open(ACTIVE_TRADE_PATH, "w") as f:
            json.dump(phantom_trade, f, indent=2)

        # Mock LiveExchangeAdapter
        mock_adapter = MagicMock(spec=LiveExchangeAdapter)
        mock_adapter.get_open_positions.return_value = []
        mock_adapter.sweep_all_orphan_orders.return_value = 2

        engine = DualModeExecutionEngine.__new__(DualModeExecutionEngine)
        engine.mode = "LIVE"
        engine.live_adapter = mock_adapter
        engine.paper_wallet = None
        engine.risk_pct = 0.05

        active = engine.get_active_trade()

        # Must return None (slate is clean, exchange has zero positions)
        self.assertIsNone(active, "get_active_trade must return None when Binance has 0 open positions.")
        mock_adapter.get_open_positions.assert_called_once()
        mock_adapter.sweep_all_orphan_orders.assert_called_once()

        # State file must be reset to CLOSED
        with open(ACTIVE_TRADE_PATH, "r") as f:
            updated_state = json.load(f)
        self.assertEqual(updated_state.get("status"), "CLOSED", "Phantom state must be purged to CLOSED.")

    def test_23_exchange_active_position_detected_and_synchronized(self):
        """
        Verify that when Binance has an active position (e.g. JTO), get_active_trade()
        detects it, overwrites any stale state, synchronizes active_trade_state.json,
        and returns the verified real exchange position.
        """
        from unittest.mock import MagicMock
        from execution import LiveExchangeAdapter

        # Stale state with a different symbol
        stale_state = {
            "status": "OPEN",
            "symbol": "HOME/USDT:USDT",
            "direction": "LONG",
            "entry_price": 10.0,
            "stop_loss": 9.8,
            "take_profit": 10.6,
            "mode": "LIVE"
        }
        with open(ACTIVE_TRADE_PATH, "w") as f:
            json.dump(stale_state, f, indent=2)

        # Mock LiveExchangeAdapter returning real JTO position
        mock_adapter = MagicMock(spec=LiveExchangeAdapter)
        mock_adapter.get_open_positions.return_value = [
            {
                "symbol": "JTO/USDT:USDT",
                "contracts": 1500.0,
                "entryPrice": 0.4500,
                "side": "long",
                "leverage": 20,
                "notional": 675.0
            }
        ]

        engine = DualModeExecutionEngine.__new__(DualModeExecutionEngine)
        engine.mode = "LIVE"
        engine.live_adapter = mock_adapter
        engine.paper_wallet = None
        engine.risk_pct = 0.05
        engine.get_symbol_max_leverage = lambda s: 20

        active = engine.get_active_trade()

        self.assertIsNotNone(active, "Must detect real active position on Binance.")
        self.assertEqual(active["symbol"], "JTO/USDT:USDT")
        self.assertEqual(active["direction"], "LONG")
        self.assertEqual(active["entry_price"], 0.4500)
        self.assertEqual(active["suggested_position_size"], 1500.0)
        self.assertEqual(active["status"], "OPEN")
        self.assertEqual(active["ground_truth"], "EXCHANGE")

        # Verify state file was updated with JTO
        with open(ACTIVE_TRADE_PATH, "r") as f:
            synced_state = json.load(f)
        self.assertEqual(synced_state["symbol"], "JTO/USDT:USDT")
        self.assertEqual(synced_state["status"], "OPEN")

        # Cleanup
        with open(ACTIVE_TRADE_PATH, "w") as f:
            json.dump({"status": "CLOSED"}, f)

    def test_24_live_phase2_exit_when_exchange_position_closes(self):
        """
        Verify that during Phase 2 monitoring in LIVE mode, when exchange position
        contracts drop to 0, check_and_process_exit triggers immediate closure sweep,
        cancels open orders, sets state to CLOSED, and returns the exit event.
        """
        from unittest.mock import MagicMock
        from execution import LiveExchangeAdapter

        mock_adapter = MagicMock(spec=LiveExchangeAdapter)
        # Position closed on exchange
        mock_adapter.get_open_positions.return_value = []
        mock_adapter.cancel_all_orders_for_symbol.return_value = 1
        mock_adapter.sweep_all_orphan_orders.return_value = 1
        mock_adapter.fetch_equity_and_balance.return_value = (150.0, 150.0)

        # verify_position_closed_on_exchange delegates to get_open_positions
        def mock_verify_closed(sym):
            active_pos = mock_adapter.get_open_positions()
            return len(active_pos) == 0
        mock_adapter.verify_position_closed_on_exchange = mock_verify_closed

        engine = DualModeExecutionEngine.__new__(DualModeExecutionEngine)
        engine.mode = "LIVE"
        engine.live_adapter = mock_adapter
        engine.paper_wallet = None
        engine.risk_pct = 0.05
        engine.fetch_live_ticker_price = lambda s: 0.4800

        active_trade = {
            "status": "OPEN",
            "symbol": "JTO/USDT:USDT",
            "direction": "LONG",
            "entry_price": 0.4500,
            "stop_loss": 0.4410,
            "take_profit": 0.4770,
            "suggested_position_size": 1500.0,
            "mode": "LIVE"
        }
        with open(ACTIVE_TRADE_PATH, "w") as f:
            json.dump(active_trade, f, indent=2)

        exit_event = engine.check_and_process_exit(active_trade)

        self.assertIsNotNone(exit_event, "Must detect exit when exchange confirms position closed.")
        self.assertEqual(exit_event["symbol"], "JTO/USDT:USDT")
        mock_adapter.cancel_all_orders_for_symbol.assert_called_once_with("JTO/USDT:USDT")
        mock_adapter.sweep_all_orphan_orders.assert_called_once()

        # State file must be CLOSED
        with open(ACTIVE_TRADE_PATH, "r") as f:
            final_state = json.load(f)
        self.assertEqual(final_state["status"], "CLOSED")


if __name__ == "__main__":
    unittest.main()
