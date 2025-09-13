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

## Realtime Phases Implemented (v2)

This repository now includes a complete Phase 0–5 realtime implementation with 15m as the default cadence:

- Phase 1 — Incremental Features + Realtime Loop
  - `feature --interval 15m --update-last` appends exactly one 15‑minute row per symbol (5m still supported).
  - `realtime` runs retrieve → features(update-last) → backtest, aligned to `--bar-interval {5m|15m}` (supports `--once`).
  - Script: `scripts/run_e2e.sh` (validate → retrieve → build 15m features → backtest → audit → report).

- Phase 2 — Watch Mode + RPS Limiter + File State
  - `retrieve --watch --workers N --rps R` with global token‑bucket limiter.
  - Per‑file JSON sidecar last_ts at `data/<SYM>/.state/<dataset>.json` to fetch only deltas.

- Phase 3 — Online Scoring + SLOs
  - `backtest --online` scores only the latest row using persisted artifacts.
  - Persists artifacts per symbol under `artifacts/<RUN_ID>/models/`.
  - Realtime SLO metrics appended to `artifacts/<RUN_ID>/metrics/realtime.jsonl`.

- Phase 4 — API & Live UI Hooks
  - `api` subcommand (FastAPI + Uvicorn): endpoints for symbols, scores, alerts, latest score, reports, and `/metrics` (Prometheus text optional).
  - Optional `--token` to require Bearer auth.

- Phase 5 — Storage/Export (optional)
  - `storage export-parquet` (raw JSONL + features CSV → Parquet; requires `pyarrow`).
  - `storage emit-ddl --kind clickhouse|timescale` outputs DDL for raw datasets and features.

## New/Updated CLI Commands

- `python -m cryptostorm realtime <config> [--data data --features features --artifacts <dir> --once --online-scoring --poll-offset-s 10 --jitter-s 2 --send-telegram --telegram-kinds storm,pre_alert --bar-interval 5m|15m]`
- `python -m cryptostorm retrieve <config> --out data --watch --workers 4 --rps 2 --poll-offset-s 10 --jitter-s 2 [--once]`
- `python -m cryptostorm feature <config> --data data --out features [--interval 5m|15m] [--update-last] [--workers N]`
- `python -m cryptostorm backtest <config> --features features [--artifacts-root <dir>] [--online] [--features-interval 5m|15m|auto] [--workers N]`
- `python -m cryptostorm api <config> [--data data --features features --artifacts <dir> --reports reports --host 0.0.0.0 --port 8000 --token TOKEN]`
- `python -m cryptostorm storage export-parquet <config> --data data --features features --out parquet`
- `python -m cryptostorm storage emit-ddl <config> --kind clickhouse|timescale --out ./ddl`
- `python -m cryptostorm binance-top --top 50 [--ai --openai-model gpt-4o-mini --rps 5.0 --out configs/realtime.yaml --print]`
- Reports:
  - Lightweight Charts: `python -m cryptostorm report <config> --data data --features features --out reports`
  - Plotly full: `python -m cryptostorm report-plotly <config> --data data --out reports`
- Plotly price+alerts: `python -m cryptostorm report-price <config> --data data --out reports`

## Reporting Engines
- Default lightweight‑charts report preserved (price + score). OI/Liq panel removed.
- Plotly full reports (`report-plotly`) now render candlesticks + score only (no OI/Liq panel).
  - Offline: place `plotly-2.32.0.min.js` in `vendor/` or `reports/vendor/`.

## Sidecar State
- Last_ts checkpoint per symbol/dataset: `data/<SYM>/.state/<dataset>.json`.
- Retrieval uses this to start at `last_ts+1` (aligned), falling back to JSONL scan if absent.

## Tests Added
- Incremental features append + dedupe (`tests/test_feature_update_last.py`).
- Realtime single‑cycle (`tests/test_realtime.py`).
- Online scoring append (`tests/test_backtest_online.py`).
- Retrieve sidecar state (`tests/test_retrieve_state.py`).
- Plotly full report generation (`tests/test_report_plotly_full.py`).

## Git Hygiene / Ignore
- `.gitignore` expanded for `parquet/`, `ddl/`, `vendor/`, `*.parquet`.
- Data directory `data/` ignored; past history purged via `git filter-repo` (use force‑push).

## Ops Notes (2025‑09‑12)

Updated behaviors (2025‑09‑13):

- 15m defaults
  - E2E builds `features_15m.csv` and backtests on 15m by default.
  - Realtime passes the matching features cadence to backtest/online scoring via `--bar-interval`.
- Retrieval performance + robustness
  - Default `slice_days: 10` in configs; retriever clamps effective slice per interval so bars_per_slice ≤ API limit (≤1000). This reduces calls for 15m while staying safe for 5m.
  - Failure‑only HTTP dumps: on WARN/ERROR, logs full URL/status/response (DEBUG); success‑path HTTP logs only with `CRYPTOSTORM_HTTP_DEBUG=all`.
  - Strict empty handling: set `CRYPTOSTORM_STRICT_EMPTY=1` to treat 0‑item responses (after fallback) as errors that fail retrieval.
- Audit soft‑fail
  - `python -m cryptostorm audit ... --soft-fail` reports missing coverage but exits 0. The E2E script uses soft‑fail by default so the pipeline continues.
- Telegram spam controls
  - Defaults standardized in configs: `kinds: storm,pre_alert`, `only_new: true`, `include_json: true`, `cooldown_min`, `limit`.
- Universe tool (Binance)
  - `binance-top` fetches USDT‑perps and ranks by 30d/24h quote volume; optional AI ranking via OpenAI (`--ai --openai-model`), then updates `universe.symbols`.

- Telegram Alerts
  - Secrets are read from `secrets/telegram_bot_token.txt` and `secrets/telegram_chat_id.txt` via `configs/realtime.yaml`.
  - Realtime now sends Telegram alerts by default (compose includes `--send-telegram --telegram-kinds storm,pre_alert`).
  - Verify connectivity (buzz):
    - `TOKEN=$(<secrets/telegram_bot_token.txt); CHAT_ID=$(<secrets/telegram_chat_id.txt); curl -sS -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" -d chat_id="${CHAT_ID}" --data-urlencode text="Buzz test" -d disable_notification=false`
  - Dry‑run sender (no network): `PYTHONPATH=src python -m cryptostorm alert-telegram configs/realtime.yaml --dry-run`.

- Realtime & E2E
  - E2E pipeline (15m): `bash scripts/run_e2e.sh -c configs/realtime.yaml -l INFO`
    - Strict mode: `CRYPTOSTORM_STRICT_EMPTY=1 bash scripts/run_e2e.sh -c configs/realtime.yaml -l DEBUG`
  - Realtime single cycle: `python -m cryptostorm realtime configs/realtime.yaml --once --bar-interval 15m`
  - Continuous realtime (compose): `docker compose up realtime`

- Docker/Compose Test Commands
  - Docker: `docker build -t cryptostorm:tests . && docker run --rm -v "$PWD:/app" -w /app cryptostorm:tests pytest -q`
  - Compose: `docker compose build backfill && docker compose run --rm backfill pytest -q`
  - Live tests: add `-e RUN_LIVE_COINGLASS=1` and API key env or file.

-- Changes in this iteration
  - 15m features default; E2E builds 15m and backtests on 15m.
  - Retrieval: strict empty handling (opt‑in), failure‑only HTTP dumps, dynamic slice clamp; default `slice_days: 10` in configs.
  - Realtime: passes cadence explicitly to backtest/online scoring; Telegram defaults standardized.
  - Audit: `--soft-fail` added; E2E uses soft‑fail.
  - Universe: added `binance-top` with optional OpenAI‑assisted ranking.

- Troubleshooting
  - Report shows price only: likely no alerts yet or artifacts missing. Seed models, let realtime run multiple cycles, or lower `model.threshold_q` and set `alerts.persist_k_5m: 1`, `storm_confirm_k_5m.default: 1` for a demo; re‑seed models, then run realtime.
  - Telegram sends nothing: ensure realtime ran with `--send-telegram`; check `artifacts/<RUN_ID>/alerts/*.csv` has new rows; remove `artifacts/<RUN_ID>/alerts/telegram_sent.json` to resend for testing.
