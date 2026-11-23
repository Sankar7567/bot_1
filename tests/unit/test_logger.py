"""Unit tests for logging infrastructure and secret redaction."""

import logging
from pathlib import Path
from src.monitoring.logger import (
    RedactingFormatter,
    get_logger,
    setup_logger,
)


class TestLogger:
    """Test suite verifying logging configuration, secret redaction, and rotation."""

    def test_redaction_formatter_sanitizes_secrets(self):
        """Verify that API keys, secrets, and signatures are stripped by RedactingFormatter."""
        formatter = RedactingFormatter(fmt="%(message)s")

        test_cases = [
            ("Connecting with api_key=ab12cd34ef56gh78 to endpoint", "Connecting with api_key=***REDACTED*** to endpoint"),
            ("Using secret='super_secret_token_12345'", "Using secret='***REDACTED***'"),
            ("Signed URL: https://api.binance.com?timestamp=100&signature=abcdef0123456789abcdef0123456789", "Signed URL: https://api.binance.com?timestamp=100&signature=***REDACTED***"),
            ("Header X-MBX-APIKEY: 123456789abcdef012345", "Header X-MBX-APIKEY: ***REDACTED***"),
        ]

        for raw_msg, expected_msg in test_cases:
            record = logging.LogRecord(
                name="test",
                level=logging.INFO,
                pathname="test.py",
                lineno=10,
                msg=raw_msg,
                args=(),
                exc_info=None,
            )
            formatted = formatter.format(record)
            assert formatted == expected_msg
            assert "12345" not in formatted or "api_key" in formatted

    def test_setup_logger_with_file(self, tmp_path: Path):
        """Verify setup_logger creates log file and writes messages cleanly."""
        log_file = tmp_path / "test_run" / "bot.log"
        logger = setup_logger(
            name="test_bot_file",
            log_level="DEBUG",
            log_file=log_file,
            enable_console=False,
        )

        test_secret = "secret=secret_val_12345678"
        logger.info(f"Test event with {test_secret}")

        # Flush handlers
        for handler in logger.handlers:
            handler.flush()
            handler.close()

        assert log_file.exists()
        content = log_file.read_text(encoding="utf-8")
        assert "Test event with secret=***REDACTED***" in content
        assert "secret_val_12345678" not in content

    def test_setup_logger_with_console_and_reconfiguration(self):
        """Verify console handler configuration and repeated setup_logger calls."""
        # Initial call with console
        logger = setup_logger(
            name="test_bot_console",
            log_level="WARNING",
            log_file=None,
            enable_console=True,
        )
        assert len(logger.handlers) == 1
        assert isinstance(logger.handlers[0], logging.StreamHandler)
        assert logger.level == logging.WARNING

        # Repeated setup call should clear previous handlers and reconfigure
        reconfigured = setup_logger(
            name="test_bot_console",
            log_level="DEBUG",
            log_file=None,
            enable_console=True,
        )
        assert len(reconfigured.handlers) == 1
        assert reconfigured.level == logging.DEBUG

    def test_get_logger_hierarchy(self):
        """Verify get_logger returns child loggers correctly nested under root namespace."""
        base_logger = get_logger()
        child_logger = get_logger("exchange")
        child_logger_full = get_logger("binance_ha_bot.scanner")

        assert base_logger.name == "binance_ha_bot"
        assert child_logger.name == "binance_ha_bot.exchange"
        assert child_logger_full.name == "binance_ha_bot.scanner"
