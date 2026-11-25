# Configuration Guide

All runtime configuration is managed by `Settings` (`pydantic_settings.BaseSettings`) in [src/config/settings.py](../src/config/settings.py).

## Loading Behavior
- Reads variables from `.env` in the repository root (`env_file=".env"`, UTF-8, case-insensitive, extra keys ignored).
- Real OS environment variables override `.env`.
- **Restart Required:** Yes. Settings are loaded once at startup (`load_settings()` in `src/main.py`). Changes to `.env` take effect only after restarting the bot.

---

## Complete Configuration Reference

### 1. Environment & Endpoints
| Env Variable | Type | Default | Valid Values / Notes |
|---|---|---|---|
| `APP_ENV` | `str` | `testnet` | `"testnet"` or `"production"`. **Defaults to `testnet`.** |
| `BINANCE_API_KEY` | `SecretStr` | `""` | Binance Futures API key (masked in logs and `get_safe_dict()`). |
| `BINANCE_API_SECRET` | `SecretStr` | `""` | Binance Futures API secret (masked in logs and `get_safe_dict()`). |
| `BINANCE_REST_BASE_URL` | `str` | `https://testnet.binancefuture.com` | Use `https://fapi.binance.com` only for production. |
| `BINANCE_WS_BASE_URL` | `str` | `wss://stream.binancefuture.com/ws` | Use `wss://fstream.binance.com/ws` only for production. |

### 2. Strategy Timeframes
| Env Variable | Type | Default | Notes |
|---|---|---|---|
| `TIMEFRAME_TRIGGER` | `str` | `15m` | Completed Heikin-Ashi directional trigger timeframe. |
| `TIMEFRAME_CONFIRMATION` | `str` | `1m` | Completed Heikin-Ashi confirmation timeframe. |

### 3. Risk Management
| Env Variable | Type | Default | Validator Constraint |
|---|---|---|---|
| `NOMINAL_RISK_RATIO` | `float` | `0.10` | `0.0 < v <= 0.25` (0.10 = 10% equity risk budget). |
| `REWARD_RISK_RATIO` | `float` | `3.0` | `v >= 1.0` (3.0 = 3:1 gross reward-to-risk). |
| `SWING_CONFIRMATION_BARS` | `int` | `5` | `v >= 2` (5 bars left & 5 bars right for 5L/5R pivot). |
| `SWING_STOP_BUFFER_RATIO` | `float` | `0.001` | Buffer beyond pivot (0.001 = 0.1%). |
| `ESTIMATED_TAKER_FEE_RATIO` | `float` | `0.0005` | Taker fee fraction for sizing (0.0005 = 0.05%). |
| `ESTIMATED_SLIPPAGE_RATIO` | `float` | `0.0005` | Slippage allowance for sizing (0.0005 = 0.05%). |

### 4. Market Scanner
| Env Variable | Type | Default | Notes |
|---|---|---|---|
| `SCANNER_REFRESH_INTERVAL_SEC` | `int` | `60` | Universe refresh interval in seconds. |
| `SCANNER_TOP_N` | `int` | `20` | Number of top 24h gainers to select. |
| `SCANNER_MIN_QUOTE_VOLUME_24H` | `float` | `1000000.0` | Minimum 24h quote volume in USDT. |
| `SCANNER_QUOTE_ASSET` | `str` | `USDT` | Quote asset filter. |
| `SCANNER_CONTRACT_TYPE` | `str` | `PERPETUAL` | Contract type filter. |

### 5. Execution Safeguards
| Env Variable | Type | Default | Validator Constraint |
|---|---|---|---|
| `GLOBAL_MAX_POSITIONS` | `int` | `1` | Strictly `v == 1` (enforced by validator). |
| `DEFAULT_MARGIN_TYPE` | `str` | `ISOLATED` | `"ISOLATED"` or `"CROSSED"`. |
| `USE_MAX_LEVERAGE` | `bool` | `true` | Apply maximum eligible bracket leverage. |

### 6. Storage, Logging & Network
| Env Variable | Type | Default | Notes |
|---|---|---|---|
| `DB_FILE_PATH` | `Path` | `data/trading_bot.db` | SQLite database file path. |
| `LOG_LEVEL` | `str` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |
| `LOG_DIR` | `Path` | `logs` | Directory for log files. |
| `LOG_FILE` | `Path` | `logs/trading_bot.log` | Rotating log file path (10 MB max, 5 backups). |
| `HTTP_TIMEOUT_SECONDS` | `float` | `10.0` | REST HTTP timeout. |
| `HTTP_MAX_RETRIES` | `int` | `3` | Retry attempts on transient network/5xx errors. |
| `HTTP_RETRY_BACKOFF_FACTOR` | `float` | `1.5` | Exponential backoff multiplier. |

> [!NOTE]
> `.env.example` also includes `DATABASE_URL`, `LOG_ROTATION`, and `LOG_RETENTION_DAYS` as informational template keys; `Settings` ignores extra keys (`extra="ignore"`) and uses `DB_FILE_PATH` and `LOG_FILE` directly.

## Validating Configuration
Always verify your `.env` before starting the bot:
```bash
python -m src.main --check-config
```
