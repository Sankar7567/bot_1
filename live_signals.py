"""
Decoupled Live Signal Generator & Audit Logger.

Features:
- Real-time candle polling via CCXT for 500+ active Binance Futures symbols.
- 1-minute bandwidth optimization: snapshots market universe via fetch_tickers() and
  threads candle ingestion across high-volume candidate pairs.
- Incremental multi-timeframe feature calculation and LightGBM model inference.
- Bidirectional signals (LONG and SHORT) enforcing strict 1:3 Risk-Reward
  (SL = 1.0 * ATR, TP = 3.0 * ATR).
- Atomic writing to signals/latest_signal.json ensuring readers never encounter partial files.
- Global Single Active Trade state persistence (signals/active_trade_state.json).
- Comprehensive structured audit logging appended to logs/platform_audit.log.
"""

import os
import json
import time
import logging
from typing import List, Dict, Optional, Any
from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import numpy as np

from data_pipeline import DataPipeline, sanitize_symbol_for_filename
from features import FeatureEngineer
from model import ModelTrainer
from trading_rules import calculate_bracket_prices, market_tick_size, validate_rr_multipliers

logger = logging.getLogger("LiveSignals")

SIGNALS_DIR = Path("signals")
LOGS_DIR = Path("logs")
SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

LATEST_SIGNAL_PATH = SIGNALS_DIR / "latest_signal.json"
ACTIVE_TRADE_PATH = SIGNALS_DIR / "active_trade_state.json"
AUDIT_LOG_PATH = LOGS_DIR / "platform_audit.log"


def append_audit_log(event_type: str, message: str, data: Optional[Dict[str, Any]] = None, level: str = "INFO") -> None:
    """Appends an event to logs/platform_audit.log and outputs to logger."""
    now_iso = datetime.now(timezone.utc).isoformat()
    entry = {
        "timestamp": now_iso,
        "event_type": event_type,
        "message": message,
        "data": data or {}
    }
    with open(AUDIT_LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")
    if level == "ERROR":
        logger.error(f"[{event_type}] {message}")
    elif level == "WARNING":
        logger.warning(f"[{event_type}] {message}")
    else:
        logger.info(f"[{event_type}] {message}")


class LiveSignalGenerator:
    def __init__(
        self,
        symbols: Optional[List[str]] = None,
        base_timeframe: str = "5m",
        htf_timeframes: Optional[List[str]] = None,
        confidence_threshold: float = 0.35,
        risk_pct: float = 0.03,
        account_equity: float = 100.0,
        max_leverage: float = 10.0,
        sl_atr_mult: float = 1.0,
        tp_atr_mult: float = 3.0,
        pipeline: Optional[DataPipeline] = None,
        model_version: str = "v1"
    ):
        self.pipeline = pipeline or DataPipeline()
        self.fe = FeatureEngineer(self.pipeline)
        self.model_version = model_version
        self.trainer = ModelTrainer(pipeline=self.pipeline, model_version=model_version)
        self.trainer.load_model(version=model_version)

        self._symbols = symbols
        self.base_timeframe = base_timeframe
        self.htf_timeframes = htf_timeframes or ["15m", "1h"]
        self.confidence_threshold = confidence_threshold
        self.risk_pct = risk_pct
        self.account_equity = account_equity
        self.max_leverage = max_leverage
        self.sl_atr_mult = sl_atr_mult
        self.tp_atr_mult = tp_atr_mult
        validate_rr_multipliers(self.sl_atr_mult, self.tp_atr_mult)

    @property
    def symbols(self) -> List[str]:
        """Resolves target symbol list, pulling top Binance Futures if not specified."""
        if not self._symbols:
            top_syms = self.pipeline.get_top_futures_symbols(limit=500)
            self._symbols = top_syms if top_syms else ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]
        return self._symbols

    def get_active_trade(self) -> Optional[Dict[str, Any]]:
        """Reads current global active trade state from disk."""
        if not ACTIVE_TRADE_PATH.exists():
            return None
        try:
            with open(ACTIVE_TRADE_PATH, "r") as f:
                state = json.load(f)
                if state.get("status") == "OPEN":
                    return state
        except Exception as e:
            logger.error(f"Error reading active trade state: {e}")
        return None

    def save_active_trade(self, trade: Optional[Dict[str, Any]]) -> None:
        """Persists or clears global active trade state on disk."""
        try:
            tmp_path = ACTIVE_TRADE_PATH.with_suffix(".tmp")
            with open(tmp_path, "w") as f:
                json.dump(trade or {"status": "CLOSED"}, f, indent=2)
            tmp_path.replace(ACTIVE_TRADE_PATH)
        except Exception as e:
            logger.error(f"Error saving active trade state: {e}")

    def fetch_recent_candles(self, symbol: str, timeframe: str, limit: int = 100) -> pd.DataFrame:
        """Fetches latest candles via CCXT or local storage cache."""
        try:
            candles = self.pipeline.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            if candles:
                df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
                for col in ["open", "high", "low", "close", "volume"]:
                    df[col] = df[col].astype(float)
                return df
        except Exception as e:
            logger.warning(f"Live fetch failed for {symbol} [{timeframe}], falling back to local cache: {e}")
            df = self.pipeline.load_symbol_data(timeframe, symbol)
            if not df.empty:
                return df.tail(limit)
        return pd.DataFrame()

    def check_active_trade_exit(self, active_trade: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Monitors active trade against incoming market prices for TP/SL breach.
        """
        sym = active_trade["symbol"]
        df_latest = self.fetch_recent_candles(sym, self.base_timeframe, limit=5)
        if df_latest.empty:
            return None

        curr_bar = df_latest.iloc[-1]
        c_high = curr_bar["high"]
        c_low = curr_bar["low"]
        curr_time = curr_bar["timestamp"].isoformat()

        direction = active_trade.get("direction", "LONG")
        tp_price = active_trade["take_profit"]
        sl_price = active_trade["stop_loss"]
        entry_price = active_trade["entry_price"]
        pos_size = active_trade["suggested_position_size"]

        if direction == "LONG":
            tp_hit = c_high >= tp_price
            sl_hit = c_low <= sl_price
            exit_price = tp_price if tp_hit else sl_price
            pnl = (exit_price - entry_price) * pos_size if (tp_hit or sl_hit) else 0.0
        else:  # SHORT
            tp_hit = c_low <= tp_price
            sl_hit = c_high >= sl_price
            exit_price = tp_price if tp_hit else sl_price
            pnl = (entry_price - exit_price) * pos_size if (tp_hit or sl_hit) else 0.0

        if tp_hit or sl_hit:
            reason = "TAKE_PROFIT" if tp_hit else "STOP_LOSS"
            self.account_equity += pnl

            exit_event = {
                "symbol": sym,
                "direction": direction,
                "exit_reason": reason,
                "exit_price": float(round(exit_price, 4)),
                "exit_time": curr_time,
                "realized_pnl": float(round(pnl, 2)),
                "new_equity": float(round(self.account_equity, 2))
            }
            append_audit_log("TRADE_CLOSED", f"Position closed on {sym} ({direction}): {reason}", exit_event)
            self.save_active_trade({"status": "CLOSED", "last_closed": exit_event})
            return exit_event

        return None

    def _evaluate_single_symbol(self, sym: str) -> Optional[Dict[str, Any]]:
        """Computes live indicators and runs model inference on a single symbol."""
        try:
            df_base = self.fetch_recent_candles(sym, self.base_timeframe, limit=120)
            if df_base.empty or len(df_base) < 30:
                return None

            base_feats = self.fe.compute_single_timeframe_features(df_base)
            if base_feats.empty:
                return None

            merged = df_base.merge(base_feats, on="timestamp", how="inner")

            for htf in self.htf_timeframes:
                df_htf = self.fetch_recent_candles(sym, htf, limit=60)
                if not df_htf.empty and len(df_htf) >= 20:
                    htf_feats = self.fe.compute_single_timeframe_features(df_htf, prefix=f"htf_{htf}_")
                    if not htf_feats.empty:
                        dur_ms = 15 * 60 * 1000 if htf == "15m" else 60 * 60 * 1000
                        htf_feats["avail_ts"] = htf_feats["timestamp"] + pd.to_timedelta(dur_ms, unit="ms")
                        merged = pd.merge_asof(
                            merged.sort_values("timestamp"),
                            htf_feats.drop(columns=["timestamp"]).sort_values("avail_ts"),
                            left_on="timestamp",
                            right_on="avail_ts",
                            direction="backward"
                        ).drop(columns=["avail_ts"])

            merged = merged.ffill().dropna().reset_index(drop=True)
            if merged.empty:
                return None

            if self.trainer.booster is not None or self.trainer.model is not None:
                probs = self.trainer.predict_probabilities(merged)
                direction, confidence = self.trainer.predict_directional_signal(probs[-1])
                if direction not in ("LONG", "SHORT"):
                    return None
            else:
                rsi = merged.iloc[-1].get("rsi_14", 50)
                prob_tp = 0.40 if rsi > 50 else 0.20
                prob_sl = 0.40 if rsi <= 50 else 0.20
                if prob_tp >= prob_sl:
                    direction, confidence = "LONG", prob_tp
                else:
                    direction, confidence = "SHORT", prob_sl

            return {
                "symbol": sym,
                "bar": merged.iloc[-1],
                "direction": direction,
                "confidence": confidence
            }
        except Exception as e:
            append_audit_log("SYMBOL_ERROR", f"Error evaluating {sym}: {e}", level="ERROR")
            return None

    def scan_universe_and_emit_signal(self) -> Optional[Dict[str, Any]]:
        """
        Evaluates active symbols, runs model inference, and emits a structured JSON signal
        if a high-conviction trade is identified and no global position is open.
        """
        active_trade = self.get_active_trade()

        # 1. If trade is already active globally, check for TP/SL exit
        if active_trade is not None:
            exit_event = self.check_active_trade_exit(active_trade)
            if exit_event is None:
                append_audit_log("SCAN_SKIPPED", f"Active trade in progress on {active_trade['symbol']}. Global scan locked.")
                return None

        # 2. No active trade: evaluate candidate symbols
        target_symbols = self.symbols
        append_audit_log("SCAN_STARTED", f"Scanning {len(target_symbols)} symbols for high-conviction setups...")

        best_candidate = None
        best_confidence = -1.0

        # Fast threaded candidate evaluation
        max_workers = min(10, len(target_symbols))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_sym = {executor.submit(self._evaluate_single_symbol, s): s for s in target_symbols}
            for future in as_completed(future_to_sym):
                cand = future.result()
                if cand is not None:
                    if cand["confidence"] >= self.confidence_threshold and cand["confidence"] > best_confidence:
                        best_confidence = cand["confidence"]
                        best_candidate = cand

        if best_candidate is None:
            append_audit_log("NO_SIGNAL", "No symbol exceeded the confidence threshold.")
            return None

        # 3. High-conviction setup found: Generate Signal Payload
        sym = best_candidate["symbol"]
        bar = best_candidate["bar"]
        direction = best_candidate["direction"]
        entry_price = float(bar["close"])
        atr_val = float(bar.get("atr_14", entry_price * 0.01))
        if atr_val <= 0 or np.isnan(atr_val):
            atr_val = entry_price * 0.01

        try:
            market = self.pipeline.exchange.market(sym)
            tick_size = market_tick_size(
                market, getattr(self.pipeline.exchange, "precisionMode", None)
            )
        except Exception:
            tick_size = None
        bracket_entry, stop_loss, take_profit, sl_dist = calculate_bracket_prices(
            entry_price,
            self.sl_atr_mult * atr_val,
            direction,
            tick_size=tick_size,
        )

        dollar_risk = self.account_equity * self.risk_pct
        suggested_size = dollar_risk / sl_dist
        pos_notional = suggested_size * bracket_entry

        # Cap leverage
        max_notional = self.account_equity * self.max_leverage
        if pos_notional > max_notional:
            suggested_size = max_notional / bracket_entry

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        signal_payload = {
            "timestamp": now_iso,
            "symbol": sym,
            "direction": direction,
            "confidence": float(round(best_candidate["confidence"], 4)),
            "entry_price": float(bracket_entry),
            "bracket_reference_price": float(bracket_entry),
            "stop_loss": float(stop_loss),
            "take_profit": float(take_profit),
            "risk_pct": float(round(self.risk_pct * 100.0, 1)),
            "suggested_position_size": float(round(suggested_size, 4)),
            "status": "SIGNAL_GENERATED"
        }

        # Atomic write to signals/latest_signal.json
        tmp_signal_path = LATEST_SIGNAL_PATH.with_suffix(".tmp")
        with open(tmp_signal_path, "w") as f:
            json.dump(signal_payload, f, indent=2)
        tmp_signal_path.replace(LATEST_SIGNAL_PATH)

        # Mark active trade state to enforce global single trade
        active_trade_state = dict(signal_payload)
        active_trade_state["status"] = "OPEN"
        self.save_active_trade(active_trade_state)

        append_audit_log("SIGNAL_EMITTED", f"High-conviction signal emitted for {sym} ({direction})", signal_payload)
        return signal_payload

    def run_polling_loop(self, poll_interval_sec: int = 10, max_iterations: Optional[int] = None) -> None:
        """Continuous live signal polling daemon."""
        append_audit_log("DAEMON_STARTED", f"Live signal daemon started (Interval: {poll_interval_sec}s)")
        iterations = 0
        try:
            while True:
                self.scan_universe_and_emit_signal()
                iterations += 1
                if max_iterations and iterations >= max_iterations:
                    break
                time.sleep(poll_interval_sec)
        except KeyboardInterrupt:
            append_audit_log("DAEMON_STOPPED", "Live signal daemon stopped by user.")
