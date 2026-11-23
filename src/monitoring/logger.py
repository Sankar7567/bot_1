"""Centralized structured logging with credential redaction and rotation.

Guarantees that API keys, secrets, and signatures are never written to
log files or standard output streams.
"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
from typing import Optional

# Regex patterns matching sensitive values in logs
REDACTION_PATTERNS = [
    (re.compile(r"(api[_-]?key[\s:=]+['\"]?)([A-Za-z0-9_\-]{8,})(['\"]?)", re.IGNORECASE), r"\1***REDACTED***\3"),
    (re.compile(r"(secret[\s:=]+['\"]?)([A-Za-z0-9_\-]{8,})(['\"]?)", re.IGNORECASE), r"\1***REDACTED***\3"),
    (re.compile(r"(signature=)([A-Fa-f0-9]{32,64})", re.IGNORECASE), r"\1***REDACTED***"),
    (re.compile(r"(X-MBX-APIKEY[\s:=]+['\"]?)([A-Za-z0-9_\-]{8,})(['\"]?)", re.IGNORECASE), r"\1***REDACTED***\3"),
]


class RedactingFormatter(logging.Formatter):
    """Custom logging formatter that strips sensitive secrets from log records."""

    def format(self, record: logging.LogRecord) -> str:
        original = super().format(record)
        sanitized = original
        for pattern, replacement in REDACTION_PATTERNS:
            sanitized = pattern.sub(replacement, sanitized)
        return sanitized


def setup_logger(
    name: str = "binance_ha_bot",
    log_level: str = "INFO",
    log_file: Optional[Path] = None,
    max_bytes: int = 10 * 1024 * 1024,  # 10 MB
    backup_count: int = 5,
    enable_console: bool = True,
) -> logging.Logger:
    """Configures and returns a sanitized, structured logger.

    Args:
        name: Name of the logger.
        log_level: Log severity level string (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        log_file: Optional path to log file. If provided, creates rotating file handler.
        max_bytes: Maximum size of a log file before rotation.
        backup_count: Number of rotated backups to keep.
        enable_console: Whether to attach a stdout console handler.

    Returns:
        logging.Logger: Configured logger instance.
    """
    logger = logging.getLogger(name)
    level = getattr(logging, log_level.upper(), logging.INFO)
    logger.setLevel(level)

    # Avoid duplicate handlers if setup_logger is called repeatedly
    if logger.handlers:
        logger.handlers.clear()

    formatter = RedactingFormatter(
        fmt="%(asctime)s UTC [%(levelname)s] [%(name)s:%(funcName)s:%(lineno)d] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if enable_console:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(level)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            filename=str(log_path),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    logger.propagate = False
    return logger


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Retrieves an existing logger or child logger under the root namespace."""
    base_name = "binance_ha_bot"
    if name:
        if name.startswith(base_name):
            return logging.getLogger(name)
        return logging.getLogger(f"{base_name}.{name}")
    return logging.getLogger(base_name)
