# CryptoStorm Retrieve — Quickstart

## Prerequisites
- Python 3.9+ (3.11+ recommended)
- Install minimal deps:
  - `python -m venv .venv && source .venv/bin/activate`
  - `pip install pyyaml`

## Configure API Key
- Use a file (preferred):
  - `mkdir -p secrets && printf '%s' 'YOUR_KEY' > secrets/coinglass_api_key.txt`
  - In `configs/example.yaml` under `acquisition.coinglass` add:
    - `api_key_file: ./secrets/coinglass_api_key.txt`
- Or use an env var:
  - `export COINGLASS_API_KEY='YOUR_KEY'`

## Validate Config (Item 1)
- `PYTHONPATH=src python -m cryptostorm validate configs/example.yaml`
- Confirms dataset modes/intervals and API key presence.

## Test Retrieval (Item 2)
- Dry-run (no network writes; prints planned requests):
  - `PYTHONPATH=src python -m cryptostorm retrieve configs/example.yaml --dry-run --log-level INFO`
- Live retrieval (writes JSONL under `data/<SYM>/`):
  - `PYTHONPATH=src python -m cryptostorm retrieve configs/example.yaml --out data --log-level INFO`
  - To fetch full 30d 5m history via v4: enable time-slicing
    - In config (`acquisition.coinglass.slice_days: 2`) or CLI `--slice-days 2`
  - To force reliable v3 per-exchange endpoints for selected datasets:
    - In config: `acquisition.coinglass.force_v3: ["futures_ohlcv_5m","oi_5m_ohlc"]`
  - Orderbook (v4) may require a time enum; set in config: `orderbook_time_enum: LAST_OF_5M`

## 30‑Day History: Proven Strategies
- Preferred: v4 time‑slicing (keeps aggregation)
  - Set `acquisition.coinglass.slice_days: 2` (or pass `--slice-days 2`).
  - Retriever fetches the 30‑day window in small slices (e.g., 2 days per request) and dedups by timestamp.
  - Add a modest pause per slice to respect rate limits (already built in).
- Fallback: v3 per‑exchange (reliable pagination)
  - Set `acquisition.coinglass.force_v3: ["futures_ohlcv_5m","oi_5m_ohlc","taker_futures_5m","liquidation_5m"]` to page reliably.
  - Use when v4 caps history to the latest ~500 bars for your plan.

## Orderbook (v4) TimeEnum Tips
- v4 OB endpoints require `timeEnum` instead of `interval` for 5‑minute snapshots.
- Try `orderbook_time_enum: LAST_OF_5M` in config. If the API responds with an error, try alternatives: `LAST_5M`, `LAST_5MIN`, or `END_OF_5M`.
- If none work, consider skipping OB temporarily or ask Coinglass support for the exact enum list for your plan.

## Troubleshooting & Logs
- Retrieval logs:
  - Empty preferred page → logs top‑level keys and response JSON snippet.
  - Fallback logs include `symbol/exchange/interval` and ISO start/end.
  - Paginator stops on “no progress” (duplicate timestamps) to avoid loops.
- Feature logs (use `--log-level DEBUG`):
  - Prints grid span, input counts/ranges, alignment, coverage, missing counts, sample rows, and payload sample keys.

## Plan Limits (Heads‑up)
- Some plans restrict 5‑minute history length or endpoint features; if v4 slices still return only recent data, prefer the v3 fallback for time‑series datasets.

## Backtest (Item 4)
- Walk-forward IsolationForest on features:
  - `PYTHONPATH=src python -m cryptostorm backtest configs/example.yaml --features features --artifacts-root artifacts/$(date +%Y%m%d-%H%M%S)`
- Outputs under `artifacts/<RUN_ID>/` by default (or `--artifacts-root` override):
  - `alerts/<SYM>.csv`, `scores/<SYM>.csv`, `metrics/metrics.json`.

## Report (Item 5)
- Full report — choose charting engine:
  - Lightweight Charts (default): price, score, OI/liqs
    - `PYTHONPATH=src python -m cryptostorm report configs/example.yaml --data data --features features --out reports`
    - Offline: place `lightweight-charts.standalone.production.js` in `vendor/` or `reports/vendor/`.
  - Plotly (no Lightweight Charts): price (candlestick), score, OI+liq
    - `PYTHONPATH=src python -m cryptostorm report-plotly configs/example.yaml --data data --out reports`
    - Offline: place `plotly-2.32.0.min.js` in `vendor/` or `reports/vendor/`.

- Interactive price+alerts (Plotly) — price line + alert markers only:
  - `PYTHONPATH=src python -m cryptostorm report-price configs/example.yaml --data data --out reports`
  - Reads alerts from `artifacts/<RUN_ID>/alerts/<SYM>.csv` (produce via backtest).
  - To inline Plotly (no CDN), put `plotly-2.32.0.min.js` at `vendor/` or `reports/vendor/`.

## Outputs & Quick Checks
- Files per symbol in `data/<SYM>/`:
  - `futures_ohlcv_5m.jsonl`, `spot_ohlcv_5m.jsonl`, `funding_8h_ohlc.jsonl`, `oi_5m_ohlc.jsonl`, `taker_futures_5m.jsonl`, `liquidation_5m.jsonl`, `orderbook_futures_5m.jsonl` (plus optional datasets if enabled).
- Verify:
  - `ls data/BTCUSDT`
  - `head -n 2 data/BTCUSDT/futures_ohlcv_5m.jsonl`

## Notes
- Dataset enable flags and modes are driven by `configs/example.yaml` (see `acquisition.coinglass.intervals`, `per_series_mode`, `enable`).
- Aggregated series (e.g., `oi_5m_ohlc`, `liquidation_5m`) use `conventions.symbol_to_coin` for coin mapping.
- Retrieval persists raw responses (no resampling) as JSONL with a stable schema: `{ts, symbol, interval, mode, source, payload}`.

## Run Unit Tests
- All tests (Items 1–2):
  - `bash scripts/test.sh`
  - or `PYTHONPATH=src python -m unittest discover -s tests -p 'test_*.py' -v`

## End-to-End Script
- One-shot pipeline (validate → retrieve → features → backtest → audit → Plotly price+alerts):
  - `bash scripts/run_e2e.sh -c configs/example.yaml`
  - Options:
    - `-l INFO|DEBUG` sets retrieve log level (default INFO)
    - `--http-debug` enables low-level HTTP GET/RESP logs for retrieve (default off)

## Realtime (Phase 1)
- Single-process loop aligned to 5‑minute UTC bar closes:
  - `PYTHONPATH=src python -m cryptostorm realtime configs/example.yaml --data data --features features --once` (single cycle)
  - Continuous: `PYTHONPATH=src python -m cryptostorm realtime configs/example.yaml --data data --features features --poll-offset-s 10 --jitter-s 2`
  - Steps per cycle: retrieve (incremental) → features `--update-last` → backtest (writes artifacts) → optional Telegram (use `--send-telegram` and env tokens; default kinds: storm,pre_alert)
- Incremental feature append (without full rebuild):
  - `PYTHONPATH=src python -m cryptostorm feature configs/example.yaml --data data --out features --update-last`

## End-to-End Realtime
- Convenience script to run realtime once (validate → realtime → report):
  - `bash scripts/run_realtime.sh -c configs/example.yaml --online --once --report-engine plotly`
  - Continuous watch (5m-aligned loop), with Telegram alerts (default storm+pre_alert):
    - `SEND_TELEGRAM=1 TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... bash scripts/run_realtime.sh -c configs/example.yaml --online --watch --poll-offset-s 10 --jitter-s 2 --report-engine plotly`
    - `--report-engine` options:
    - `price` (default): Plotly price+alerts only, `<SYM>_price_alert.html`
    - `plotly`: full report with candles, score, OI+liq, `<SYM>_plotly.html`
    - `lightweight`: lightweight-charts report, `<SYM>.html`
  - Fill gaps automatically and launch console monitor:
    - `COINGLASS_API_KEY=... bash scripts/run_realtime.sh -c configs/realtime.yaml --ensure-data --online --once --monitor --monitor-view alerts`
    - `--ensure-data` runs an audit and, if coverage < `--ensure-min-ratio` (default 0.95), backfills via `retrieve --watch --once` with `--ensure-workers` (default 1) and `--ensure-rps` (default 3).
    - `--monitor` launches the console dashboard after realtime; `--monitor-view alerts` shows the latest score/alert per symbol.
  - Update reports every 5 minutes (and write `reports/index.html`):
    - `COINGLASS_API_KEY=... bash scripts/run_realtime.sh -c configs/realtime.yaml --ensure-data --online --watch --build-reports`
    - Choose engine: `price` (default), `plotly`, or `lightweight`.

- One‑time tuned bootstrap (Top‑N → retrieve → features(15m) → tune q → merge → backtest) before realtime:
  - `bash scripts/run_realtime.sh -c configs/realtime.yaml --bootstrap-tuned --top 200 --online --watch`
  - Optional: speed up tuner with a smaller grid: `--q-grid "0.972,0.980,0.988"`
  - Idempotent: leaves a marker at `artifacts/<RUN_ID>/.bootstrapped` and skips on next start.

### Realtime Config
- A production‑ready config tailored for the realtime loop is provided at `configs/realtime.yaml`.
- It mirrors `configs/top.yaml` but fixes `run.run_id: realtime` so artifacts consolidate under `artifacts/realtime/`, and includes a `notifications.telegram` section.
- Use it with the E2E script or CLI commands.

## Realtime (Phase 2)
- Retrieve watch mode with parallel workers and global RPS limiter:
  - `PYTHONPATH=src python -m cryptostorm retrieve configs/example.yaml --out data --watch --workers 4 --rps 2 --poll-offset-s 10 --jitter-s 2`
  - Uses per-file sidecar state at `data/<SYM>/.state/<dataset>.json` to fetch only deltas.
  - Global rate limit applies to all HTTP calls across workers.
- Notes:
  - Sidecar is updated after each cycle; first run falls back to scanning JSONL for `last_ts`.
  - Combine with Phase 1 realtime to build features/alerts after retrieve.

## Realtime (Phase 3)
- Online scoring (no retrain):
  - Create model artifacts once (offline or scheduled):
    - `PYTHONPATH=src python -m cryptostorm backtest configs/example.yaml --features features --artifacts-root artifacts/<RUN_ID>`
  - Append a new features row (per Phase 1).
  - Score latest row only:
    - `PYTHONPATH=src python -m cryptostorm backtest configs/example.yaml --features features --artifacts-root artifacts/<RUN_ID> --online`
- Realtime loop with online scoring + SLO metrics:
  - `PYTHONPATH=src python -m cryptostorm realtime configs/example.yaml --data data --features features --online-scoring`
  - Writes per-cycle SLOs to `artifacts/<RUN_ID>/metrics/realtime.jsonl` (scheduler_lag_s, durations, mode).

## Realtime (Phase 4)
- FastAPI server for scores/alerts and reports (optional):
  - Start: `PYTHONPATH=src python -m cryptostorm api configs/example.yaml --data data --features features --reports reports --host 0.0.0.0 --port 8000`
  - Auth (optional): add `--token YOUR_TOKEN` and include `Authorization: Bearer YOUR_TOKEN` on requests.
  - Endpoints:
    - `GET /healthz` — service health
    - `GET /symbols` — list symbols (auth if token set)
    - `GET /scores/{symbol}?n=200` — latest N score rows
    - `GET /alerts/{symbol}?n=200` — latest N alerts
    - `GET /latest/score/{symbol}` — most recent score tuple
    - `GET /report/{symbol}` — serves HTML report
    - `GET /metrics?prom=true|false` — latest realtime SLOs (Prometheus text if `prom=true`)
  - Notes: requires `fastapi` and `uvicorn` (install if you plan to run the API)

## Auto‑select Top Binance Symbols
- Populate `universe.symbols` with the top USDT‑perp contracts by quote volume (simple, deterministic):
  - `PYTHONPATH=src python -m cryptostorm binance-top --top 200 --out configs/realtime.yaml --print`
  - Binance-first: candidates come from Binance (USDT‑M PERPETUAL) so you can select exactly `--top` even when local data is sparse. If Binance is unavailable, the tool falls back to local Coinglass data under `data/<SYM>/futures_ohlcv_{15m|5m}.jsonl`.
  - Metrics source is hybrid: for symbols that exist under `data/`, local Coinglass JSONL is used to compute volumes deterministically; otherwise Binance daily klines + 24h ticker are used.
  - Ranks by `(−vol30d_quote, −vol24h_quote, symbol)` and writes exactly `--top` symbols when candidates allow.

  - AI ranking removed: the tool now uses a simple deterministic volume-based ranking only.

## Console Monitor (TUI)
- Text dashboard to monitor freshness and SLOs:
  - `PYTHONPATH=src python -m cryptostorm monitor configs/top.yaml --data data --features features --symbols 20 --refresh-s 2`
  - Views:
    - `--view data` (default): now/bar times, SLOs, per‑symbol latest feature ts/age, key dataset last_ts/age, and last alert time.
    - `--view alerts`: per‑symbol latest score ts/age + value/threshold, and latest alert kind + age (no raw dataset columns).
  - Quit with `q`.

## Reports Index
- Report generators now write a lightweight `index.html` under the reports directory.
- The realtime loop with `--build-reports` also refreshes `index.html` each cycle.

## Docker Usage
- Build images once:
  - `docker compose build`
- Continuous realtime with tuned bootstrap (one‑command startup):
  - `COINGLASS_API_KEY=... docker compose up realtime`
  - The `realtime` service runs a one‑time bootstrap: select Top 200, retrieve once, build 15m features, tune q and merge overlay, backtest to persist artifacts, then starts the realtime loop (`--online`).
  - Idempotent via `artifacts/<RUN_ID>/.bootstrapped`.
  - Open `reports/index.html`
  - Tail logs: `docker compose logs -f realtime`
  - Background mode: `docker compose up -d realtime`
- One‑shot (single cycle + reports):
  - `COINGLASS_API_KEY=... docker compose run --rm backfill`
  - Open `reports/index.html`
- Backfill data (audit + fetch missing):
  - Standard: `COINGLASS_API_KEY=... docker compose run --rm backfill`
  - Faster (tune workers/RPS):
    - `COINGLASS_API_KEY=... docker compose run --rm backfill bash -lc "scripts/run_realtime.sh -c configs/realtime.yaml --ensure-data --online --once --build-reports --ensure-workers 12 --ensure-rps 6"`
  - Raw‑only (retrieve deltas, skip features/score/reports):
    - `COINGLASS_API_KEY=... docker compose run --rm backfill python -m cryptostorm retrieve configs/realtime.yaml --out data --watch --once --workers 8 --rps 3`
- Optional services:
  - API: `docker compose up api` (http://localhost:8000)
  - Monitor (console): `docker compose run --rm monitor`

## Telegram Alerts (Optional)
- Create a bot and obtain credentials:
  - `TELEGRAM_BOT_TOKEN` from BotFather
  - `TELEGRAM_CHAT_ID` (your chat or channel ID)
- Config‑based settings (avoid CLI/env where desired):
  ```yaml
  notifications:
    telegram:
      bot_token_file: ./secrets/telegram_bot_token.txt  # or bot_token: "..."
      chat_id_file:   ./secrets/telegram_chat_id.txt    # or chat_id:   "..."
      kinds: storm,pre_alert
      only_new: true
      since_ts: null
      dry_run: false
  ```
- Send alerts from the latest run (config‑aware):
  - `PYTHONPATH=src python -m cryptostorm alert-telegram configs/example.yaml`
  - Override as needed: `--kinds storm`, `--only-new`, `--since-ts 1700000000000`, `--dry-run`
- Inline credentials via env or files remain supported:
  - `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, or `*_FILE`
- E2E convenience (send after realtime):
  - `SEND_TELEGRAM=1 bash scripts/run_realtime.sh -c configs/example.yaml`

## Verify 30‑Day Coverage
- Audit that each dataset has enough rows in the rolling 30‑day window:
  - `PYTHONPATH=src python -m cryptostorm audit configs/example.yaml --data data --min-ratio 0.95`
- Output shows observed vs expected counts per symbol/dataset and fails if any ratio falls below the threshold.

## Run Unit Tests (Docker)
- Build image with dev deps:
  - `docker build --pull -t cryptostorm:tests .`
- Run all tests (live tests skipped by default):
  - `docker run --rm -v "$PWD:/app" -w /app cryptostorm:tests pytest -q`
- One‑liner build + run:
  - `docker build -t cryptostorm:tests . && docker run --rm -v "$PWD:/app" -w /app cryptostorm:tests pytest -q`
- Include live Coinglass tests (requires API key):
  - Env var: `docker run --rm -e RUN_LIVE_COINGLASS=1 -e COINGLASS_API_KEY=YOUR_KEY -v "$PWD:/app" -w /app cryptostorm:tests pytest -q`
  - Or via file: `docker run --rm -e RUN_LIVE_COINGLASS=1 -e COINGLASS_API_KEY_FILE=/app/secrets/coinglass_api_key.txt -v "$PWD:/app" -w /app cryptostorm:tests pytest -q`
- Run only Telegram tests:
  - `docker run --rm -v "$PWD:/app" -w /app cryptostorm:tests pytest -q -k telegram`

## Run Unit Tests (Docker Compose)
- Build the service image defined in `docker-compose.yml`:
  - `docker compose build backfill`
- Run all tests inside the `backfill` service container:
  - `docker compose run --rm backfill pytest -q`
- Run a subset (example: Telegram tests only):
  - `docker compose run --rm backfill pytest -q -k telegram`
- Include live Coinglass tests (requires API key):
  - Env var: `docker compose run --rm -e RUN_LIVE_COINGLASS=1 -e COINGLASS_API_KEY=YOUR_KEY backfill pytest -q`
  - Or via file: `docker compose run --rm -e RUN_LIVE_COINGLASS=1 -e COINGLASS_API_KEY_FILE=/app/secrets/coinglass_api_key.txt backfill pytest -q`

Notes:
- "docker" runs a single container by image; "docker compose" uses the repo’s compose services and their preconfigured environment/volumes.

## Performance Tips
- Realtime retrieve: avoid full-window refetches. Keep `acquisition.coinglass.slice_days` small for initial backfill, but deltas are always respected via sidecar `last_ts` (the retriever now uses `last_ts+1` even when `slice_days>0`). For pure realtime, you can also set `slice_days: 0`.
- Time-slice pacing: a global RPS limiter in watch mode governs calls; per-slice delay is minimal when no limiter is set.
- Features build (full backfill): orderbook processing is optimized to read snapshots once per symbol (previously O(N) file scans per bar). Still, building 30 days for many symbols is heavy; consider doing it once, then using `--online` scoring.
- Backtest training cost: reduce `model.retrain_every_hours` (e.g., 12) and/or `per_tier_overrides.*.n_estimators` for large universes. `n_jobs: -1` already enables parallel trees.
- Realtime scoring: prefer `realtime --online-scoring` with pre-seeded artifacts to avoid retraining each cycle.
## Binance USDT‑M Perps Dataset (CSV)
- Build a reviewable dataset of trading pairs and metrics (30d daily klines + 24h stats):
  - `pip install aiohttp`
  - `python scripts/binance_perp_usdt_dataset.py --days 30 --concurrency 8 --outfile dataset_usdtm_perps.csv`
- Outputs columns: symbol, base/quote assets, onboarding date, precision/filters, 30d volumes (base/quote), avg_daily_quote, vol24h_quote, liquidity momentum (vol24/avg_daily), 30d trades, tradeCount24h, realized vol (daily and annualized).
  - Keep concurrency modest (6–12) to avoid 418/429 bans by Binance.
### One-shot Pipeline via Compose
- Run a simple pipeline (update symbols → backfill → tuning) as a one-off job:
  - `docker compose run --rm pipeline`
- After it finishes, bring up long-running services:
  - `docker compose up -d realtime trainer api`
