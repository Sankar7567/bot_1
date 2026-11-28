"""Configuration management module for Binance Futures HA Trading Bot.

Uses Pydantic Settings for rigorous type validation, range enforcement,
and secret protection.
"""

from pathlib import Path
from typing import Any, Dict, Literal
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
  """Application configuration settings with strict type and range validations."""

  model_config = SettingsConfigDict(
      env_file=".env",
      env_file_encoding="utf-8",
      case_sensitive=False,
      extra="ignore",
  )

  # Operational Environment
  app_env: Literal["testnet", "production"] = Field(
      default="testnet",
      description="Operational mode. Development must strictly use 'testnet'.",
  )

  # Binance Futures API Credentials
  binance_api_key: SecretStr = Field(
      default=SecretStr(""),
      description="Binance Futures API Key. Masked in logs.",
  )
  binance_api_secret: SecretStr = Field(
      default=SecretStr(""),
      description="Binance Futures API Secret. Masked in logs.",
  )

  # Binance Endpoints
  binance_rest_base_url: str = Field(
      default="https://testnet.binancefuture.com",
      description="REST API base URL.",
  )
  binance_ws_base_url: str = Field(
      default="wss://stream.binancefuture.com/ws",
      description="WebSocket stream base URL.",
  )

  # Strategy Timeframes
  strategy_name: Literal["top_gainers_ha", "momentum_breakout"] = Field(
      default="top_gainers_ha",
      description="Active scanner/strategy workflow ('top_gainers_ha' or 'momentum_breakout').",
  )
  timeframe_trigger: str = Field(
      default="15m",
      description="Directional trigger timeframe (completed Heikin-Ashi).",
  )
  timeframe_confirmation: str = Field(
      default="1m",
      description="Confirmation timeframe (first subsequent completed HA).",
  )

  # Risk Management Parameters
  nominal_risk_ratio: float = Field(
      default=0.10,
      description="Nominal risk budget fraction of total equity per trade (0.10 = 10%).",
  )
  reward_risk_ratio: float = Field(
      default=3.0,
      description="Target reward to risk ratio (3.0 = 3:1 RR).",
  )
  swing_confirmation_bars: int = Field(
      default=5,
      description="Number of bars required left and right for swing pivot confirmation.",
  )
  swing_stop_buffer_ratio: float = Field(
      default=0.001,
      description="Stop-loss price buffer beyond swing pivot (0.001 = 0.1%).",
  )
  estimated_taker_fee_ratio: float = Field(
      default=0.0005,
      description="Estimated taker fee fraction for trade sizing (0.0005 = 0.05%).",
  )
  estimated_slippage_ratio: float = Field(
      default=0.0005,
      description="Estimated execution slippage allowance for trade sizing.",
  )

  # Market Scanner Configuration
  scanner_refresh_interval_sec: int = Field(
      default=60,
      description="Universe scan refresh interval in seconds.",
  )
  scanner_top_n: int = Field(
      default=20,
      description="Number of top 24h percentage gainers to select.",
  )
  scanner_min_quote_volume_24h: float = Field(
      default=1000000.0,
      description="Minimum 24h quote volume (USDT) to qualify for universe.",
  )
  scanner_quote_asset: str = Field(
      default="USDT",
      description="Quote asset for perpetual contracts.",
  )
  scanner_contract_type: str = Field(
      default="PERPETUAL",
      description="Contract type filter.",
  )

  # Execution & Concurrency Safeguards
  global_max_positions: int = Field(
      default=1,
      description="Maximum simultaneous bot-managed positions globally.",
  )
  default_margin_type: Literal["ISOLATED", "CROSSED"] = Field(
      default="ISOLATED",
      description="Margin type for positions (strictly ISOLATED).",
  )
  use_max_leverage: bool = Field(
      default=True,
      description="Apply maximum permitted leverage per symbol bracket.",
  )

  # Storage & Persistence
  db_file_path: Path = Field(
      default=Path("data/trading_bot.db"),
      description="SQLite database path for state persistence.",
  )

  # Logging
  log_level: str = Field(
      default="INFO",
      description="Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL).",
  )
  log_dir: Path = Field(
      default=Path("logs"),
      description="Directory for log files.",
  )
  log_file: Path = Field(
      default=Path("logs/trading_bot.log"),
      description="Path to output log file.",
  )

  # HTTP & Network Resilience
  http_timeout_seconds: float = Field(
      default=10.0,
      description="HTTP request timeout in seconds.",
  )
  http_max_retries: int = Field(
      default=3,
      description="Maximum HTTP request retry attempts on transient network failures.",
  )
  http_retry_backoff_factor: float = Field(
      default=1.5,
      description="Exponential backoff factor for HTTP retries.",
  )

  # Validators
  @field_validator("nominal_risk_ratio")
  @classmethod
  def validate_risk_ratio(cls, v: float) -> float:
    if not (0.0 < v <= 0.25):
      raise ValueError(
          f"nominal_risk_ratio must be between 0.0 and 0.25 (inclusive). Got:"
          f" {v}"
      )
    return v

  @field_validator("reward_risk_ratio")
  @classmethod
  def validate_rr_ratio(cls, v: float) -> float:
    if v < 1.0:
      raise ValueError(f"reward_risk_ratio must be at least 1.0. Got: {v}")
    return v

  @field_validator("swing_confirmation_bars")
  @classmethod
  def validate_swing_bars(cls, v: int) -> int:
    if v < 2:
      raise ValueError(
          f"swing_confirmation_bars must be at least 2. Specification requires"
          f" 5. Got: {v}"
      )
    return v

  @field_validator("global_max_positions")
  @classmethod
  def validate_max_positions(cls, v: int) -> int:
    if v != 1:
      raise ValueError(
          f"global_max_positions must strictly be 1 as per specification. Got:"
          f" {v}"
      )
    return v

  @property
  def is_testnet(self) -> bool:
    """Returns True if operating in testnet mode."""
    return self.app_env.lower() == "testnet"

  def get_safe_dict(self) -> Dict[str, Any]:
    """Returns a dictionary representation with sensitive credentials redacted."""
    data = self.model_dump()
    data["binance_api_key"] = (
        "***REDACTED***" if self.binance_api_key.get_secret_value() else ""
    )
    data["binance_api_secret"] = (
        "***REDACTED***" if self.binance_api_secret.get_secret_value() else ""
    )
    return data


def load_settings(**kwargs: Any) -> Settings:
  """Factory function to load and validate application settings."""
  return Settings(**kwargs)
