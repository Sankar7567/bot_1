"""Safe Live Trading Controller for the Trading Control Panel (Section 7).

Wraps TradingBotOrchestrator with strict startup safety gates:
- Disabled by default; never starts on app boot.
- Never auto-switches from testnet to production.
- Requires explicit target_env + separate production confirmation phrase.
- Refuses to start when credentials are missing or preflight checks fail.
- Prevents duplicate concurrent starts.
- Stopping halts new entries while preserving protective SL/TP on open positions.
- Separate explicitly confirmed emergency-close using PositionManager.
"""

import asyncio
import time
from typing import Any, Callable, Dict, Optional
from src.config.settings import Settings, load_settings
from src.exchange.client import BinanceFuturesClient
from src.monitoring.health import SystemHealthChecker
from src.monitoring.logger import get_logger
from src.orchestrator import TradingBotOrchestrator
from src.state.database import DatabaseManager
from src.state.repository import StateRepository

logger = get_logger("live_controller")

PRODUCTION_CONFIRMATION_PHRASE = "ENABLE LIVE TRADING"


class LiveTradingController:
  """Manages lifecycle, safety gates, and status inspection for TradingBotOrchestrator."""

  def __init__(
      self,
      settings: Optional[Settings] = None,
      orchestrator_factory: Optional[Callable[[Settings], TradingBotOrchestrator]] = None,
      health_checker_factory: Optional[Callable[[Settings], SystemHealthChecker]] = None,
  ):
    self.base_settings = settings or load_settings()
    self._orchestrator_factory = orchestrator_factory or (
        lambda s: TradingBotOrchestrator(settings=s)
    )
    self._health_checker_factory = health_checker_factory or (
        lambda s: SystemHealthChecker(settings=s)
    )

    self._lock = asyncio.Lock()
    self._orchestrator: Optional[TradingBotOrchestrator] = None
    self._run_task: Optional[asyncio.Task] = None
    self._state: str = "stopped"  # stopped | starting | running | stopping | error
    self._active_env: str = self.base_settings.app_env
    self._active_strategy: str = self.base_settings.strategy_name
    self._started_at_ms: Optional[int] = None
    self._last_error: Optional[str] = None
    self._last_reconciliation: Optional[str] = None

  def _has_credentials(self, cfg: Settings) -> bool:
    return bool(
        cfg.binance_api_key.get_secret_value()
        and cfg.binance_api_secret.get_secret_value()
    )

  def _build_env_settings(
      self, target_env: str, strategy_name: Optional[str] = None
  ) -> Settings:
    """Builds settings for the explicitly requested environment without mutating defaults."""
    if target_env not in ("testnet", "production"):
      raise ValueError(f"Unsupported environment: {target_env}")

    if target_env == "testnet":
      rest_url = (
          self.base_settings.binance_rest_base_url
          if self.base_settings.app_env == "testnet"
          else "https://testnet.binancefuture.com"
      )
      ws_url = (
          self.base_settings.binance_ws_base_url
          if self.base_settings.app_env == "testnet"
          else "wss://stream.binancefuture.com/ws"
      )
    else:
      rest_url = (
          self.base_settings.binance_rest_base_url
          if self.base_settings.app_env == "production"
          else "https://fapi.binance.com"
      )
      ws_url = (
          self.base_settings.binance_ws_base_url
          if self.base_settings.app_env == "production"
          else "wss://fstream.binance.com/ws"
      )

    updates: Dict[str, Any] = {
        "app_env": target_env,
        "binance_rest_base_url": rest_url,
        "binance_ws_base_url": ws_url,
    }
    if strategy_name in ("top_gainers_ha", "momentum_breakout"):
      updates["strategy_name"] = strategy_name
    return self.base_settings.model_copy(update=updates)

  def is_running(self) -> bool:
    return self._state in ("starting", "running")

  async def start_trading(
      self,
      target_env: str = "testnet",
      confirm_production: bool = False,
      confirmation_phrase: str = "",
      strategy_name: str = "top_gainers_ha",
  ) -> Dict[str, Any]:
    """Starts the existing TradingBotOrchestrator after enforcing all safety gates."""
    async with self._lock:
      if self._state in ("starting", "running"):
        raise RuntimeError("Live trading is already running or starting.")

      # Gate 1: Explicit environment & production confirmation
      if target_env == "production":
        if not confirm_production or confirmation_phrase.strip() != PRODUCTION_CONFIRMATION_PHRASE:
          raise PermissionError(
              f"Production live trading requires explicit confirmation and phrase "
              f"'{PRODUCTION_CONFIRMATION_PHRASE}'."
          )

      cfg = self._build_env_settings(target_env, strategy_name=strategy_name)
      self._active_strategy = cfg.strategy_name

      # Gate 2: API credentials must exist
      if not self._has_credentials(cfg):
        raise PermissionError(
            "Binance API credentials (BINANCE_API_KEY / BINANCE_API_SECRET) are not configured. "
            "Live trading remains disabled."
        )

      self._state = "starting"
      self._last_error = None
      self._active_env = target_env

      try:
        # Gate 3: Preflight system health check
        checker = self._health_checker_factory(cfg)
        health = await checker.run_all_checks()
        if not health.get("can_live_trade", False):
          failed_names = [
              c["name"] for c in health.get("checks", []) if c["status"] == "Failed"
          ]
          raise RuntimeError(
              f"Preflight health check failed: {', '.join(failed_names) or 'missing requirements'}"
          )

        # Gate 4: Start existing TradingBotOrchestrator
        orch = self._orchestrator_factory(cfg)
        report = await orch.startup(verify_connectivity=True, start_streams=True)
        self._orchestrator = orch
        self._last_reconciliation = report.status.value
        self._started_at_ms = int(time.time() * 1000)
        self._state = "running"

        self._run_task = asyncio.create_task(
            self._supervise_orchestrator(orch),
            name=f"LiveTrading-{target_env}",
        )
        logger.info("LiveTradingController started in %s mode.", target_env)
        return await self.get_status()
      except Exception as exc:
        self._state = "error"
        self._last_error = str(exc)
        if self._orchestrator is not None:
          try:
            await self._orchestrator.shutdown()
          except Exception:
            pass
          self._orchestrator = None
        raise

  async def _supervise_orchestrator(self, orch: TradingBotOrchestrator) -> None:
    try:
      await orch.run_forever()
    except asyncio.CancelledError:
      pass
    except Exception as exc:
      logger.error("Orchestrator loop terminated with error: %s", exc, exc_info=True)
      self._last_error = str(exc)
      self._state = "error"
    finally:
      await orch.shutdown()
      if self._state != "error":
        self._state = "stopped"

  async def stop_trading(self) -> Dict[str, Any]:
    """Stops new entries/scanning while leaving any open position's exchange SL/TP intact."""
    async with self._lock:
      if self._orchestrator is None or self._state == "stopped":
        self._state = "stopped"
        return await self.get_status()

      self._state = "stopping"
      orch = self._orchestrator
      cur_pos = orch.position_manager.get_current_position()
      if cur_pos and cur_pos.is_active:
        logger.warning(
            "Stopping strategy while position %s (%s) is OPEN. "
            "Exchange-side SL/TP protective orders remain active.",
            cur_pos.position_id,
            cur_pos.symbol,
        )

      orch.request_shutdown()
      if self._run_task is not None:
        try:
          await asyncio.wait_for(self._run_task, timeout=10.0)
        except asyncio.TimeoutError:
          self._run_task.cancel()
      self._run_task = None
      self._orchestrator = None
      self._state = "stopped"
      return await self.get_status()

  async def emergency_close(self, confirm_emergency: bool = False) -> Dict[str, Any]:
    """Explicitly confirmed emergency market close via existing PositionManager."""
    if not confirm_emergency:
      raise PermissionError("Emergency close requires explicit confirmation.")

    async with self._lock:
      if self._orchestrator is not None:
        orch = self._orchestrator
        res = await orch.position_manager.emergency_close_current_position(
            reason="MANUAL_EMERGENCY_UI"
        )
        # Update persistent live trade ledger
        open_pos = orch.repository.get_open_position()
        if open_pos is None:
          # Sync any recently closed positions
          orch.repository.sync_live_trades_from_positions(
              environment=self._active_env
          )
        return {
            "closed": res.is_closed,
            "symbol": res.symbol,
            "order_id": res.order_id,
            "message": "Emergency market close executed.",
        }

      # Fallback if orchestrator is stopped but DB shows an open position and creds exist
      cfg = self._build_env_settings(self._active_env)
      if not self._has_credentials(cfg):
        raise PermissionError("Cannot emergency close without API credentials.")

      db = DatabaseManager(cfg.db_file_path)
      db.initialize()
      client = BinanceFuturesClient(cfg)
      try:
        repo = StateRepository(db)
        open_rec = repo.get_open_position()
        if open_rec is None:
          return {"closed": False, "message": "No open bot-managed position found."}

        from src.execution.order_router import OrderRouter
        from src.positions.manager import PositionManager
        from src.positions.protective import ProtectiveOrderManager

        pm = PositionManager(
            exchange_client=client,
            repository=repo,
            order_router=OrderRouter(client),
            protective_manager=ProtectiveOrderManager(client),
        )
        res = await pm.emergency_close_current_position(reason="MANUAL_EMERGENCY_UI")
        repo.upsert_live_trade_from_position(
            open_rec.position_id, environment=self._active_env
        )
        return {
            "closed": res.is_closed,
            "symbol": res.symbol,
            "order_id": res.order_id,
            "message": "Emergency market close executed.",
        }
      finally:
        await client.close()
        db.close()

  async def get_status(self) -> Dict[str, Any]:
    """Returns live trading state, balances (if authed), and active position details."""
    cfg = self._build_env_settings(self._active_env)
    has_creds = self._has_credentials(cfg)

    account_balance: Optional[float] = None
    available_balance: Optional[float] = None
    exchange_connected = False
    open_position_info: Optional[Dict[str, Any]] = None

    # Check active orchestrator or persistent DB for open position
    if self._orchestrator is not None:
      pos = self._orchestrator.position_manager.get_current_position()
      if pos and pos.is_active:
        pair = pos.protective_pair
        open_position_info = {
            "position_id": pos.position_id,
            "symbol": pos.symbol,
            "direction": "LONG" if pos.side.value == "BUY" else "SHORT",
            "entry_price": pos.entry_price,
            "quantity": pos.quantity,
            "stop_loss": pos.stop_loss_price,
            "take_profit": pos.take_profit_price,
            "leverage": pos.leverage,
            "unrealized_pnl": None,
            "protective_status": (
                f"SL={pair.sl_client_order_id}, TP={pair.tp_client_order_id}"
                if pair
                else "UNKNOWN"
            ),
        }
    else:
      db = DatabaseManager(cfg.db_file_path)
      try:
        db.initialize()
        repo = StateRepository(db)
        rec = repo.get_open_position()
        if rec is not None:
          open_position_info = {
              "position_id": rec.position_id,
              "symbol": rec.symbol,
              "direction": "LONG" if rec.side.upper() == "BUY" else "SHORT",
              "entry_price": rec.entry_price,
              "quantity": rec.quantity,
              "stop_loss": rec.stop_loss_price,
              "take_profit": rec.take_profit_price,
              "leverage": rec.leverage,
              "unrealized_pnl": None,
              "protective_status": (
                  f"SL={rec.sl_client_order_id}, TP={rec.tp_client_order_id}"
                  if rec.sl_client_order_id or rec.tp_client_order_id
                  else "PERSISTED"
              ),
          }
      finally:
        db.close()

    # Query account/position unrealized PnL only when credentials are configured
    if has_creds and self._orchestrator is not None:
      try:
        acct = await self._orchestrator.client.get_account_info()
        account_balance = acct.total_margin_balance
        available_balance = acct.available_balance
        exchange_connected = True
        if open_position_info is not None:
          ex_pos = await self._orchestrator.client.get_positions(
              open_position_info["symbol"]
          )
          for p in ex_pos:
            if p.symbol == open_position_info["symbol"] and p.is_open:
              open_position_info["unrealized_pnl"] = p.unrealized_profit
              break
      except Exception as exc:
        logger.debug("Could not refresh live account info: %s", exc)

    return {
        "state": self._state,
        "environment": self._active_env,
        "strategy_name": self._active_strategy,
        "credentials_configured": has_creds,
        "live_enabled": has_creds,
        "missing_credentials_message": (
            None
            if has_creds
            else "BINANCE_API_KEY and BINANCE_API_SECRET are not configured in .env. "
            "Live trading controls are disabled."
        ),
        "exchange_connected": exchange_connected,
        "started_at_ms": self._started_at_ms,
        "last_reconciliation": self._last_reconciliation,
        "last_error": self._last_error,
        "account_balance": account_balance,
        "available_balance": available_balance,
        "open_position": open_position_info,
        "production_confirmation_phrase": PRODUCTION_CONFIRMATION_PHRASE,
    }
