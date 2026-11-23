"""Unit tests for configuration management."""

import pytest
from pydantic import ValidationError
from src.config.settings import Settings, load_settings


class TestSettings:
    """Test suite verifying configuration validation and secret safeguards."""

    def test_default_settings(self):
        """Verify standard default configuration matches project specification."""
        settings = Settings()

        assert settings.app_env == "testnet"
        assert settings.is_testnet is True
        assert settings.nominal_risk_ratio == 0.10
        assert settings.reward_risk_ratio == 3.0
        assert settings.swing_confirmation_bars == 5
        assert settings.swing_stop_buffer_ratio == 0.001
        assert settings.global_max_positions == 1
        assert settings.default_margin_type == "ISOLATED"
        assert settings.scanner_top_n == 20
        assert settings.scanner_refresh_interval_sec == 60
        assert settings.timeframe_trigger == "15m"
        assert settings.timeframe_confirmation == "1m"

    def test_secret_masking(self):
        """Verify API keys and secrets are masked and never exposed in safe dict or repr."""
        raw_key = "test_binance_api_key_123456"
        raw_secret = "test_binance_secret_key_789012"

        settings = Settings(
            binance_api_key=raw_key,
            binance_api_secret=raw_secret,
        )

        # Direct secret access works when explicitly requested
        assert settings.binance_api_key.get_secret_value() == raw_key
        assert settings.binance_api_secret.get_secret_value() == raw_secret

        # String representation masks the values
        assert raw_key not in str(settings.binance_api_key)
        assert raw_secret not in str(settings.binance_api_secret)

        # Safe dictionary representation masks the values
        safe_dict = settings.get_safe_dict()
        assert safe_dict["binance_api_key"] == "***REDACTED***"
        assert safe_dict["binance_api_secret"] == "***REDACTED***"

    def test_risk_ratio_validation(self):
        """Verify nominal_risk_ratio rejects invalid out-of-bounds values."""
        # Valid boundary
        settings = Settings(nominal_risk_ratio=0.20)
        assert settings.nominal_risk_ratio == 0.20

        # Reject negative or zero
        with pytest.raises(ValidationError):
            Settings(nominal_risk_ratio=0.0)

        with pytest.raises(ValidationError):
            Settings(nominal_risk_ratio=-0.05)

        # Reject dangerously high risk (> 25%)
        with pytest.raises(ValidationError):
            Settings(nominal_risk_ratio=0.30)

    def test_reward_risk_ratio_validation(self):
        """Verify reward_risk_ratio must be at least 1.0."""
        with pytest.raises(ValidationError):
            Settings(reward_risk_ratio=0.5)

        settings = Settings(reward_risk_ratio=3.0)
        assert settings.reward_risk_ratio == 3.0

    def test_swing_confirmation_bars_validation(self):
        """Verify swing_confirmation_bars requires at least 2 bars."""
        with pytest.raises(ValidationError):
            Settings(swing_confirmation_bars=1)

        settings = Settings(swing_confirmation_bars=5)
        assert settings.swing_confirmation_bars == 5

    def test_global_max_positions_validation(self):
        """Verify global_max_positions strictly enforces exactly 1 position."""
        with pytest.raises(ValidationError):
            Settings(global_max_positions=2)

        with pytest.raises(ValidationError):
            Settings(global_max_positions=0)

        settings = Settings(global_max_positions=1)
        assert settings.global_max_positions == 1

    def test_load_settings_factory(self, monkeypatch):
        """Verify factory loader and environment variable overrides."""
        monkeypatch.setenv("NOMINAL_RISK_RATIO", "0.15")
        monkeypatch.setenv("APP_ENV", "testnet")

        settings = load_settings()
        assert settings.nominal_risk_ratio == 0.15
        assert settings.app_env == "testnet"
