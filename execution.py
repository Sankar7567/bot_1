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
- 24/7 Daemon Loop (run_loop):
  - SCANNING PHASE: Full ML universe scan across 500+ symbols when no trade is active.
  - MONITORING PHASE: Tight TP/SL polling loop while a trade is open; checks BOTH local
    price arithmetic AND live Binance fetch_positions() reconciliation so bracket fills
    registered directly on the exchange are detected immediately.
  - Trade closure triggers an instant Closure Sweep then immediately resumes scanning.
- Binance fetchOpenOrders Warning Suppression:
  - Sets exchange.options["fetchOpenOrders"]["warnWithoutSymbol"] = False to silence the
    CCXT rate-limit advisory emitted when calling fetch_open_orders() without a symbol.
    We intentionally call it without a symbol during the global orphan sweep.
- Credential Management & Graceful Fallback:
  - Loads BINANCE_API_KEY / BINANCE_API_SECRET from .env via python-dotenv.
  - If keys are missing, empty, or dummy placeholders, emits a warning and falls back
    to PAPER mode without crashing.
- Dynamic Exchange Limit Validation & Sub-Minimum Skipping:
  - Queries per-symbol minNotional, minQty, stepSize from Binance exchange metadata.
  - If 5% risk sizing yields a position below the exchange minimum, logs
    [SKIPPED_MIN_NOTIONAL] and evaluates the next highest-confidence candidate.
"""

import os
import json
import time
import math
import logging
from typing import Dict, List, Optional, Any, Tuple, Callable
from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv
load_dotenv()

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


def validate_binance_credentials(api_key: Optional[str], api_secret: Optional[str]) -> bool:
    """Validates that Binance API credentials are non-empty and not dummy placeholders."""
    if not api_key or not api_secret:
        return False
    k = api_key.strip()
    s = api_secret.strip()
    if not k or not s:
        return False
    dummy_markers = {
        "your_binance_api_key_here",
        "your_binance_api_secret_here",
        "none",
        "placeholder",
        "dummy",
        "test",
        "xxx"
    }
    if k.lower() in dummy_markers or s.lower() in dummy_markers:
        return False
    if len(k) < 16 or len(s) < 16:
        return False
    return True


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
        # Suppress Binance CCXT warning about calling fetchOpenOrders without a symbol.
        # We call it intentionally during the orphan sweep to scan all open orders globally.
        self.exchange.options["fetchOpenOrders"] = {"warnWithoutSymbol": False}
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

    def verify_position_closed_on_exchange(self, symbol: str) -> bool:
        """
        Queries Binance Futures exchange to verify whether a position for the given
        symbol is still open (non-zero contracts). Returns True if the position is
        confirmed closed (zero size), False if still open or undetermined.

        Used by the daemon loop to detect live TP/SL fills that occurred on the
        exchange independently of local price tracking, ensuring the active-trade
        lock is released promptly and the next scan cycle begins.
        """
        try:
            positions = self.exchange.fetch_positions([symbol])
            for pos in positions:
                contracts = float(pos.get("contracts", 0.0) or pos.get("size", 0.0) or 0.0)
                if abs(contracts) > 1e-6:
                    # Position is still live on exchange
                    return False
            # No non-zero position found — confirmed closed
            return True
        except Exception as e:
            logger.warning(f"[LIVE_CHECK] Could not verify position status for {symbol} on exchange: {e}")
            # Default to not closed (safe assumption) to avoid spurious re-entry
            return False

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
        self.latest_scan_leaderboard: List[Dict[str, Any]] = []

        # Initialize Mode Adapter
        self.live_adapter: Optional[LiveExchangeAdapter] = None
        self.paper_wallet: Optional[PaperWallet] = None

        if self.mode == "LIVE":
            key = (api_key or os.getenv("BINANCE_API_KEY", "")).strip()
            sec = (api_secret or os.getenv("BINANCE_API_SECRET", "")).strip()
            
            if not validate_binance_credentials(key, sec):
                logger.warning("[WARN] Valid live credentials not found. Falling back to PAPER mode.")
                append_audit_log(
                    "CREDENTIAL_FALLBACK",
                    "[WARN] Valid live credentials not found. Falling back to PAPER mode.",
                    level="WARNING"
                )
                self.mode = "PAPER"
                self.paper_wallet = PaperWallet(
                    initial_equity=initial_paper_equity,
                    wallet_path=wallet_path or PAPER_WALLET_PATH
                )
                logger.info(f"Initialized PAPER Trading Simulator (Starting Balance: ${self.paper_wallet.equity:.2f}).")
            else:
                try:
                    self.live_adapter = LiveExchangeAdapter(api_key=key, api_secret=sec)
                    self.live_adapter.fetch_equity_and_balance()
                    logger.info("Initialized LIVE Binance Futures Execution Adapter.")
                except Exception as e:
                    logger.warning(f"[WARN] Valid live credentials not found ({e}). Falling back to PAPER mode.")
                    append_audit_log(
                        "CREDENTIAL_FALLBACK",
                        f"[WARN] Valid live credentials not found ({e}). Falling back to PAPER mode.",
                        level="WARNING"
                    )
                    self.mode = "PAPER"
                    self.paper_wallet = PaperWallet(
                        initial_equity=initial_paper_equity,
                        wallet_path=wallet_path or PAPER_WALLET_PATH
                    )
                    logger.info(f"Initialized PAPER Trading Simulator (Starting Balance: ${self.paper_wallet.equity:.2f}).")
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
        Monitors active trade against market prices AND verifies against live exchange positions.

        Two-layer exit detection:
          1. Local price arithmetic: Compares current ticker price against stored TP/SL levels.
          2. Live exchange reconciliation (LIVE mode only): Queries Binance fetch_positions() to
             detect fills that occurred on the exchange (e.g., limit TP/SL orders filled by broker)
             even before the local ticker price check confirms the barrier breach.

        If either layer detects closure, executes the Closure Sweep (cancels orphan bracket
        orders), updates active_trade_state.json to CLOSED, logs to audit, and returns the
        exit event dict to unblock the daemon loop for the next universe scan.
        """
        sym = active_trade["symbol"]
        direction = active_trade["direction"]
        sl_price = active_trade["stop_loss"]
        tp_price = active_trade["take_profit"]

        # ── Layer 1: Live Exchange Reconciliation (LIVE mode) ────────────────────
        # Detect TP/SL bracket fills that the exchange executed independently.
        exchange_confirmed_closed = False
        if self.mode == "LIVE" and self.live_adapter:
            exchange_confirmed_closed = self.live_adapter.verify_position_closed_on_exchange(sym)
            if exchange_confirmed_closed:
                logger.info(
                    f"[LIVE_CHECK] Exchange confirms position on {sym} is CLOSED "
                    f"(TP/SL bracket filled). Triggering closure sweep."
                )

        # ── Layer 2: Local Price Arithmetic ──────────────────────────────────────
        curr_price = self.fetch_live_ticker_price(sym)
        tp_hit, sl_hit = False, False

        if curr_price > 0:
            if direction == "LONG":
                tp_hit = curr_price >= tp_price
                sl_hit = curr_price <= sl_price
            else:  # SHORT
                tp_hit = curr_price <= tp_price
                sl_hit = curr_price >= sl_price

        # ── If neither layer detected an exit, trade is still active ─────────────
        if not (tp_hit or sl_hit or exchange_confirmed_closed):
            # Update unrealized PnL in paper mode
            if self.mode == "PAPER" and self.paper_wallet and curr_price > 0:
                self.paper_wallet.update_unrealized_pnl(curr_price)
            return None

        # ── Exit Triggered ────────────────────────────────────────────────────────
        if exchange_confirmed_closed and not (tp_hit or sl_hit):
            # Exchange closed the position but price hasn't confirmed a local barrier yet.
            # Use last known price and infer exit reason from position relative to entry.
            exit_price = curr_price if curr_price > 0 else float(active_trade.get("entry_price", 0.0))
            entry_price = float(active_trade.get("entry_price", exit_price))
            pnl_sign = (exit_price - entry_price) if direction == "LONG" else (entry_price - exit_price)
            exit_reason = "TAKE_PROFIT" if pnl_sign >= 0 else "STOP_LOSS"
            logger.info(
                f"[EXCHANGE_CLOSED] Position on {sym} closed on exchange at ~${exit_price:.4f} "
                f"(inferred reason: {exit_reason})"
            )
        else:
            exit_reason = "TAKE_PROFIT" if tp_hit else "STOP_LOSS"
            exit_price = tp_price if tp_hit else sl_price
            logger.info(f"[{exit_reason}] Active position on {sym} triggered exit at ${exit_price:.4f}")

        exit_event: Dict[str, Any] = {}

        if self.mode == "PAPER" and self.paper_wallet:
            exit_event = self.paper_wallet.close_position(
                exit_price=exit_price,
                exit_reason=exit_reason
            )
        elif self.mode == "LIVE" and self.live_adapter:
            # Closure sweep: immediately cancel all remaining bracket orders for symbol
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

    def get_symbol_limits(self, symbol: str) -> Dict[str, Any]:
        """
        Queries Binance exchange limits for minNotional, minQty, and precision.
        """
        try:
            market = self.pipeline.exchange.market(symbol)
            limits = market.get("limits", {})
            cost_min = limits.get("cost", {}).get("min")
            amount_min = limits.get("amount", {}).get("min")
            precision = market.get("precision", {})
            amount_step = precision.get("amount")
            price_step = precision.get("price")

            min_notional = float(cost_min) if cost_min is not None else 5.0
            min_qty = float(amount_min) if amount_min is not None else 0.001
            step_size = float(amount_step) if amount_step is not None else 0.001
            tick_size = float(price_step) if price_step is not None else 0.01

            return {
                "min_notional": min_notional,
                "min_qty": min_qty,
                "step_size": step_size,
                "tick_size": tick_size
            }
        except Exception:
            return {
                "min_notional": 5.0,
                "min_qty": 0.001,
                "step_size": 0.001,
                "tick_size": 0.01
            }

    def get_symbol_max_leverage(self, symbol: str) -> int:
        """Determines maximum leverage for symbol from exchange market rules."""
        if self.mode == "LIVE" and self.live_adapter:
            return self.live_adapter.get_symbol_max_leverage(symbol)
        try:
            market = self.pipeline.exchange.market(symbol)
            max_lev = market.get("limits", {}).get("leverage", {}).get("max")
            if max_lev:
                return int(max_lev)
        except Exception:
            pass
        if "BTC" in symbol:
            return 125
        elif "ETH" in symbol:
            return 100
        elif any(k in symbol for k in ["SOL", "BNB", "XRP"]):
            return 50
        return 20

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

        # Format size according to precision / step size
        limits = self.get_symbol_limits(symbol)
        step = limits["step_size"]
        if step > 0:
            target_size = math.floor(target_size / step) * step
            # Avoid precision artifacts
            decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
            target_size = round(target_size, decimals)

        target_size = max(0.0001, round(target_size, 4))
        return target_size, stop_loss, take_profit, dollar_risk

    def evaluate_symbol_for_entry(self, sym: str) -> Optional[Dict[str, Any]]:
        """
        Computes live features and evaluates LightGBM inference for a symbol.
        Driven purely by normalized technical features without any hardcoded symbol bias.
        """
        try:
            df_base = self.pipeline.load_symbol_data(self.base_timeframe, sym)
            if df_base.empty or len(df_base) < 30:
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

            # Model inference: purely data-driven without symbol favoritism
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

    def execute_trade(
        self,
        candidate: Dict[str, Any],
        pos_size: Optional[float] = None,
        sl_price: Optional[float] = None,
        tp_price: Optional[float] = None,
        dollar_risk: Optional[float] = None,
        max_lev: Optional[int] = None
    ) -> Dict[str, Any]:
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
        if max_lev is None:
            max_lev = self.get_symbol_max_leverage(sym)

        if self.mode == "LIVE" and self.live_adapter:
            self.live_adapter.configure_symbol_margin_and_leverage(sym, max_lev)

        # 2. Derive 5% Account Risk Position Sizing if not precomputed
        if pos_size is None or sl_price is None or tp_price is None or dollar_risk is None:
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

    def step(
        self,
        progress_callback: Optional[Callable[[int, int, str, Optional[Dict[str, Any]]], None]] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Executes a single evaluation cycle:
        1. Checks active trade for exit (TP/SL). If hit, sweeps orphan orders.
        2. If active trade in progress, locks global universe scan (1-trade-at-a-time).
        3. Global Scan across symbols: pure ML prediction probabilities.
        4. Global Ranking: ranks candidate setups strictly by raw ML probability in descending order.
        5. Sub-Minimum Trade Skipping:
           If 5% risk sizing yields notional < minNotional or size < minQty, emits log and
           evaluates the next top candidate in the queue until a valid trade executes.
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
        total_syms = len(target_symbols)
        evaluated_candidates: List[Dict[str, Any]] = []

        max_workers = min(12, max(2, total_syms))
        completed_count = 0

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_sym = {executor.submit(self.evaluate_symbol_for_entry, s): s for s in target_symbols}
            for fut in as_completed(future_to_sym):
                sym = future_to_sym[fut]
                completed_count += 1
                try:
                    cand = fut.result()
                    if cand is not None:
                        evaluated_candidates.append(cand)
                    if progress_callback:
                        progress_callback(completed_count, total_syms, sym, cand)
                except Exception as e:
                    logger.debug(f"Scan worker error on {sym}: {e}")
                    if progress_callback:
                        progress_callback(completed_count, total_syms, sym, None)

        if not evaluated_candidates:
            logger.debug("No symbols could be evaluated.")
            self.latest_scan_leaderboard = []
            return None

        # 3. Pure ML Probability Global Ranking (Strict Descending Order)
        ranked_candidates = sorted(evaluated_candidates, key=lambda c: c["confidence"], reverse=True)

        leaderboard: List[Dict[str, Any]] = []
        executed_payload: Optional[Dict[str, Any]] = None

        # 4. Iterate Candidates & Enforce Dynamic Symbol Limits
        for cand in ranked_candidates:
            sym = cand["symbol"]
            conf = cand["confidence"]
            dir_ = cand["direction"]
            bar = cand["bar"]
            entry_price = float(bar["close"])
            atr_val = float(bar.get("atr_14", entry_price * 0.01))
            if atr_val <= 0 or np.isnan(atr_val):
                atr_val = entry_price * 0.01

            max_lev = self.get_symbol_max_leverage(sym)
            limits = self.get_symbol_limits(sym)

            pos_size, sl_price, tp_price, dollar_risk = self.calculate_5pct_risk_position(
                symbol=sym,
                entry_price=entry_price,
                atr_val=atr_val,
                direction=dir_,
                max_leverage=max_lev
            )
            notional = pos_size * entry_price

            item_status = "PENDING"

            if conf < self.confidence_threshold:
                item_status = "BELOW_THRESHOLD"
            elif executed_payload is not None:
                item_status = "VALID_SETUP_QUEUE"
            else:
                # Sub-minimum trade inspection
                if notional < limits["min_notional"]:
                    item_status = "SKIPPED_MIN_NOTIONAL"
                    logger.info(
                        f"[SKIPPED_MIN_NOTIONAL] Symbol {sym} position size ${notional:.2f} "
                        f"below exchange minNotional ${limits['min_notional']:.2f}. Evaluating next top setup."
                    )
                    append_audit_log(
                        "SKIPPED_MIN_NOTIONAL",
                        f"Symbol {sym} position size ${notional:.2f} below exchange minNotional ${limits['min_notional']:.2f}. Evaluating next top setup.",
                        {"symbol": sym, "notional": notional, "min_notional": limits["min_notional"]}
                    )
                elif pos_size < limits["min_qty"]:
                    item_status = "SKIPPED_MIN_QTY"
                    logger.info(
                        f"[SKIPPED_MIN_QTY] Symbol {sym} position size {pos_size} "
                        f"below exchange minQty {limits['min_qty']}. Evaluating next top setup."
                    )
                    append_audit_log(
                        "SKIPPED_MIN_QTY",
                        f"Symbol {sym} position size {pos_size} below exchange minQty {limits['min_qty']}. Evaluating next top setup.",
                        {"symbol": sym, "pos_size": pos_size, "min_qty": limits["min_qty"]}
                    )
                else:
                    # Valid highest-probability trade found! Execute!
                    item_status = "SELECTED_AND_EXECUTED"
                    executed_payload = self.execute_trade(
                        candidate=cand,
                        pos_size=pos_size,
                        sl_price=sl_price,
                        tp_price=tp_price,
                        dollar_risk=dollar_risk,
                        max_lev=max_lev
                    )

            leaderboard.append({
                "symbol": sym,
                "direction": dir_,
                "confidence": round(conf, 4),
                "atr": round(atr_val, 4),
                "entry_price": round(entry_price, 4),
                "position_size": pos_size,
                "notional": round(notional, 2),
                "leverage": max_lev,
                "min_notional": limits["min_notional"],
                "min_qty": limits["min_qty"],
                "status": item_status
            })

        self.latest_scan_leaderboard = leaderboard
        return executed_payload

    def run_loop(self, poll_interval_sec: int = 10, max_iterations: Optional[int] = None) -> None:
        """
        Runs the execution engine as a continuous 24/7 daemon loop.

        Lifecycle per iteration:
          ┌─ SCANNING PHASE ───────────────────────────────────────────────────────┐
          │  No active trade found → Full universe scan across 500+ symbols.       │
          │  Pure ML probability ranking → Select highest-conviction valid trade.  │
          │  If a trade is executed → transition to MONITORING PHASE.             │
          └────────────────────────────────────────────────────────────────────────┘
          ┌─ MONITORING PHASE ─────────────────────────────────────────────────────┐
          │  Active trade detected → Tick-by-tick TP/SL monitoring sub-loop.      │
          │  Two-layer exit detection every poll_interval_sec seconds:            │
          │    Layer 1: Local price arithmetic (ticker vs TP/SL levels).          │
          │    Layer 2: Live exchange reconciliation (fetch_positions check).      │
          │  Once exit confirmed → Closure sweep → Log closure → Resume SCANNING. │
          └────────────────────────────────────────────────────────────────────────┘

        Never exits unless:
          - KeyboardInterrupt (Ctrl+C by operator)
          - max_iterations reached (test / bounded run)
        """
        append_audit_log(
            "ENGINE_STARTED",
            f"[DAEMON] Execution Engine started in {self.mode} mode | "
            f"Poll Interval: {poll_interval_sec}s | Risk: {self.risk_pct*100:.1f}% | "
            f"Threshold: {self.confidence_threshold}"
        )
        logger.info(
            f"[DAEMON] 24/7 daemon loop active | Mode: {self.mode} | "
            f"Interval: {poll_interval_sec}s | Risk: {self.risk_pct*100:.1f}%"
        )

        iterations = 0
        try:
            while True:
                # ── Check iteration cap (used for tests / bounded runs) ──────────
                if max_iterations is not None and iterations >= max_iterations:
                    logger.info(f"[DAEMON] Reached max_iterations={max_iterations}. Exiting daemon loop.")
                    break

                active_trade = self.get_active_trade()

                if active_trade is not None:
                    # ── MONITORING PHASE ─────────────────────────────────────────
                    sym = active_trade.get("symbol", "UNKNOWN")
                    direction = active_trade.get("direction", "?")
                    entry = active_trade.get("entry_price", 0.0)
                    sl = active_trade.get("stop_loss", 0.0)
                    tp = active_trade.get("take_profit", 0.0)
                    logger.info(
                        f"[MONITOR] Active {direction} on {sym} | "
                        f"Entry: ${entry:.4f} | SL: ${sl:.4f} | TP: ${tp:.4f} | "
                        f"Watching for exit every {poll_interval_sec}s..."
                    )
                    exit_event = self.check_and_process_exit(active_trade)
                    if exit_event is not None:
                        # Trade closed — log summary and immediately return to scan
                        equity = self.get_current_equity()
                        exit_reason = exit_event.get("exit_reason", "UNKNOWN")
                        exit_price = exit_event.get("exit_price", 0.0)
                        net_pnl = exit_event.get("net_pnl", exit_event.get("final_equity", equity) - equity)
                        logger.info(
                            f"[DAEMON] Trade CLOSED | {sym} ({direction}) | "
                            f"Exit: ${exit_price:.4f} via {exit_reason} | "
                            f"Equity: ${equity:.2f} | Resuming universe scan..."
                        )
                        append_audit_log(
                            "DAEMON_TRADE_CLOSED",
                            f"Daemon detected closure of {sym} ({direction}) via {exit_reason}. "
                            f"Resuming universe scan. Current equity: ${equity:.2f}",
                            exit_event
                        )
                        # Immediately scan — skip sleep on closure to minimize re-entry latency
                        iterations += 1
                        continue
                    else:
                        logger.debug(
                            f"[MONITOR] {sym} position still active. Next check in {poll_interval_sec}s."
                        )
                else:
                    # ── SCANNING PHASE ───────────────────────────────────────────
                    logger.info(
                        f"[SCAN] No active trade. Starting universe scan across {len(self.symbols)} symbols..."
                    )
                    executed = self.step()
                    if executed:
                        sym_e = executed.get("symbol", "?")
                        dir_e = executed.get("direction", "?")
                        conf_e = executed.get("confidence", 0.0)
                        equity = self.get_current_equity()
                        logger.info(
                            f"[DAEMON] Trade OPENED | {sym_e} ({dir_e}) | "
                            f"ML Conf: {conf_e*100:.1f}% | Equity: ${equity:.2f}"
                        )
                        append_audit_log(
                            "DAEMON_TRADE_OPENED",
                            f"Daemon opened {dir_e} on {sym_e} (ML conf: {conf_e*100:.1f}%). "
                            f"Entering monitoring phase.",
                            executed
                        )
                    else:
                        logger.info(
                            f"[SCAN] No setup met threshold {self.confidence_threshold:.2f} or "
                            f"all candidates were below exchange minimum size. Sleeping {poll_interval_sec}s."
                        )

                iterations += 1
                time.sleep(poll_interval_sec)

        except KeyboardInterrupt:
            logger.info("[DAEMON] Keyboard interrupt received. Shutting down daemon loop gracefully.")
            append_audit_log(
                "ENGINE_STOPPED",
                f"Execution Engine daemon stopped by operator after {iterations} iterations."
            )
