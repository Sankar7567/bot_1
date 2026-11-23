"""HMAC-SHA256 request signing for Binance USDⓈ-M Futures.

Implements query parameter formatting, timestamping, recvWindow validation,
and cryptographic hashing.
"""

import hashlib
import hmac
from typing import Any, Dict
from urllib.parse import urlencode


def calculate_signature(secret: str, query_string: str) -> str:
  """Calculates the HMAC-SHA256 hex digest for a given query string."""
  return hmac.new(
      secret.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256
  ).hexdigest()


def sign_parameters(
    params: Dict[str, Any],
    secret: str,
    timestamp_ms: int,
    recv_window_ms: int = 5000,
) -> Dict[str, Any]:
  """Appends timestamp, recvWindow, and cryptographic signature to parameters.

  Args:
      params: Original request query/body dictionary.
      secret: Binance API secret key string.
      timestamp_ms: Current synchronized UTC millisecond timestamp.
      recv_window_ms: Maximum request latency window in milliseconds.

  Returns:
      Dict[str, Any]: A new dictionary containing original parameters plus
      timestamp,
          recvWindow, and the calculated signature.
  """
  signed_params = {**params}
  signed_params["timestamp"] = timestamp_ms
  if recv_window_ms:
    signed_params["recvWindow"] = recv_window_ms

  # URL encode query string according to RFC 3986
  query_string = urlencode(signed_params)
  signature = calculate_signature(secret, query_string)
  signed_params["signature"] = signature

  return signed_params
