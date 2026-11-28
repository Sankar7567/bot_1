"""Main CLI entrypoint for the Binance USD(S)-M Futures HA Trading Bot."""

import argparse
import asyncio
import signal
import sys
from src.config.settings import load_settings
from src.monitoring.logger import setup_logger
from src.orchestrator import TradingBotOrchestrator


async def run_bot(verify_connectivity: bool = True) -> None:
  """Initializes configuration, logging, orchestrator, and signal handlers."""
  settings = load_settings()
  logger = setup_logger(
      log_level=settings.log_level,
      log_file=settings.log_file,
  )

  logger.info(
      "Launching Binance Futures HA Trading Bot [env=%s]", settings.app_env
  )

  orchestrator = TradingBotOrchestrator(settings=settings)
  loop = asyncio.get_running_loop()

  def _handle_signal(sig_name: str) -> None:
    logger.info("Received OS signal %s. Requesting graceful shutdown...", sig_name)
    orchestrator.request_shutdown()

  for sig in (signal.SIGINT, signal.SIGTERM):
    try:
      loop.add_signal_handler(sig, _handle_signal, sig.name)
    except NotImplementedError:
      pass

  try:
    await orchestrator.startup(
        verify_connectivity=verify_connectivity,
        start_streams=True,
    )
    await orchestrator.run_forever()
  finally:
    await orchestrator.shutdown()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(
      description="Binance USD(S)-M Futures Heikin-Ashi Automated Trading Bot"
  )
  parser.add_argument(
      "--check-config",
      action="store_true",
      help="Validate configuration and print sanitized settings then exit.",
  )
  parser.add_argument(
      "--ui",
      action="store_true",
      help="Launch the local Trading Control Panel web UI (does not start live trading).",
  )
  parser.add_argument(
      "--port",
      type=int,
      default=8080,
      help="Port for the local Trading Control Panel web UI (default: 8080).",
  )
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  if args.check_config:
    settings = load_settings()
    print(f"Configuration OK (env={settings.app_env}): {settings.get_safe_dict()}")
    return 0

  if args.ui:
    from src.web.server import run_control_panel_server

    settings = load_settings()
    setup_logger(log_level=settings.log_level, log_file=settings.log_file)
    run_control_panel_server(port=args.port, settings=settings)
    return 0

  try:
    asyncio.run(run_bot())
    return 0
  except KeyboardInterrupt:
    return 0
  except Exception as exc:
    print(f"Fatal error: {exc}", file=sys.stderr)
    return 1


if __name__ == "__main__":
  raise SystemExit(main())
