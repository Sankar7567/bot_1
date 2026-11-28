"""Minimal local HTTP server for the Binance HA Trading Control Panel.

Uses Python standard library ThreadingHTTPServer bridged to a dedicated asyncio
event loop so long-running backtests, health checks, and live trading run
asynchronously without blocking the UI. Binds strictly to 127.0.0.1.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse
from src.backtesting.service import BacktestService, export_trades_csv
from src.config.settings import Settings, load_settings
from src.live_controller import LiveTradingController
from src.monitoring.health import SystemHealthChecker
from src.monitoring.logger import get_logger
from src.state.database import DatabaseManager
from src.state.repository import StateRepository

logger = get_logger("web.server")

STATIC_DIR = Path(__file__).resolve().parent / "static"


class ControlPanelApp:
  """Coordinates backend services for the HTTP control panel."""

  def __init__(
      self,
      settings: Optional[Settings] = None,
      loop: Optional[asyncio.AbstractEventLoop] = None,
      backtest_service: Optional[BacktestService] = None,
      live_controller: Optional[LiveTradingController] = None,
      health_checker: Optional[SystemHealthChecker] = None,
  ):
    self.settings = settings or load_settings()
    self.loop = loop
    self.backtest_service = backtest_service or BacktestService(
        settings=self.settings
    )
    self.live_controller = live_controller or LiveTradingController(
        settings=self.settings
    )
    self.health_checker = health_checker or SystemHealthChecker(
        settings=self.settings
    )
    self._last_health: Optional[Dict[str, Any]] = None

  def run_coro(self, coro, timeout: float = 30.0) -> Any:
    if self.loop is None:
      raise RuntimeError("Asyncio event loop is not attached to ControlPanelApp.")
    fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
    return fut.result(timeout=timeout)

  async def run_health_check(self) -> Dict[str, Any]:
    res = await self.health_checker.run_all_checks()
    self._last_health = res
    return res

  def get_last_health(self) -> Optional[Dict[str, Any]]:
    return self._last_health

  def get_backtest_defaults(self) -> Dict[str, Any]:
    now_ms = int(time.time() * 1000)
    # Latest fully completed 1m candle
    latest_completed_end = (now_ms // 60_000) * 60_000 - 1
    start_dt = datetime.fromtimestamp(
        latest_completed_end / 1000.0, tz=timezone.utc
    ) - timedelta(days=30)
    end_dt = datetime.fromtimestamp(
        latest_completed_end / 1000.0, tz=timezone.utc
    )
    has_creds = bool(
        self.settings.binance_api_key.get_secret_value()
        and self.settings.binance_api_secret.get_secret_value()
    )
    return {
        "default_start_date": start_dt.strftime("%Y-%m-%d"),
        "default_end_date": end_dt.strftime("%Y-%m-%d"),
        "default_initial_balance": 100.0,
        "has_credentials": has_creds,
        "default_metadata_mode": "RESEARCH_MODE",
        "default_assumed_leverage": 20,
        "default_assumed_mmr": 0.005,
    }

  def list_live_trades(
      self,
      symbol: Optional[str] = None,
      direction: Optional[str] = None,
      status: Optional[str] = None,
  ) -> list[Dict[str, Any]]:
    db = DatabaseManager(self.settings.db_file_path)
    try:
      db.initialize()
      repo = StateRepository(db)
      repo.sync_live_trades_from_positions(environment=self.settings.app_env)
      return [
          t.to_dict()
          for t in repo.list_live_trades(
              symbol=symbol, direction=direction, status=status
          )
      ]
    finally:
      db.close()

  def export_live_trades_csv(self) -> str:
    db = DatabaseManager(self.settings.db_file_path)
    try:
      db.initialize()
      repo = StateRepository(db)
      repo.sync_live_trades_from_positions(environment=self.settings.app_env)
      return repo.export_live_trades_csv()
    finally:
      db.close()


def _parse_date_to_ms(val: Any, is_end: bool = False) -> int:
  """Parses ISO date string (YYYY-MM-DD or ISO8601) or epoch ms into UTC ms."""
  if isinstance(val, (int, float)):
    return int(val)
  s = str(val).strip()
  if len(s) == 10:  # YYYY-MM-DD
    dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if is_end:
      dt = dt + timedelta(days=1)
      ts_ms = int(dt.timestamp() * 1000) - 1
      # Cap to latest completed 1m candle if end date is today or future
      now_ms = int(time.time() * 1000)
      latest_completed = (now_ms // 60_000) * 60_000 - 1
      return min(ts_ms, latest_completed)
    return int(dt.timestamp() * 1000)
  dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
  if dt.tzinfo is None:
    dt = dt.replace(tzinfo=timezone.utc)
  return int(dt.timestamp() * 1000)


def make_handler(app: ControlPanelApp):
  """Creates a BaseHTTPRequestHandler class bound to ControlPanelApp."""

  class ControlPanelHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:
      logger.debug("HTTP %s", format % args)

    def _send_json(self, status: int, payload: Any) -> None:
      body = json.dumps(payload).encode("utf-8")
      self.send_response(status)
      self.send_header("Content-Type", "application/json; charset=utf-8")
      self.send_header("Content-Length", str(len(body)))
      self.send_header("Cache-Control", "no-store")
      self.end_headers()
      self.wfile.write(body)

    def _send_bytes(
        self,
        status: int,
        content_type: str,
        data: bytes,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
      self.send_response(status)
      self.send_header("Content-Type", content_type)
      self.send_header("Content-Length", str(len(data)))
      if extra_headers:
        for k, v in extra_headers.items():
          self.send_header(k, v)
      self.end_headers()
      self.wfile.write(data)

    def _read_json_body(self) -> Dict[str, Any]:
      length = int(self.headers.get("Content-Length", "0") or "0")
      if length <= 0:
        return {}
      raw = self.rfile.read(length)
      return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:
      parsed = urlparse(self.path)
      path = parsed.path
      qs = parse_qs(parsed.query)

      try:
        if path == "/" or path == "/index.html":
          html = (STATIC_DIR / "index.html").read_bytes()
          self._send_bytes(200, "text/html; charset=utf-8", html)
          return

        if path.startswith("/static/"):
          fname = path.removeprefix("/static/")
          if "/" in fname or ".." in fname:
            self._send_json(404, {"error": "Not found"})
            return
          fpath = STATIC_DIR / fname
          if not fpath.is_file():
            self._send_json(404, {"error": "Not found"})
            return
          ctype = (
              "application/javascript; charset=utf-8"
              if fname.endswith(".js")
              else (
                  "text/css; charset=utf-8"
                  if fname.endswith(".css")
                  else "application/octet-stream"
              )
          )
          self._send_bytes(200, ctype, fpath.read_bytes())
          return

        if path == "/api/health":
          self._send_json(200, {"health": app.get_last_health()})
          return

        if path == "/api/backtest/defaults":
          self._send_json(200, app.get_backtest_defaults())
          return

        if path == "/api/backtest/status":
          self._send_json(200, app.backtest_service.get_status())
          return

        if path == "/api/backtest/report":
          rep = app.backtest_service.get_last_report()
          if rep is None:
            self._send_json(404, {"error": "No backtest report available."})
            return
          self._send_json(200, rep)
          return

        if path == "/api/backtest/export.csv":
          rep = app.backtest_service.get_last_report()
          trades = rep.get("trades", []) if rep else []
          csv_text = export_trades_csv(trades).encode("utf-8")
          self._send_bytes(
              200,
              "text/csv; charset=utf-8",
              csv_text,
              {"Content-Disposition": 'attachment; filename="backtest_trades.csv"'},
          )
          return

        if path == "/api/live/status":
          status = app.run_coro(app.live_controller.get_status(), timeout=15.0)
          self._send_json(200, status)
          return

        if path == "/api/live/trades":
          sym = qs.get("symbol", [None])[0] or None
          direction = qs.get("direction", [None])[0] or None
          status_f = qs.get("status", [None])[0] or None
          trades = app.list_live_trades(
              symbol=sym, direction=direction, status=status_f
          )
          self._send_json(200, {"trades": trades})
          return

        if path == "/api/live/trades/export.csv":
          csv_text = app.export_live_trades_csv().encode("utf-8")
          self._send_bytes(
              200,
              "text/csv; charset=utf-8",
              csv_text,
              {"Content-Disposition": 'attachment; filename="live_trades.csv"'},
          )
          return

        self._send_json(404, {"error": f"Unknown endpoint: {path}"})
      except Exception as exc:
        logger.error("GET %s failed: %s", path, exc, exc_info=True)
        self._send_json(500, {"error": str(exc)})

    def do_POST(self) -> None:
      parsed = urlparse(self.path)
      path = parsed.path

      try:
        body = self._read_json_body()

        if path == "/api/health/run":
          res = app.run_coro(app.run_health_check(), timeout=45.0)
          self._send_json(200, res)
          return

        if path == "/api/backtest/start":
          start_ms = _parse_date_to_ms(body["start_date"], is_end=False)
          end_ms = _parse_date_to_ms(body["end_date"], is_end=True)
          initial_balance = float(body.get("initial_balance", 100.0))
          strategy_name = str(body.get("strategy_name", "top_gainers_ha"))
          metadata_mode = str(body.get("metadata_mode", "RESEARCH_MODE"))
          assumed_lev = int(body.get("assumed_max_leverage", 20))
          assumed_mmr = float(body.get("assumed_maint_margin_ratio", 0.005))
          symbols_filter = body.get("symbols_filter") or None
          job_id = app.run_coro(
              app.backtest_service.start_backtest(
                  start_time_ms=start_ms,
                  end_time_ms=end_ms,
                  initial_balance=initial_balance,
                  strategy_name=strategy_name,
                  metadata_mode=metadata_mode,
                  assumed_max_leverage=assumed_lev,
                  assumed_maint_margin_ratio=assumed_mmr,
                  symbols_filter=symbols_filter,
              ),
              timeout=15.0,
          )
          self._send_json(202, {"job_id": job_id})
          return

        if path == "/api/live/start":
          target_env = str(body.get("target_env", "testnet"))
          confirm_prod = bool(body.get("confirm_production", False))
          phrase = str(body.get("confirmation_phrase", ""))
          strategy_name = str(body.get("strategy_name", "top_gainers_ha"))
          status = app.run_coro(
              app.live_controller.start_trading(
                  target_env=target_env,
                  confirm_production=confirm_prod,
                  confirmation_phrase=phrase,
                  strategy_name=strategy_name,
              ),
              timeout=45.0,
          )
          self._send_json(200, status)
          return

        if path == "/api/live/stop":
          status = app.run_coro(app.live_controller.stop_trading(), timeout=20.0)
          self._send_json(200, status)
          return

        if path == "/api/live/emergency-close":
          confirm_emg = bool(body.get("confirm_emergency", False))
          res = app.run_coro(
              app.live_controller.emergency_close(confirm_emergency=confirm_emg),
              timeout=20.0,
          )
          self._send_json(200, res)
          return

        self._send_json(404, {"error": f"Unknown endpoint: {path}"})
      except (ValueError, KeyError) as exc:
        self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
      except PermissionError as exc:
        self._send_json(HTTPStatus.FORBIDDEN, {"error": str(exc)})
      except RuntimeError as exc:
        self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
      except Exception as exc:
        logger.error("POST %s failed: %s", path, exc, exc_info=True)
        self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

  return ControlPanelHandler


def run_control_panel_server(
    host: str = "127.0.0.1",
    port: int = 8080,
    settings: Optional[Settings] = None,
) -> None:
  """Starts the asyncio background loop and HTTP control panel server."""
  loop = asyncio.new_event_loop()
  loop_thread = threading.Thread(
      target=loop.run_forever, name="ControlPanelAsyncLoop", daemon=True
  )
  loop_thread.start()

  app = ControlPanelApp(settings=settings, loop=loop)
  httpd = ThreadingHTTPServer((host, port), make_handler(app))
  logger.info("Trading Control Panel listening at http://%s:%d", host, port)
  print(f"Binance HA Trading Control Panel running at http://{host}:{port}")
  try:
    httpd.serve_forever()
  except KeyboardInterrupt:
    pass
  finally:
    print("\nShutting down Trading Control Panel...")
    try:
      app.run_coro(app.live_controller.stop_trading(), timeout=10.0)
    except Exception:
      pass
    httpd.shutdown()
    httpd.server_close()
    loop.call_soon_threadsafe(loop.stop)
    loop_thread.join(timeout=3.0)
