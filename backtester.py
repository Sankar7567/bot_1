"""
Backtesting & Risk Execution Engine for Binance Futures ML Strategy.

Features:
1. Strict One Trade at a Time: No concurrent trades until active position exits.
2. Compounding Equity & Dynamic ATR Risk Sizing:
   - Account risk per trade (e.g. 2%, 3%, 5% of compounding equity).
   - Position size = (Equity * Risk_Pct) / (1.0 * ATR), strictly enforcing 1:3 RR.
   - Max leverage cap prevents overleveraging on abnormal low volatility.
3. Intra-Candle Precision Validation: Bar-by-bar high/low inspection against dynamic ATR
   barriers (1:3 Risk-Reward). Resolves same-candle breaches conservatively based on open price.
4. Realistic Friction: Deducts Binance Futures taker/maker fees on entry and exit,
   and applies continuous 8-hour perpetual funding rate costs prorated over holding time.
5. Risk Portfolio Optimizer: Automatically backtests and compares 2%, 3%, and 5% account risk
   to identify the optimal risk-adjusted configuration.
"""

import logging
from typing import List, Dict, Optional, Tuple, Any
from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

from model import ModelTrainer
from trading_rules import directional_probability_scores, validate_rr_multipliers

logger = logging.getLogger("Backtester")


@dataclass
class Trade:
    trade_id: int
    symbol: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    side: str
    entry_price: float
    exit_price: float
    tp_price: float
    sl_price: float
    exit_reason: str
    holding_bars: int
    pos_size: float
    pos_value: float
    risk_dollars: float
    gross_pnl: float
    fees: float
    funding_cost: float
    net_pnl: float
    return_pct: float
    equity_after: float


class BacktestEngine:
    def __init__(
        self,
        initial_capital: float = 100.0,    # $100 starting balance as requested
        risk_per_trade: float = 0.03,      # 3% account risk default (2%, 3%, 5% selectable)
        max_leverage: float = 10.0,        # Maximum allowable notional leverage
        maker_fee: float = 0.0002,         # 0.02% maker fee
        taker_fee: float = 0.0005,         # 0.05% taker fee
        entry_is_taker: bool = True,
        exit_is_taker: bool = False,       # Limit TP/SL orders act as maker if rested
        funding_rate_8h: float = 0.0001,   # 0.01% per 8 hours
        slippage_bps: float = 1.0          # 1 basis point slippage
    ):
        self.initial_capital = initial_capital
        self.risk_per_trade = risk_per_trade
        self.max_leverage = max_leverage
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee
        self.entry_is_taker = entry_is_taker
        self.exit_is_taker = exit_is_taker
        self.funding_rate_8h = funding_rate_8h
        self.slippage = slippage_bps / 10000.0

    def run_backtest(
        self,
        df: pd.DataFrame,
        symbol: str = "BTC/USDT:USDT",
        sl_atr_mult: float = 1.0,
        tp_atr_mult: float = 3.0,
        max_holding_bars: int = 24,
        confidence_threshold: float = 0.35,
        risk_per_trade: Optional[float] = None,
        model_trainer: Optional[ModelTrainer] = None
    ) -> Dict[str, Any]:
        """
        Executes strict one-trade-at-a-time backtest with compounding equity and ATR risk sizing.
        """
        if df.empty or len(df) < 50:
            return {"error": "Insufficient data for backtest."}

        validate_rr_multipliers(sl_atr_mult, tp_atr_mult)

        target_risk = risk_per_trade if risk_per_trade is not None else self.risk_per_trade
        df = df.copy().sort_values("timestamp").reset_index(drop=True)

        trainer = model_trainer or ModelTrainer()
        # Only an explicit pair of directional columns is trusted. If either
        # side is missing, refresh both from the same model prediction so a
        # stale legacy prob_tp/prob_sl value cannot bias side selection.
        explicit_directional = "prob_long" in df.columns and "prob_short" in df.columns

        if not explicit_directional:
            try:
                probs = trainer.predict_probabilities(df)
                scores = [
                    directional_probability_scores(
                        probability, allow_short_score=trainer.supports_short_trades
                    )
                    for probability in probs
                ]
                df["prob_long"] = [long_score for long_score, _ in scores]
                df["prob_short"] = [short_score for _, short_score in scores]
            except Exception as e:
                logger.warning(f"ML model inference unavailable; using directional trend signals: {e}")
                bullish = df.get("trend_bullish", pd.Series(0, index=df.index)) == 1
                bearish = df.get("trend_bearish", pd.Series(0, index=df.index)) == 1
                rsi = df.get("rsi_14", pd.Series(50, index=df.index))
                macd_hist = df.get("macd_hist", pd.Series(0, index=df.index))
                df["prob_long"] = (bullish & (rsi > 50) & (macd_hist > 0)).astype(float) * 0.50
                df["prob_short"] = (bearish & (rsi < 50) & (macd_hist < 0)).astype(float) * 0.50
        if "prob_short" not in df.columns:
            df["prob_short"] = 0.0

        n = len(df)
        timestamps = df["timestamp"].to_numpy()
        opens = df["open"].to_numpy()
        highs = df["high"].to_numpy()
        lows = df["low"].to_numpy()
        closes = df["close"].to_numpy()
        atrs = df["atr_14"].to_numpy() if "atr_14" in df.columns else (closes * 0.01)
        prob_long = df["prob_long"].to_numpy()
        prob_short = df["prob_short"].to_numpy()

        equity = float(self.initial_capital)
        trades: List[Trade] = []
        equity_curve = []
        
        active_position = None
        trade_counter = 0

        entry_fee_rate = self.taker_fee if self.entry_is_taker else self.maker_fee
        exit_fee_rate = self.taker_fee if self.exit_is_taker else self.maker_fee

        for i in range(n):
            current_time = pd.Timestamp(timestamps[i])
            c_open = opens[i]
            c_high = highs[i]
            c_low = lows[i]
            c_close = closes[i]

            # 1. Intra-candle exit evaluation for active position
            if active_position is not None:
                holding_bars = i - active_position["entry_bar"]
                tp_price = active_position["tp_price"]
                sl_price = active_position["sl_price"]
                entry_price = active_position["entry_price"]
                pos_size = active_position["pos_size"]
                pos_value = active_position["pos_value"]
                risk_dollars = active_position["risk_dollars"]

                tp_hit = c_high >= tp_price
                sl_hit = c_low <= sl_price
                if active_position["direction"] == "SHORT":
                    tp_hit = c_low <= tp_price
                    sl_hit = c_high >= sl_price
                time_expired = holding_bars >= max_holding_bars

                trade_closed = False
                exit_price = 0.0
                exit_reason = ""

                if tp_hit and not sl_hit:
                    trade_closed = True
                    exit_price = tp_price * (1.0 - self.slippage if active_position["direction"] == "LONG" else 1.0 + self.slippage)
                    exit_reason = "TAKE_PROFIT"
                elif sl_hit and not tp_hit:
                    trade_closed = True
                    exit_price = sl_price * (1.0 - self.slippage if active_position["direction"] == "LONG" else 1.0 + self.slippage)
                    exit_reason = "STOP_LOSS"
                elif tp_hit and sl_hit:
                    # Intra-candle conflict: resolve using open price proximity
                    trade_closed = True
                    dist_to_sl = abs(c_open - sl_price)
                    dist_to_tp = abs(c_open - tp_price)
                    if dist_to_sl <= dist_to_tp:
                        exit_price = sl_price * (1.0 - self.slippage if active_position["direction"] == "LONG" else 1.0 + self.slippage)
                        exit_reason = "STOP_LOSS"
                    else:
                        exit_price = tp_price * (1.0 - self.slippage if active_position["direction"] == "LONG" else 1.0 + self.slippage)
                        exit_reason = "TAKE_PROFIT"
                elif time_expired:
                    trade_closed = True
                    exit_price = c_close * (1.0 - self.slippage if active_position["direction"] == "LONG" else 1.0 + self.slippage)
                    exit_reason = "TIME_EXPIRY"

                if trade_closed:
                    signed_move = exit_price - entry_price
                    if active_position["direction"] == "SHORT":
                        signed_move = -signed_move
                    gross_pnl = signed_move * pos_size
                    exit_notional = exit_price * pos_size
                    fees = (pos_value * entry_fee_rate) + (exit_notional * exit_fee_rate)

                    # Continuous 8-hour perpetual funding rate calculation
                    duration_hours = max(0.08, (current_time - active_position["entry_time"]).total_seconds() / 3600.0)
                    funding_cost = pos_value * self.funding_rate_8h * (duration_hours / 8.0)

                    net_pnl = gross_pnl - fees - funding_cost
                    # Compounding equity update
                    equity += net_pnl
                    return_pct = (net_pnl / pos_value) * 100.0 if pos_value > 0 else 0.0
                    trade_counter += 1

                    trades.append(Trade(
                        trade_id=trade_counter,
                        symbol=symbol,
                        entry_time=active_position["entry_time"],
                        exit_time=current_time,
                        side=active_position["direction"],
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
                    active_position = None

            # 2. Strict One Trade at a Time: check entry if no active position
            if active_position is None and i < n - 1 and equity > 5.0:
                direction = "LONG" if prob_long[i] >= prob_short[i] else "SHORT"
                signal_strength = max(prob_long[i], prob_short[i])
                if signal_strength >= confidence_threshold:
                    direction_sign = 1.0 if direction == "LONG" else -1.0
                    entry_price = c_close * (1.0 + direction_sign * self.slippage)
                    atr_val = atrs[i]
                    if atr_val <= 0 or np.isnan(atr_val):
                        atr_val = entry_price * 0.01

                    # Dynamic ATR Barriers (Strict 1:3 RR)
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

                    # Compounding Position Sizing based on ATR Risk
                    # Dollar Risk = Equity * target_risk
                    dollar_risk = equity * target_risk
                    pos_size = dollar_risk / sl_dist
                    pos_notional = pos_size * entry_price

                    # Cap maximum leverage to safeguard against abnormal tight volatility
                    max_notional = equity * self.max_leverage
                    if pos_notional > max_notional:
                        pos_notional = max_notional
                        pos_size = pos_notional / entry_price
                        dollar_risk = pos_size * sl_dist

                    active_position = {
                        "entry_bar": i,
                        "entry_time": current_time,
                        "entry_price": entry_price,
                        "direction": direction,
                        "tp_price": tp_price,
                        "sl_price": sl_price,
                        "pos_size": pos_size,
                        "pos_value": pos_notional,
                        "risk_dollars": dollar_risk
                    }

            # Mark-to-market equity point
            if active_position is not None:
                direction_sign = 1.0 if active_position["direction"] == "LONG" else -1.0
                unrealized_pnl = direction_sign * (c_close - active_position["entry_price"]) * active_position["pos_size"]
                current_equity = max(0.0, equity + unrealized_pnl)
            else:
                current_equity = equity

            equity_curve.append({
                "timestamp": current_time,
                "equity": float(current_equity),
                "in_position": 1 if active_position is not None else 0
            })

        trades_df = pd.DataFrame([asdict(t) for t in trades])
        equity_df = pd.DataFrame(equity_curve)

        metrics = self._calculate_performance_metrics(trades_df, equity_df, target_risk)
        return {
            "metrics": metrics,
            "trades": trades_df,
            "equity_curve": equity_df
        }

    def _calculate_performance_metrics(
        self,
        trades_df: pd.DataFrame,
        equity_df: pd.DataFrame,
        risk_pct: float
    ) -> Dict[str, Any]:
        """Calculates quantitative performance, compounding return, and drawdowns."""
        if trades_df.empty or equity_df.empty:
            return {
                "risk_pct": risk_pct,
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
                "total_funding": 0.0,
                "tp_hits": 0,
                "sl_hits": 0,
                "time_exits": 0,
                "avg_holding_bars": 0.0
            }

        initial_eq = self.initial_capital
        final_eq = float(equity_df["equity"].iloc[-1])
        compounding_return_pct = ((final_eq - initial_eq) / initial_eq) * 100.0

        equity_series = equity_df["equity"]
        peak = equity_series.cummax()
        drawdown = (equity_series - peak) / peak.replace(0, 1.0)
        max_drawdown_pct = abs(float(drawdown.min())) * 100.0
        equity_df["drawdown"] = drawdown

        winning_trades = trades_df[trades_df["net_pnl"] > 0]
        losing_trades = trades_df[trades_df["net_pnl"] <= 0]
        win_rate = (len(winning_trades) / len(trades_df)) * 100.0

        gross_profit = float(winning_trades["net_pnl"].sum())
        gross_loss = abs(float(losing_trades["net_pnl"].sum()))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0)

        # Annualized Sharpe & Sortino
        pct_returns = equity_series.pct_change().dropna()
        if len(pct_returns) > 1 and pct_returns.std() > 0:
            sharpe = float((pct_returns.mean() / pct_returns.std()) * np.sqrt(105120))
            neg_returns = pct_returns[pct_returns < 0]
            downside_std = neg_returns.std() if len(neg_returns) > 0 else 1e-6
            sortino = float((pct_returns.mean() / downside_std) * np.sqrt(105120))
        else:
            sharpe = 0.0
            sortino = 0.0

        calmar = (compounding_return_pct / max_drawdown_pct) if max_drawdown_pct > 0 else 0.0
        exit_counts = trades_df["exit_reason"].value_counts().to_dict()

        return {
            "risk_pct": float(risk_pct),
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
            "avg_holding_bars": float(round(trades_df["holding_bars"].mean(), 1))
        }


def compare_risk_profiles(
    df: pd.DataFrame,
    symbol: str = "BTC/USDT:USDT",
    risks: Optional[List[float]] = None,
    initial_capital: float = 100.0,
    confidence_threshold: float = 0.35,
    model_trainer: Optional[ModelTrainer] = None
) -> Dict[str, Any]:
    """
    Compares 2%, 3%, and 5% account risk per trade with compounding equity from $100 starting balance.
    Identifies the optimal risk configuration based on Sharpe and Calmar ratios.
    """
    if risks is None:
        risks = [0.02, 0.03, 0.05]

    engine = BacktestEngine(initial_capital=initial_capital)
    comparison_table = []
    runs = {}

    for r in risks:
        res = engine.run_backtest(
            df,
            symbol=symbol,
            risk_per_trade=r,
            confidence_threshold=confidence_threshold,
            model_trainer=model_trainer
        )
        runs[f"{int(r*100)}% Risk"] = res
        comparison_table.append(res["metrics"])

    comp_df = pd.DataFrame(comparison_table)
    
    # Identify optimal configuration by risk-adjusted return (Calmar * Sharpe score)
    best_idx = 0
    best_score = -999.0
    for idx, row in comp_df.iterrows():
        # Score combines Return, Drawdown penalty, and Sharpe
        score = row["sharpe_ratio"] * (row["calmar_ratio"] if row["calmar_ratio"] > 0 else 0.1)
        if score > best_score:
            best_score = score
            best_idx = idx

    optimal_risk = risks[best_idx]
    optimal_summary = comp_df.iloc[best_idx].to_dict()

    return {
        "comparison_table": comp_df,
        "runs": runs,
        "optimal_risk": optimal_risk,
        "optimal_summary": optimal_summary
    }
