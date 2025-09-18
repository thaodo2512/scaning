# Repository Guidelines

Spec: `CryptoStorm_Spec_v2_1.md` is the source of truth.

## Project Structure & Module Organization
- `src/cryptostorm/` — modules: `retrieve/`, `feature/`, `backtest/`, `report/` (paths mirror the spec; e.g., `src/cryptostorm/retrieve/coinglass.py`).
- `configs/` — YAML run configs (symbols, windows, Coinglass, realtime).
- `data/` — raw JSONL by symbol; sidecar state under `data/<SYM>/.state/`.
- `artifacts/<RUN_ID>/` — `alerts/`, `scores/`, `metrics/`, `models/`.
- `reports/` — self‑contained HTML reports.
- `tests/` — unit/integration/acceptance.

## Build, Test, and Development Commands
- Python 3.11+. Setup: `python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt -r requirements-dev.txt`.
- Quality: `ruff check .` (lint), `black .` (format), `mypy src` (types).
- Tests: `pytest -q` (all) or `pytest -k retrieve` (subset).
- E2E (15m default): `bash scripts/run_e2e.sh -c configs/realtime.yaml`.
- Open reports locally: `reports/<SYM>.html`.

## Coding Style & Naming Conventions
- 4‑space indentation; type hints required in `src/`.
- Filenames/modules `snake_case.py`; classes `PascalCase`; functions/vars `snake_case`.
- Use `logging` (no `print`); timestamps are UTC‑only.

## Testing Guidelines
- Framework: `pytest`; files `tests/test_*.py`; fixtures in `tests/conftest.py`.
- Coverage target ≥80% for `feature/` and `backtest/`.
- Acceptance (spec §6): funding snap times, order‑book age ≤60s, determinism, offline report rendering.

## Commit & Pull Request Guidelines
- Conventional Commits: `feat:`, `fix:`, `docs:`, `refactor:`, `chore:`.
- PRs include scope, linked issues, config used, sample `run_id`, and screenshots showing report markers aligned with scores. Update this file and spec references if behavior changes.

## Security & Configuration Tips
- Never commit secrets. Use env vars (e.g., `COINGLASS_API_KEY`) or `secrets/` files for Telegram.
- Respect Coinglass rate limits; use the global RPS limiter.
- Strict empties: set `CRYPTOSTORM_STRICT_EMPTY=1`; HTTP debug: `CRYPTOSTORM_HTTP_DEBUG=all`.
- Plotly offline: place `vendor/plotly-2.32.0.min.js` or `reports/vendor/`.

## Realtime & CLI Quickstart
- 15m cadence by default; single cycle: `python -m cryptostorm realtime configs/realtime.yaml --once --bar-interval 15m`.
- Feature update‑last: `python -m cryptostorm feature <config> --interval 15m --update-last`.

## Agent‑Specific Instructions
- Treat the spec as normative; keep patches minimal and scoped; prefer `rg` for search; read files in ≤250‑line chunks; avoid new deps without justification; update docs when adding files.

