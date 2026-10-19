"""
Global Multi-Symbol Universe Scanner & Global One-Trade-At-A-Time Engine.

Features:
- Global scan across 500+ Binance Futures symbols simultaneously across time.
- Strict Global Single Position Invariance: At any given moment, exactly ONE trade can be open
  across the entire universe.
- At each timestamp, if no position is open, the engine evaluates model confidence scores (P(TP))
  across all candidate symbols and enters the highest-conviction setup (P(TP) >= threshold).
- Enforces strict 1:3 Risk-Reward (SL = 1.0 * ATR, TP = 3.0 * ATR).
- Compounding equity from $100 starting balance with dynamic ATR risk sizing (e.g. 2%, 3%, 5%).
- Generates global portfolio equity curves, drawdown profiles, and symbol distribution stats.
"""

import logging
from typing import List, Dict, Optional, Tuple, Any
from pathlib import Path
from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

from data_pipeline import DataPipeline, desanitize_filename_to_symbol
from features import FeatureEngineer
from model import ModelTrainer
from backtester import Trade
from trading_rules import directional_probability_scores, validate_rr_multipliers

logger = logging.getLogger("GlobalUniverseScanner")


class GlobalUniverseScanner:
    def __init__(
        self,
        initial_capital: float = 100.0,
        risk_per_trade: float = 0.03,
        max_leverage: float = 10.0,
        maker_fee: float = 0.0002,
        taker_fee: float = 0.0005,
        funding_rate_8h: float = 0.0001,
        slippage_bps: float = 1.0,
        pipeline: Optional[DataPipeline] = None,
        model_version: str = "v1"
    ):
        self.initial_capital = initial_capital
        self.risk_per_trade = risk_per_trade
        self.max_leverage = max_leverage
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee
        self.funding_rate_8h = funding_rate_8h
        self.slippage = slippage_bps / 10000.0
        self.pipeline = pipeline or DataPipeline()
        self.model_version = model_version

    @staticmethod
    def _with_directional_scores(
        df: pd.DataFrame, trainer: ModelTrainer, force_refresh: bool = False
    ) -> pd.DataFrame:
        """Return a frame with a fresh, matched LONG/SHORT score pair."""
        if not force_refresh and "prob_long" in df.columns and "prob_short" in df.columns:
            return df

        scored = df.copy()
        try:
            probs = trainer.predict_probabilities(scored)
            scores = [
                directional_probability_scores(
                    probability, allow_short_score=trainer.supports_short_trades
                )
                for probability in probs
            ]
            scored["prob_long"] = [long_score for long_score, _ in scores]
            scored["prob_short"] = [short_score for _, short_score in scores]
        except Exception:
            bullish = scored.get("trend_bullish", pd.Series(0, index=scored.index)) == 1
            bearish = scored.get("trend_bearish", pd.Series(0, index=scored.index)) == 1
            rsi = scored.get("rsi_14", pd.Series(50, index=scored.index))
            macd_hist = scored.get("macd_hist", pd.Series(0, index=scored.index))
            scored["prob_long"] = (bullish & (rsi > 50) & (macd_hist > 0)).astype(float) * 0.45
            scored["prob_short"] = (bearish & (rsi < 50) & (macd_hist < 0)).astype(float) * 0.45
        return scored

    def load_universe_feature_datasets(
        self,
        timeframe: str = "5m",
        symbols: Optional[List[str]] = None,
        max_symbols: int = 500
    ) -> Dict[str, pd.DataFrame]:
        """
        Loads cached multi-timeframe feature Parquet datasets for the universe.
        """
        fe = FeatureEngineer(self.pipeline)
        feat_dir = self.pipeline.features_dir / timeframe
        
        if not feat_dir.exists():
            return {}

        feature_files = list(feat_dir.glob("*.parquet"))
        universe_dfs: Dict[str, pd.DataFrame] = {}

        trainer = ModelTrainer(pipeline=self.pipeline, model_version=self.model_version)
        trainer.load_model(version=self.model_version)

        for f in feature_files[:max_symbols]:
            sym = desanitize_filename_to_symbol(f.name)
            if symbols and sym not in symbols:
                continue

            try:
                df = pd.read_parquet(f)
                if len(df) < 50:
                    continue

                df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
                df = df.sort_values("timestamp").reset_index(drop=True)

                # Always refresh the pair together. A cached legacy prob_sl is
                # not a short score and cannot be mixed with fresh inference.
                df = self._with_directional_scores(df, trainer, force_refresh=True)

                universe_dfs[sym] = df
            except Exception as e:
                logger.error(f"Error loading universe features for {sym}: {e}")

        logger.info(f"Loaded {len(universe_dfs)} symbol datasets for global multi-symbol scan.")
        return universe_dfs

    def run_global_backtest(
        self,
        universe_dfs: Optional[Dict[str, pd.DataFrame]] = None,
        timeframe: str = "5m",
        symbols: Optional[List[str]] = None,
        confidence_threshold: float = 0.35,
        risk_per_trade: Optional[float] = None,
        sl_atr_mult: float = 1.0,
        tp_atr_mult: float = 3.0,
        max_holding_bars: int = 24
    ) -> Dict[str, Any]:
        """
        Simulates simultaneous universe scanning across 500+ symbols.
        Strictly executes ONE trade at a time globally on the highest-conviction setup.
        """
        if universe_dfs is None:
            universe_dfs = self.load_universe_feature_datasets(timeframe=timeframe, symbols=symbols)

        if not universe_dfs:
            return {"error": "No universe feature datasets available for global scan."}

        validate_rr_multipliers(sl_atr_mult, tp_atr_mult)

        target_risk = risk_per_trade if risk_per_trade is not None else self.risk_per_trade

        # Fast timestamp indexing across all symbols
        symbol_lookups: Dict[str, Dict[pd.Timestamp, Dict[str, Any]]] = {}
        all_timestamps_set = set()
        trainer = None

        for sym, df in universe_dfs.items():
            if "prob_long" not in df.columns or "prob_short" not in df.columns:
                if trainer is None:
                    trainer = ModelTrainer(pipeline=self.pipeline, model_version=self.model_version)
                    trainer.load_model(version=self.model_version)
                df = self._with_directional_scores(df, trainer)
            ts_series = df["timestamp"].tolist()
            all_timestamps_set.update(ts_series)
            
            # Map timestamp -> row dict for fast O(1) access
            records = df[[
                "timestamp", "open", "high", "low", "close", "atr_14", "prob_long", "prob_short"
            ]].to_dict("records")
            symbol_lookups[sym] = {r["timestamp"]: r for r in records}

        # Chronologically sorted global timeline
        timeline = sorted(list(all_timestamps_set))
        logger.info(f"Global universe scan initiated across {len(timeline)} timestamps and {len(universe_dfs)} symbols.")

        equity = float(self.initial_capital)
        trades: List[Trade] = []
        equity_curve = []

        # Strict Global Single Trade State
        active_trade = None
        trade_counter = 0

        for t_idx, current_time in enumerate(timeline):
            # 1. Evaluate intra-candle exit if a global trade is active
            if active_trade is not None:
                sym = active_trade["symbol"]
                bar = symbol_lookups.get(sym, {}).get(current_time)

                if bar is not None:
                    holding_bars = t_idx - active_trade["entry_timeline_idx"]
                    c_open = bar["open"]
                    c_high = bar["high"]
                    c_low = bar["low"]
                    c_close = bar["close"]

                    tp_price = active_trade["tp_price"]
                    sl_price = active_trade["sl_price"]
                    entry_price = active_trade["entry_price"]
                    pos_size = active_trade["pos_size"]
                    pos_value = active_trade["pos_value"]
                    risk_dollars = active_trade["risk_dollars"]

                    direction = active_trade["direction"]
                    if direction == "LONG":
                        tp_hit = c_high >= tp_price
                        sl_hit = c_low <= sl_price
                    else:
                        tp_hit = c_low <= tp_price
                        sl_hit = c_high >= sl_price
                    time_expired = holding_bars >= max_holding_bars

                    trade_closed = False
                    exit_price = 0.0
                    exit_reason = ""

                    if tp_hit and not sl_hit:
                        trade_closed = True
                        exit_price = tp_price * (1.0 - self.slippage if direction == "LONG" else 1.0 + self.slippage)
                        exit_reason = "TAKE_PROFIT"
                    elif sl_hit and not tp_hit:
                        trade_closed = True
                        exit_price = sl_price * (1.0 - self.slippage if direction == "LONG" else 1.0 + self.slippage)
                        exit_reason = "STOP_LOSS"
                    elif tp_hit and sl_hit:
                        trade_closed = True
                        dist_to_sl = abs(c_open - sl_price)
                        dist_to_tp = abs(c_open - tp_price)
                        if dist_to_sl <= dist_to_tp:
                            exit_price = sl_price * (1.0 - self.slippage if direction == "LONG" else 1.0 + self.slippage)
                            exit_reason = "STOP_LOSS"
                        else:
                            exit_price = tp_price * (1.0 - self.slippage if direction == "LONG" else 1.0 + self.slippage)
                            exit_reason = "TAKE_PROFIT"
                    elif time_expired:
                        trade_closed = True
                        exit_price = c_close * (1.0 - self.slippage if direction == "LONG" else 1.0 + self.slippage)
                        exit_reason = "TIME_EXPIRY"

                    if trade_closed:
                        signed_move = exit_price - entry_price
                        if direction == "SHORT":
                            signed_move = -signed_move
                        gross_pnl = signed_move * pos_size
                        exit_notional = exit_price * pos_size
                        fees = (pos_value * self.taker_fee) + (exit_notional * self.maker_fee)

                        duration_hours = max(0.08, (current_time - active_trade["entry_time"]).total_seconds() / 3600.0)
                        funding_cost = pos_value * self.funding_rate_8h * (duration_hours / 8.0)

                        net_pnl = gross_pnl - fees - funding_cost
                        equity += net_pnl
                        return_pct = (net_pnl / pos_value) * 100.0 if pos_value > 0 else 0.0
                        trade_counter += 1

                        trades.append(Trade(
                            trade_id=trade_counter,
                            symbol=sym,
                            entry_time=active_trade["entry_time"],
                            exit_time=current_time,
                            side=direction,
                            entry_price=float(entry_price),
                            exit_price=float(exit_price),
                            tp_price=float(tp_price),
                            sl_price=float(sl_price),
                            exit_reason=exit_reason,
                            holding_bars=int(holding_bars),
                            pos_size=float(pos_size),
                            pos_value=float(pos_value),
                            risk_dollars=float(risk_dollars),
                            gross_pnl=float(gross_pnl),
                            fees=float(fees),
                            funding_cost=float(funding_cost),
                            net_pnl=float(net_pnl),
                            return_pct=float(return_pct),
                            equity_after=float(equity)
                        ))
                        active_trade = None

            # 2. Strict Global Single Trade Invariance:
            # If NO trade is open globally, scan all symbols at this timestamp for highest conviction
            if active_trade is None and t_idx < len(timeline) - 1 and equity > 5.0:
                best_symbol = None
                best_confidence = -1.0
                best_bar = None

                for sym, lookup in symbol_lookups.items():
                    bar = lookup.get(current_time)
                    if bar is not None:
                        long_conf = float(bar.get("prob_long", 0.0) or 0.0)
                        short_conf = float(bar.get("prob_short", 0.0) or 0.0)
                        conf = max(long_conf, short_conf)
                        if conf >= confidence_threshold and conf > best_confidence:
                            best_confidence = conf
                            best_symbol = sym
                            best_bar = bar

                # Open strictly ONE trade globally on highest conviction setup
                if best_symbol is not None and best_bar is not None:
                    c_close = best_bar["close"]
                    atr_val = best_bar.get("atr_14", c_close * 0.01)
                    if atr_val <= 0 or np.isnan(atr_val):
                        atr_val = c_close * 0.01

                    direction = "LONG" if float(best_bar.get("prob_long", 0.0) or 0.0) >= float(best_bar.get("prob_short", 0.0) or 0.0) else "SHORT"
                    direction_sign = 1.0 if direction == "LONG" else -1.0
                    entry_price = c_close * (1.0 + direction_sign * self.slippage)
                    sl_dist = sl_atr_mult * atr_val
                    tp_dist = 3.0 * sl_dist
                    if direction == "LONG":
                        sl_price = entry_price - sl_dist
                        tp_price = entry_price + tp_dist
                    else:
                        if entry_price <= tp_dist:
                            continue
                        sl_price = entry_price + sl_dist
                        tp_price = entry_price - tp_dist

                    dollar_risk = equity * target_risk
                    pos_size = dollar_risk / sl_dist
                    pos_notional = pos_size * entry_price

                    max_notional = equity * self.max_leverage
                    if pos_notional > max_notional:
                        pos_notional = max_notional
                        pos_size = pos_notional / entry_price
                        dollar_risk = pos_size * sl_dist

                    active_trade = {
                        "symbol": best_symbol,
                        "entry_timeline_idx": t_idx,
                        "entry_time": current_time,
                        "entry_price": entry_price,
                        "direction": direction,
                        "tp_price": tp_price,
                        "sl_price": sl_price,
                        "pos_size": pos_size,
                        "pos_value": pos_notional,
                        "risk_dollars": dollar_risk,
                        "confidence": best_confidence
                    }

            # Global Mark-to-market equity point
            if active_trade is not None:
                sym = active_trade["symbol"]
                curr_bar = symbol_lookups.get(sym, {}).get(current_time)
                if curr_bar is not None:
                    direction_sign = 1.0 if active_trade["direction"] == "LONG" else -1.0
                    unrealized = direction_sign * (curr_bar["close"] - active_trade["entry_price"]) * active_trade["pos_size"]
                    curr_eq = max(0.0, equity + unrealized)
                else:
                    curr_eq = equity
            else:
                curr_eq = equity

            equity_curve.append({
                "timestamp": current_time,
                "equity": float(curr_eq),
                "active_symbol": active_trade["symbol"] if active_trade else "NONE"
            })

        trades_df = pd.DataFrame([asdict(t) for t in trades])
        equity_df = pd.DataFrame(equity_curve)

        metrics = self._calculate_global_metrics(trades_df, equity_df, target_risk)
        symbol_stats = {}
        if not trades_df.empty:
            symbol_stats = trades_df["symbol"].value_counts().to_dict()

        return {
            "metrics": metrics,
            "trades": trades_df,
            "equity_curve": equity_df,
            "symbol_trade_distribution": symbol_stats
        }

    def _calculate_global_metrics(
        self,
        trades_df: pd.DataFrame,
        equity_df: pd.DataFrame,
        risk_pct: float
    ) -> Dict[str, Any]:
        """Calculates global multi-symbol execution metrics."""
        if trades_df.empty or equity_df.empty:
            return {
                "initial_capital": self.initial_capital,
                "final_equity": self.initial_capital,
                "total_trades": 0,
                "win_rate_pct": 0.0,
                "profit_factor": 0.0,
                "compounding_return_pct": 0.0,
                "max_drawdown_pct": 0.0,
                "sharpe_ratio": 0.0,
                "sortino_ratio": 0.0,
                "calmar_ratio": 0.0,
                "net_profit": 0.0,
                "total_fees": 0.0,
                "total_funding": 0.0
            }

        initial_eq = self.initial_capital
        final_eq = float(equity_df["equity"].iloc[-1])
        compounding_return_pct = ((final_eq - initial_eq) / initial_eq) * 100.0

        equity_series = equity_df["equity"]
        peak = equity_series.cummax()
        drawdown = (equity_series - peak) / peak.replace(0, 1.0)
        max_drawdown_pct = abs(float(drawdown.min())) * 100.0
        equity_df["drawdown"] = drawdown

        winning = trades_df[trades_df["net_pnl"] > 0]
        losing = trades_df[trades_df["net_pnl"] <= 0]
        win_rate = (len(winning) / len(trades_df)) * 100.0

        gross_profit = float(winning["net_pnl"].sum())
        gross_loss = abs(float(losing["net_pnl"].sum()))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0)

        pct_returns = equity_series.pct_change().dropna()
        if len(pct_returns) > 1 and pct_returns.std() > 0:
            sharpe = float((pct_returns.mean() / pct_returns.std()) * np.sqrt(105120))
            neg = pct_returns[pct_returns < 0]
            downside_std = neg.std() if len(neg) > 0 else 1e-6
            sortino = float((pct_returns.mean() / downside_std) * np.sqrt(105120))
        else:
            sharpe = 0.0
            sortino = 0.0

        calmar = (compounding_return_pct / max_drawdown_pct) if max_drawdown_pct > 0 else 0.0
        exit_counts = trades_df["exit_reason"].value_counts().to_dict()

        return {
            "initial_capital": float(round(initial_eq, 2)),
            "final_equity": float(round(final_eq, 2)),
            "total_trades": int(len(trades_df)),
            "win_rate_pct": float(round(win_rate, 2)),
            "profit_factor": float(round(profit_factor, 2)),
            "compounding_return_pct": float(round(compounding_return_pct, 2)),
            "max_drawdown_pct": float(round(max_drawdown_pct, 2)),
            "sharpe_ratio": float(round(sharpe, 2)),
            "sortino_ratio": float(round(sortino, 2)),
            "calmar_ratio": float(round(calmar, 2)),
            "net_profit": float(round(trades_df["net_pnl"].sum(), 2)),
            "total_fees": float(round(trades_df["fees"].sum(), 2)),
            "total_funding": float(round(trades_df["funding_cost"].sum(), 2)),
            "tp_hits": int(exit_counts.get("TAKE_PROFIT", 0)),
            "sl_hits": int(exit_counts.get("STOP_LOSS", 0)),
            "time_exits": int(exit_counts.get("TIME_EXPIRY", 0)),
            "avg_holding_bars": float(round(trades_df["holding_bars"].mean(), 1)) if len(trades_df) > 0 else 0.0
        }
