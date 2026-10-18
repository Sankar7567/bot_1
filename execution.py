"""
Dual-Mode Execution Engine for Binance Futures ML Platform.

Features:
- Dual execution modes:
  1. PAPER: Local simulator tracking orders, fills, fees, funding, and compounding equity
     in signals/paper_wallet.json against live Binance ticker prices.
  2. LIVE: Real order execution directly on Binance Futures via CCXT using API credentials.
- Exchange Max Leverage Inspection:
  - Dynamically queries exchange trading rules / leverage brackets for each target symbol.
  - Automatically sets margin mode to ISOLATED and applies the maximum allowed leverage.
- Precise 5% Account Risk Calculation:
  - Risk is calculated strictly against current Account Equity (5% of balance), NOT leveraged notional.
  - Position size in coins = (Account_Equity * Risk_Pct) / (1.0 * ATR).
  - High leverage covers the required margin while protecting total capital.
- Strict Order Lifecycle & Global 1-Trade Rule:
  - Strict global single-trade invariance: strictly 1 trade active globally across 500+ symbols.
  - Bracket orders with 1:3 Risk-Reward (SL = 1.0 * ATR, TP = 3.0 * ATR).
- Automated Orphan Order Cleanup:
  - Startup Sweep: Cancels dangling, unlinked, or leftover pending orders on boot.
  - Closure Sweep: The exact moment an active trade closes, immediately sweeps and cancels
    all remaining associated bracket orders to ensure zero orphan exposure.
"""

import os
import json
import time
import math
import logging
from typing import Dict, List, Optional, Any, Tuple
from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import ccxt
import pandas as pd
import numpy as np

from data_pipeline import DataPipeline, sanitize_symbol_for_filename
from features import FeatureEngineer
from model import ModelTrainer
from live_signals import append_audit_log, LATEST_SIGNAL_PATH, ACTIVE_TRADE_PATH, AUDIT_LOG_PATH

logger = logging.getLogger("ExecutionEngine")

SIGNALS_DIR = Path("signals")
LOGS_DIR = Path("logs")
PAPER_WALLET_PATH = SIGNALS_DIR / "paper_wallet.json"
SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)


class PaperWallet:
    """
    Simulated paper trading wallet tracking equity, margin, fills, fees, and funding
    against live Binance ticker prices.
    """
    def __init__(self, initial_equity: float = 100.0, wallet_path: Path = PAPER_WALLET_PATH):
        self.wallet_path = wallet_path
        self.initial_equity = initial_equity
        self.equity: float = initial_equity
        self.available_balance: float = initial_equity
        self.margin_used: float = 0.0
        self.realized_pnl: float = 0.0
        self.unrealized_pnl: float = 0.0
        self.total_fees_paid: float = 0.0
        self.active_position: Optional[Dict[str, Any]] = None
        self.pending_orders: List[Dict[str, Any]] = []
        self.trade_history: List[Dict[str, Any]] = []
        self.load()

    def load(self) -> None:
        """Loads wallet state from disk if exists, else initializes new wallet."""
        if self.wallet_path.exists():
            try:
                with open(self.wallet_path, "r") as f:
                    data = json.load(f)
                    self.equity = float(data.get("equity", self.initial_equity))
                    self.available_balance = float(data.get("available_balance", self.equity))
                    self.margin_used = float(data.get("margin_used", 0.0))
                    self.realized_pnl = float(data.get("realized_pnl", 0.0))
                    self.unrealized_pnl = float(data.get("unrealized_pnl", 0.0))
                    self.total_fees_paid = float(data.get("total_fees_paid", 0.0))
                    self.active_position = data.get("active_position")
                    self.pending_orders = data.get("pending_orders", [])
                    self.trade_history = data.get("trade_history", [])
                    return
            except Exception as e:
                logger.error(f"Error loading paper wallet: {e}. Reinitializing.")
        self.save()

    def save(self) -> None:
        """Atomically saves wallet state to disk."""
        data = {
            "equity": round(self.equity, 4),
            "available_balance": round(self.available_balance, 4),
            "margin_used": round(self.margin_used, 4),
            "realized_pnl": round(self.realized_pnl, 4),
            "unrealized_pnl": round(self.unrealized_pnl, 4),
            "total_fees_paid": round(self.total_fees_paid, 4),
            "active_position": self.active_position,
            "pending_orders": self.pending_orders,
            "trade_history": self.trade_history[-100:],  # Store last 100 trades
            "updated_at": datetime.now(timezone.utc).isoformat()
        }
        tmp_path = self.wallet_path.with_suffix(".tmp")
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2)
        tmp_path.replace(self.wallet_path)

    def open_position(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        size: float,
        sl_price: float,
        tp_price: float,
        leverage: float,
        taker_fee_pct: float = 0.0005
    ) -> Dict[str, Any]:
        """Opens a new simulated position with 1:3 bracket orders."""
        notional = size * entry_price
        required_margin = notional / leverage
        entry_fee = notional * taker_fee_pct

        self.margin_used = required_margin
        self.available_balance = max(0.0, self.available_balance - required_margin - entry_fee)
        self.equity -= entry_fee
        self.total_fees_paid += entry_fee

        now_iso = datetime.now(timezone.utc).isoformat()
        pos = {
            "symbol": symbol,
            "direction": direction,
            "entry_price": entry_price,
            "size": size,
            "notional": notional,
            "leverage": leverage,
            "margin": required_margin,
            "stop_loss": sl_price,
            "take_profit": tp_price,
            "entry_time": now_iso,
            "unrealized_pnl": 0.0
        }
        self.active_position = pos

        # Create simulated bracket orders
        opp_side = "SELL" if direction == "LONG" else "BUY"
        self.pending_orders = [
            {
                "order_id": f"paper_sl_{int(time.time()*1000)}",
                "symbol": symbol,
                "type": "STOP_MARKET",
                "side": opp_side,
                "stop_price": sl_price,
                "amount": size,
                "status": "OPEN",
                "reduce_only": True
            },
            {
                "order_id": f"paper_tp_{int(time.time()*1000)}",
                "symbol": symbol,
                "type": "TAKE_PROFIT_MARKET",
                "side": opp_side,
                "stop_price": tp_price,
                "amount": size,
                "status": "OPEN",
                "reduce_only": True
            }
        ]
        self.save()
        return pos

    def update_unrealized_pnl(self, current_price: float) -> float:
        """Updates unrealized PnL based on live ticker price."""
        if not self.active_position:
            self.unrealized_pnl = 0.0
            return 0.0

        pos = self.active_position
        dir_mult = 1.0 if pos["direction"] == "LONG" else -1.0
        raw_pnl = dir_mult * (current_price - pos["entry_price"]) * pos["size"]
        self.unrealized_pnl = raw_pnl
        pos["unrealized_pnl"] = round(raw_pnl, 4)
        return raw_pnl

    def close_position(
        self,
        exit_price: float,
        exit_reason: str,
        taker_fee_pct: float = 0.0005
    ) -> Dict[str, Any]:
        """Closes active position, updates wallet equity, and sweeps pending bracket orders."""
        if not self.active_position:
            return {}

        pos = self.active_position
        dir_mult = 1.0 if pos["direction"] == "LONG" else -1.0
        gross_pnl = dir_mult * (exit_price - pos["entry_price"]) * pos["size"]
        exit_notional = pos["size"] * exit_price
        exit_fee = exit_notional * taker_fee_pct

        net_pnl = gross_pnl - exit_fee
        self.realized_pnl += net_pnl
        self.equity += (pos["margin"] + gross_pnl - exit_fee)
        self.available_balance += (pos["margin"] + gross_pnl - exit_fee)
        self.total_fees_paid += exit_fee
        self.margin_used = 0.0
        self.unrealized_pnl = 0.0

        trade_record = {
            "symbol": pos["symbol"],
            "direction": pos["direction"],
            "entry_price": pos["entry_price"],
            "exit_price": exit_price,
            "size": pos["size"],
            "leverage": pos["leverage"],
            "margin": pos["margin"],
            "gross_pnl": round(gross_pnl, 4),
            "exit_fee": round(exit_fee, 4),
            "net_pnl": round(net_pnl, 4),
            "exit_reason": exit_reason,
            "entry_time": pos["entry_time"],
            "exit_time": datetime.now(timezone.utc).isoformat(),
            "final_equity": round(self.equity, 4)
        }
        self.trade_history.append(trade_record)
        self.active_position = None

        # Closure sweep: cancel all remaining bracket orders
        self.sweep_orphan_orders()
        self.save()
        return trade_record

    def sweep_orphan_orders(self) -> int:
        """Removes all dangling or pending bracket orders."""
        count = len(self.pending_orders)
        self.pending_orders = []
        self.save()
        return count


class LiveExchangeAdapter:
    """
    Production CCXT Binance Futures exchange adapter handling API authentication,
    leverage brackets inspection, isolated margin mode, and bracket order lifecycle.
    """
    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.exchange = ccxt.binance({
            "apiKey": self.api_key,
            "secret": self.api_secret,
            "enableRateLimit": True,
            "options": {
                "defaultType": "future",
                "adjustForTimeDifference": True
            }
        })
        self._leverage_brackets_cache: Dict[str, int] = {}

    def fetch_equity_and_balance(self) -> Tuple[float, float]:
        """Fetches total wallet equity and available margin in USDT."""
        try:
            balance = self.exchange.fetch_balance()
            usdt_info = balance.get("USDT", {})
            total_equity = float(usdt_info.get("total", 0.0) or balance.get("total", {}).get("USDT", 0.0))
            free_margin = float(usdt_info.get("free", 0.0) or balance.get("free", {}).get("USDT", 0.0))
            return total_equity, free_margin
        except Exception as e:
            logger.error(f"Failed to fetch account balance: {e}")
            raise

    def get_symbol_max_leverage(self, symbol: str) -> int:
        """
        Queries exchange leverage brackets for symbol and returns maximum allowed leverage.
        No arbitrary caps applied.
        """
        if symbol in self._leverage_brackets_cache:
            return self._leverage_brackets_cache[symbol]

        try:
            # CCXT fetch_leverage_tiers returns tiers for symbols
            tiers = self.exchange.fetch_leverage_tiers([symbol])
            sym_tiers = tiers.get(symbol, [])
            if not sym_tiers:
                # Try market id
                market = self.exchange.market(symbol)
                sym_tiers = tiers.get(market.get("id", ""), [])

            if sym_tiers:
                max_lev = int(max(t.get("maxLeverage", 20) for t in sym_tiers))
                self._leverage_brackets_cache[symbol] = max_lev
                return max_lev
        except Exception as e:
            logger.warning(f"Could not fetch leverage tiers via API for {symbol}: {e}. Inspecting market info.")

        try:
            market = self.exchange.market(symbol)
            max_lev = market.get("limits", {}).get("leverage", {}).get("max")
            if max_lev:
                self._leverage_brackets_cache[symbol] = int(max_lev)
                return int(max_lev)
        except Exception:
            pass

        # Robust exchange fallback based on Binance pair liquidity tier
        if "BTC" in symbol:
            fallback = 125
        elif "ETH" in symbol:
            fallback = 100
        elif any(k in symbol for k in ["SOL", "BNB", "XRP", "ADA", "DOGE"]):
            fallback = 50
        else:
            fallback = 20

        self._leverage_brackets_cache[symbol] = fallback
        return fallback

    def configure_symbol_margin_and_leverage(self, symbol: str, leverage: int) -> None:
        """
        Enforces ISOLATED margin mode and applies symbol's maximum leverage.
        Handles idempotent API responses gracefully.
        """
        # 1. Set Margin Mode to ISOLATED
        try:
            self.exchange.set_margin_mode("ISOLATED", symbol)
            logger.info(f"Set {symbol} margin mode to ISOLATED.")
        except Exception as e:
            msg = str(e).lower()
            if "no need to change" in msg or "already" in msg or "margin type cannot be changed" in msg:
                logger.debug(f"{symbol} is already set to ISOLATED.")
            else:
                logger.warning(f"Note on setting margin mode for {symbol}: {e}")

        # 2. Set Max Leverage
        try:
            self.exchange.set_leverage(leverage, symbol)
            logger.info(f"Set {symbol} leverage to {leverage}x.")
        except Exception as e:
            logger.warning(f"Note on setting leverage for {symbol} to {leverage}x: {e}")

    def get_open_positions(self) -> List[Dict[str, Any]]:
        """Queries the exchange for any currently active futures positions."""
        try:
            positions = self.exchange.fetch_positions()
            active = []
            for p in positions:
                contracts = float(p.get("contracts", 0.0) or p.get("size", 0.0) or 0.0)
                if abs(contracts) > 1e-6:
                    active.append(p)
            return active
        except Exception as e:
            logger.error(f"Error fetching active positions: {e}")
            return []

    def cancel_all_orders_for_symbol(self, symbol: str) -> int:
        """Cancels all open orders for a specific symbol."""
        try:
            res = self.exchange.cancel_all_orders(symbol)
            logger.info(f"Cancelled all open orders for {symbol}.")
            return len(res) if isinstance(res, list) else 1
        except Exception as e:
            logger.warning(f"Error cancelling orders for {symbol}: {e}")
            return 0

    def sweep_all_orphan_orders(self) -> int:
        """
        Automated Orphan Cleanup: Scans all open orders on exchange.
        If no position exists for that symbol, cancels the lingering bracket orders.
        """
        try:
            open_orders = self.exchange.fetch_open_orders()
            if not open_orders:
                return 0

            active_positions = self.get_open_positions()
            active_symbols = {p.get("symbol") for p in active_positions}

            cancelled_count = 0
            for o in open_orders:
                sym = o.get("symbol")
                if sym not in active_symbols:
                    try:
                        self.exchange.cancel_order(o["id"], sym)
                        cancelled_count += 1
                        logger.info(f"[ORPHAN_CLEANUP] Cancelled orphan order {o['id']} on {sym}")
                    except Exception as err:
                        logger.warning(f"Failed to cancel orphan order {o['id']}: {err}")

            if cancelled_count > 0:
                append_audit_log("ORPHAN_SWEEP", f"Cleaned up {cancelled_count} orphan orders on startup/closure.")
            return cancelled_count
        except Exception as e:
            logger.error(f"Error during exchange orphan sweep: {e}")
            return 0

    def execute_bracket_trade(
        self,
        symbol: str,
        direction: str,
        amount: float,
        stop_loss: float,
        take_profit: float
    ) -> Dict[str, Any]:
        """
        Places market entry order followed immediately by Stop-Loss and Take-Profit bracket orders.
        """
        side = "buy" if direction == "LONG" else "sell"
        opp_side = "sell" if direction == "LONG" else "buy"

        market = self.exchange.market(symbol)
        formatted_amount = float(self.exchange.amount_to_precision(symbol, amount))
        formatted_sl = float(self.exchange.price_to_precision(symbol, stop_loss))
        formatted_tp = float(self.exchange.price_to_precision(symbol, take_profit))

        # 1. Market Entry Order
        logger.info(f"Executing MARKET {side.upper()} for {formatted_amount} {symbol}...")
        entry_order = self.exchange.create_order(
            symbol=symbol,
            type="MARKET",
            side=side,
            amount=formatted_amount
        )
        entry_price = float(entry_order.get("average") or entry_order.get("price") or 0.0)
        if entry_price <= 0:
            ticker = self.exchange.fetch_ticker(symbol)
            entry_price = float(ticker.get("last") or ticker.get("close") or 0.0)

        # 2. Stop-Loss Bracket Order (reduce-only)
        sl_order = None
        try:
            sl_order = self.exchange.create_order(
                symbol=symbol,
                type="STOP_MARKET",
                side=opp_side,
                amount=formatted_amount,
                params={"stopPrice": formatted_sl, "reduceOnly": True}
            )
            logger.info(f"Placed Stop-Loss order at {formatted_sl} for {symbol}.")
        except Exception as e:
            logger.error(f"Failed to place SL bracket order: {e}")

        # 3. Take-Profit Bracket Order (reduce-only)
        tp_order = None
        try:
            tp_order = self.exchange.create_order(
                symbol=symbol,
                type="TAKE_PROFIT_MARKET",
                side=opp_side,
                amount=formatted_amount,
                params={"stopPrice": formatted_tp, "reduceOnly": True}
            )
            logger.info(f"Placed Take-Profit order at {formatted_tp} for {symbol}.")
        except Exception as e:
            logger.error(f"Failed to place TP bracket order: {e}")

        return {
            "entry_order": entry_order,
            "entry_price": entry_price,
            "amount": formatted_amount,
            "sl_order": sl_order,
            "tp_order": tp_order,
            "stop_loss": formatted_sl,
            "take_profit": formatted_tp
        }


class DualModeExecutionEngine:
    """
    Universal Dual-Mode Trading Engine for Binance Futures.
    Supports PAPER and LIVE execution with exact 5% risk calculation,
    isolated margin max leverage, global 1-trade rule, and automated orphan cleanup.
    """
    def __init__(
        self,
        mode: str = "PAPER",
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        risk_pct: float = 0.05,            # Strict 5% account risk
        confidence_threshold: float = 0.35,
        base_timeframe: str = "5m",
        htf_timeframes: Optional[List[str]] = None,
        symbols: Optional[List[str]] = None,
        initial_paper_equity: float = 100.0,
        wallet_path: Optional[Path] = None,
        pipeline: Optional[DataPipeline] = None
    ):
        self.mode = mode.upper()
        if self.mode not in ["PAPER", "LIVE"]:
            raise ValueError(f"Invalid mode: {mode}. Must be 'PAPER' or 'LIVE'.")

        self.risk_pct = risk_pct
        self.confidence_threshold = confidence_threshold
        self.base_timeframe = base_timeframe
        self.htf_timeframes = htf_timeframes or ["15m", "1h"]
        self._symbols = symbols

        self.pipeline = pipeline or DataPipeline()
        self.fe = FeatureEngineer(self.pipeline)
        self.trainer = ModelTrainer(pipeline=self.pipeline)
        self.trainer.load_model()

        # Initialize Mode Adapter
        self.live_adapter: Optional[LiveExchangeAdapter] = None
        self.paper_wallet: Optional[PaperWallet] = None

        if self.mode == "LIVE":
            key = api_key or os.getenv("BINANCE_API_KEY")
            sec = api_secret or os.getenv("BINANCE_API_SECRET")
            if not key or not sec:
                raise ValueError("LIVE mode requires valid BINANCE_API_KEY and BINANCE_API_SECRET.")
            self.live_adapter = LiveExchangeAdapter(api_key=key, api_secret=sec)
            logger.info("Initialized LIVE Binance Futures Execution Adapter.")
        else:
            self.paper_wallet = PaperWallet(
                initial_equity=initial_paper_equity,
                wallet_path=wallet_path or PAPER_WALLET_PATH
            )
            logger.info(f"Initialized PAPER Trading Simulator (Starting Balance: ${self.paper_wallet.equity:.2f}).")

        # Startup Sweep
        self.startup_sweep()

    @property
    def symbols(self) -> List[str]:
        """Resolves target symbol list, pulling top Binance Futures if not specified."""
        if not self._symbols:
            top_syms = self.pipeline.get_top_futures_symbols(limit=500)
            self._symbols = top_syms if top_syms else ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]
        return self._symbols

    def startup_sweep(self) -> None:
        """
        Automated Startup Sweep: Cancels dangling, unlinked, or leftover pending orders.
        """
        append_audit_log("STARTUP_SWEEP", f"Executing startup sweep in {self.mode} mode...")
        if self.mode == "LIVE" and self.live_adapter:
            cleared = self.live_adapter.sweep_all_orphan_orders()
            logger.info(f"Live startup sweep cleared {cleared} orphan orders.")
        elif self.mode == "PAPER" and self.paper_wallet:
            if not self.paper_wallet.active_position:
                cleared = self.paper_wallet.sweep_orphan_orders()
                logger.info(f"Paper startup sweep cleared {cleared} pending orders.")

    def get_current_equity(self) -> float:
        """Returns the current real or simulated account equity."""
        if self.mode == "LIVE" and self.live_adapter:
            try:
                equity, _ = self.live_adapter.fetch_equity_and_balance()
                return equity
            except Exception:
                return 100.0
        elif self.paper_wallet:
            return self.paper_wallet.equity
        return 100.0

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

    def fetch_live_ticker_price(self, symbol: str) -> float:
        """Fetches live market price via CCXT."""
        try:
            ticker = self.pipeline.exchange.fetch_ticker(symbol)
            return float(ticker.get("last") or ticker.get("close") or 0.0)
        except Exception:
            df = self.pipeline.load_symbol_data(self.base_timeframe, symbol)
            if not df.empty:
                return float(df.iloc[-1]["close"])
        return 0.0

    def check_and_process_exit(self, active_trade: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Monitors active trade against market prices.
        If Stop-Loss or Take-Profit is hit, executes closure and triggers immediate orphan cleanup sweep.
        """
        sym = active_trade["symbol"]
        curr_price = self.fetch_live_ticker_price(sym)
        if curr_price <= 0:
            return None

        direction = active_trade["direction"]
        sl_price = active_trade["stop_loss"]
        tp_price = active_trade["take_profit"]

        if direction == "LONG":
            tp_hit = curr_price >= tp_price
            sl_hit = curr_price <= sl_price
        else:  # SHORT
            tp_hit = curr_price <= tp_price
            sl_hit = curr_price >= sl_price

        if not (tp_hit or sl_hit):
            # In paper mode, update unrealized PnL
            if self.mode == "PAPER" and self.paper_wallet:
                self.paper_wallet.update_unrealized_pnl(curr_price)
            return None

        # Exit triggered!
        exit_reason = "TAKE_PROFIT" if tp_hit else "STOP_LOSS"
        exit_price = tp_price if tp_hit else sl_price
        logger.info(f"[{exit_reason}] Active position on {sym} triggered exit at {exit_price}")

        exit_event: Dict[str, Any] = {}

        if self.mode == "PAPER" and self.paper_wallet:
            exit_event = self.paper_wallet.close_position(
                exit_price=exit_price,
                exit_reason=exit_reason
            )
        elif self.mode == "LIVE" and self.live_adapter:
            # Closure sweep: Immediately cancel all remaining orders for symbol
            self.live_adapter.cancel_all_orders_for_symbol(sym)
            self.live_adapter.sweep_all_orphan_orders()
            equity = self.get_current_equity()
            exit_event = {
                "symbol": sym,
                "direction": direction,
                "exit_reason": exit_reason,
                "exit_price": exit_price,
                "exit_time": datetime.now(timezone.utc).isoformat(),
                "final_equity": equity
            }

        # Clear active trade state on disk
        self.save_active_trade({"status": "CLOSED", "last_closed": exit_event})
        append_audit_log(
            "TRADE_CLOSED",
            f"Closed {sym} ({direction}) via {exit_reason} at ${exit_price:.4f}. Immediate orphan sweep complete.",
            exit_event
        )
        return exit_event

    def calculate_5pct_risk_position(
        self,
        symbol: str,
        entry_price: float,
        atr_val: float,
        direction: str,
        max_leverage: int,
        account_equity: Optional[float] = None
    ) -> Tuple[float, float, float, float]:
        """
        Derives exact position size based on 5% account equity risk and ATR stop distance:
        - dollar_risk = account_equity * 0.05
        - sl_dist = 1.0 * ATR
        - target_size = dollar_risk / sl_dist
        - required_margin = (target_size * entry_price) / max_leverage
        Returns: (position_size, stop_loss, take_profit, dollar_risk)
        """
        equity = account_equity if account_equity is not None else self.get_current_equity()
        if equity <= 0:
            equity = 100.0

        # Exact 5% risk calculation strictly on Account Equity
        dollar_risk = equity * self.risk_pct
        sl_dist = 1.0 * atr_val
        tp_dist = 3.0 * atr_val

        if direction == "LONG":
            stop_loss = max(0.0001, entry_price - sl_dist)
            take_profit = entry_price + tp_dist
        else:  # SHORT
            stop_loss = entry_price + sl_dist
            take_profit = max(0.0001, entry_price - tp_dist)

        # Size in coin units
        target_size = dollar_risk / sl_dist

        # Leverage coverage check: Ensure required margin does not exceed available equity
        max_notional = equity * max_leverage
        max_allowed_coins = max_notional / entry_price
        if target_size > max_allowed_coins:
            target_size = max_allowed_coins

        # Format size to 4 decimal places
        target_size = max(0.0001, round(target_size, 4))
        return target_size, stop_loss, take_profit, dollar_risk

    def evaluate_symbol_for_entry(self, sym: str) -> Optional[Dict[str, Any]]:
        """Computes live features and evaluates LightGBM inference for a symbol."""
        try:
            df_base = self.pipeline.load_symbol_data(self.base_timeframe, sym)
            if df_base.empty or len(df_base) < 30:
                # Try fetching recent candles live via CCXT
                candles = self.pipeline.exchange.fetch_ohlcv(sym, self.base_timeframe, limit=100)
                if candles:
                    df_base = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
                    df_base["timestamp"] = pd.to_datetime(df_base["timestamp"], unit="ms", utc=True)
                    for col in ["open", "high", "low", "close", "volume"]:
                        df_base[col] = df_base[col].astype(float)

            if df_base.empty or len(df_base) < 30:
                return None

            base_feats = self.fe.compute_single_timeframe_features(df_base)
            if base_feats.empty:
                return None

            merged = df_base.merge(base_feats, on="timestamp", how="inner")

            # Merge HTF features
            for htf in self.htf_timeframes:
                df_htf = self.pipeline.load_symbol_data(htf, sym)
                if df_htf.empty or len(df_htf) < 20:
                    candles_htf = self.pipeline.exchange.fetch_ohlcv(sym, htf, limit=60)
                    if candles_htf:
                        df_htf = pd.DataFrame(candles_htf, columns=["timestamp", "open", "high", "low", "close", "volume"])
                        df_htf["timestamp"] = pd.to_datetime(df_htf["timestamp"], unit="ms", utc=True)
                        for col in ["open", "high", "low", "close", "volume"]:
                            df_htf[col] = df_htf[col].astype(float)

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

            # Model inference
            if self.trainer.booster is not None or self.trainer.model is not None:
                probs = self.trainer.predict_probabilities(merged)
                prob_tp = float(probs[-1, 2])
                prob_sl = float(probs[-1, 0])
            else:
                rsi = merged.iloc[-1].get("rsi_14", 50)
                prob_tp = 0.40 if rsi > 50 else 0.20
                prob_sl = 0.40 if rsi <= 50 else 0.20

            if prob_tp >= prob_sl:
                direction = "LONG"
                confidence = prob_tp
            else:
                direction = "SHORT"
                confidence = prob_sl

            curr_bar = merged.iloc[-1]
            return {
                "symbol": sym,
                "bar": curr_bar,
                "direction": direction,
                "confidence": confidence
            }
        except Exception as e:
            logger.debug(f"Evaluation error for {sym}: {e}")
            return None

    def execute_trade(self, candidate: Dict[str, Any]) -> Dict[str, Any]:
        """
        Executes a high-conviction trade in either PAPER or LIVE mode.
        - Sets ISOLATED margin mode and symbol's max leverage.
        - Sizes position to 5% equity risk.
        - Places entry and bracket orders (1:3 RR).
        - Persists active state.
        """
        sym = candidate["symbol"]
        bar = candidate["bar"]
        direction = candidate["direction"]
        confidence = candidate["confidence"]

        entry_price = float(bar["close"])
        atr_val = float(bar.get("atr_14", entry_price * 0.01))
        if atr_val <= 0 or np.isnan(atr_val):
            atr_val = entry_price * 0.01

        # 1. Determine Symbol Max Leverage
        if self.mode == "LIVE" and self.live_adapter:
            max_lev = self.live_adapter.get_symbol_max_leverage(sym)
            self.live_adapter.configure_symbol_margin_and_leverage(sym, max_lev)
        else:
            # Paper mode leverage simulation based on liquidity tiers
            if "BTC" in sym:
                max_lev = 125
            elif "ETH" in sym:
                max_lev = 100
            elif any(k in sym for k in ["SOL", "BNB", "XRP"]):
                max_lev = 50
            else:
                max_lev = 20

        # 2. Derive 5% Account Risk Position Sizing
        pos_size, sl_price, tp_price, dollar_risk = self.calculate_5pct_risk_position(
            symbol=sym,
            entry_price=entry_price,
            atr_val=atr_val,
            direction=direction,
            max_leverage=max_lev
        )

        now_iso = datetime.now(timezone.utc).isoformat()
        signal_payload = {
            "timestamp": now_iso,
            "symbol": sym,
            "direction": direction,
            "confidence": round(confidence, 4),
            "entry_price": round(entry_price, 4),
            "stop_loss": round(sl_price, 4),
            "take_profit": round(tp_price, 4),
            "risk_pct": round(self.risk_pct * 100.0, 1),
            "dollar_risk": round(dollar_risk, 4),
            "leverage": max_lev,
            "suggested_position_size": pos_size,
            "mode": self.mode,
            "status": "SIGNAL_GENERATED"
        }

        # Save to latest_signal.json
        tmp_signal_path = LATEST_SIGNAL_PATH.with_suffix(".tmp")
        with open(tmp_signal_path, "w") as f:
            json.dump(signal_payload, f, indent=2)
        tmp_signal_path.replace(LATEST_SIGNAL_PATH)

        # 3. Mode Execution
        if self.mode == "PAPER" and self.paper_wallet:
            self.paper_wallet.open_position(
                symbol=sym,
                direction=direction,
                entry_price=entry_price,
                size=pos_size,
                sl_price=sl_price,
                tp_price=tp_price,
                leverage=max_lev
            )
            logger.info(
                f"[PAPER_ORDER] Opened {direction} on {sym} at ${entry_price:.4f} | "
                f"Size: {pos_size} | Leverage: {max_lev}x | Risk: ${dollar_risk:.2f} (5%) | "
                f"SL: ${sl_price:.4f} | TP: ${tp_price:.4f}"
            )
        elif self.mode == "LIVE" and self.live_adapter:
            order_res = self.live_adapter.execute_bracket_trade(
                symbol=sym,
                direction=direction,
                amount=pos_size,
                stop_loss=sl_price,
                take_profit=tp_price
            )
            signal_payload["live_order_details"] = str(order_res.get("entry_order", {}))
            logger.info(f"[LIVE_ORDER] Executed {direction} on {sym} on Binance Futures.")

        # Persist Active State (Strict Global 1-Trade Rule)
        active_state = dict(signal_payload)
        active_state["status"] = "OPEN"
        self.save_active_trade(active_state)

        append_audit_log(
            "ORDER_EXECUTED",
            f"Executed {self.mode} {direction} on {sym} ({max_lev}x Leverage, 5% Risk: ${dollar_risk:.2f})",
            signal_payload
        )
        return signal_payload

    def step(self) -> Optional[Dict[str, Any]]:
        """
        Executes a single evaluation cycle:
        1. Checks active trade for exit (TP/SL). If hit, sweeps orphan orders.
        2. If active trade in progress, locks global universe scan (1-trade-at-a-time).
        3. If no active trade, evaluates universe for highest-conviction setup.
        4. If found, executes trade with max leverage & 5% risk calculation.
        """
        # 1. Check Active Trade
        active_trade = self.get_active_trade()
        if active_trade is not None:
            exit_event = self.check_and_process_exit(active_trade)
            if exit_event is None:
                logger.debug(f"Trade in progress on {active_trade['symbol']}. Global scan locked.")
                return None

        # 2. Universe Scan
        target_symbols = self.symbols
        best_cand = None
        best_conf = -1.0

        max_workers = min(10, max(2, len(target_symbols)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_sym = {executor.submit(self.evaluate_symbol_for_entry, s): s for s in target_symbols}
            for fut in as_completed(future_to_sym):
                cand = fut.result()
                if cand is not None:
                    if cand["confidence"] >= self.confidence_threshold and cand["confidence"] > best_conf:
                        best_conf = cand["confidence"]
                        best_cand = cand

        if best_cand is None:
            logger.debug("No setup met the confidence threshold.")
            return None

        # 3. High-Conviction Setup Found -> Execute
        return self.execute_trade(best_cand)

    def run_loop(self, poll_interval_sec: int = 10, max_iterations: Optional[int] = None) -> None:
        """Runs the execution engine continuously in a polling loop."""
        append_audit_log(
            "ENGINE_STARTED",
            f"Execution Engine started in {self.mode} mode (Interval: {poll_interval_sec}s, 5% Risk)"
        )
        iterations = 0
        try:
            while True:
                self.step()
                iterations += 1
                if max_iterations and iterations >= max_iterations:
                    break
                time.sleep(poll_interval_sec)
        except KeyboardInterrupt:
            append_audit_log("ENGINE_STOPPED", f"Execution Engine stopped by user after {iterations} iterations.")
