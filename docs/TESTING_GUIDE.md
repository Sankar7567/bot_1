# Testing Guide

Configured in [pyproject.toml](../pyproject.toml).

---

## 1. Prerequisites
Install development dependencies:
```bash
source .venv/bin/activate
pip install -r requirements-dev.txt
```

## 2. Test Commands

| Suite | Command | Network / Credentials |
|---|---|---|
| **Full Offline Suite + Branch Coverage** | `pytest --cov=src --cov-fail-under=80 -q` | Offline (`@pytest.mark.testnet` skips without keys) |
| **Unit Tests Only** | `pytest tests/unit/ --no-cov -v` | Offline |
| **Integration & Fault Injection** | `pytest tests/integration/ --no-cov -v` | Offline (public ping hits Testnet; auth tests skip without keys) |
| **Chronological Backtesting (17 Invariants)** | `pytest tests/unit/test_backtesting_chronological.py --no-cov -v` | Offline |
| **End-to-End Lifecycle & Fault Injection** | `pytest tests/integration/test_e2e_lifecycle.py tests/integration/test_fault_injection.py --no-cov -v` | Offline |
| **Live Binance Testnet Suite** | `pytest -m testnet --no-cov -v` | Requires `BINANCE_API_KEY` & `BINANCE_API_SECRET` |

## 3. Verified Baseline Results
On the current codebase (`v1.0.0`):
- **Collected:** 283 tests
- **Executed & Passed (offline):** **280 passed**
- **Skipped:** **3 skipped** (`test_exchange_integration.py`, `test_execution_integration.py`, `test_testnet_operational.py` — gated on Testnet API credentials)
- **Failing:** **0**
- **Branch Coverage:** **86.71%** across `src/` (exceeding the 80% `fail_under` threshold in `pyproject.toml`).

> [!NOTE]
> **Definitions:** *Written/Collected* = all test functions discovered by `pytest`; *Executed* = tests actually run (not deselected or skipped); *Passed* = assertions succeeded; *Skipped* = intentionally bypassed (e.g. missing Testnet credentials); *Failing* = assertion or runtime error.
