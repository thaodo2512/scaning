# Repository Guidelines

## Project Structure & Module Organization
- Spec: `CryptoStorm_Spec_v2_1.md` (source of truth).
- Proposed layout:
  - `src/cryptostorm/` — modules: `retrieve/`, `feature/`, `backtest/`, `report/`.
  - `configs/` — YAML run configs (Coinglass, symbols, windows).
  - `data/` — raw JSONL per symbol (see spec §2 outputs).
  - `artifacts/<RUN_ID>/` — `alerts/`, `scores/`, `metrics/` (see spec §4).
  - `reports/` — self-contained HTML reports (spec §5).
  - `tests/` — unit/integration/acceptance (spec §6).

## Build, Test, and Development Commands
- Python 3.11+ recommended. Example setup:
  - `python -m venv .venv && source .venv/bin/activate`
  - `pip install -r requirements.txt -r requirements-dev.txt`
- Quality:
  - `ruff check .` — lint; `black .` — format; `mypy src` — type-check.
- Tests:
  - `pytest -q` — run all; `pytest -k retrieve` — subset.
- Reports: open `reports/<SYM>.html` locally (no server needed).

## Coding Style & Naming Conventions
- 4-space indentation, type hints required in `src/`.
- Names: modules/files `snake_case.py`; classes `PascalCase`; functions/vars `snake_case`.
- Package paths mirror spec modules (e.g., `src/cryptostorm/retrieve/coinglass.py`).
- Prefer `logging` over `print`; UTC-only timestamps; no implicit local time.

## Testing Guidelines
- Framework: `pytest`. Files: `tests/test_*.py`; fixtures in `tests/conftest.py`.
- Aim for ≥80% coverage on `feature/` and `backtest/`.
- Add acceptance tests for spec §6 (funding snap times, OB age ≤60s, determinism, offline report rendering).

## Commit & Pull Request Guidelines
- Use Conventional Commits: `feat:`, `fix:`, `docs:`, `refactor:`, `chore:`.
- PRs include: scope, linked issues, config used, sample `run_id`, and screenshots of report markers aligning with scores.
- Update `AGENTS.md` and spec references if behavior changes.

## Security & Configuration Tips
- Keep secrets in env vars (e.g., `COINGLASS_API_KEY`); never commit keys.
- Respect Coinglass rate limits; implement exponential backoff per spec.

## Agent-Specific Instructions
- Treat the spec as normative. Keep patches minimal and scoped.
- Create new code under the paths above; avoid unrelated refactors.
- Prefer `rg` for search; read files in ≤250-line chunks.
- Do not add dependencies without justification; update docs when adding files.
