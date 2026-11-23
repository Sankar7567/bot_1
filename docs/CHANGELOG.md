# Changelog

All notable changes to the Binance Futures HA Trading Bot will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-11-23
### Added
- Phase 01: Project Foundation and Architectural Design.
- Core specification documents:
  - `docs/PROJECT_SPEC.md`
  - `docs/ARCHITECTURE.md`
  - `docs/STRATEGY_RULES.md`
  - `docs/RISK_MODEL.md`
  - `docs/API_CONTRACTS.md`
  - `docs/TEST_PLAN.md`
  - `docs/DEVELOPMENT_LOG.md`
  - `docs/CHANGELOG.md`
  - `docs/RUNBOOK.md`
- Project directory structure for all modules under `src/` and `tests/`.
- Configuration system using Pydantic Settings with automatic secret masking.
- Centralized structured logging with credential sanitization and file rotation.
- Dependency definitions (`requirements.txt`, `requirements-dev.txt`, `pyproject.toml`).
- Git repository initialization and `.gitignore` safety rules.
- Environment template `.env.example`.
- Foundational unit test suite and pytest coverage configuration.
