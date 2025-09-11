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
- Full report (Lightweight Charts) — price, score, OI/liqs:
  - `PYTHONPATH=src python -m cryptostorm report configs/example.yaml --data data --features features --out reports`
  - Opens offline; to inline the charting lib without CDN, drop `lightweight-charts.standalone.production.js` at `vendor/` or `reports/vendor/`.

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
  - Steps per cycle: retrieve (incremental) → features `--update-last` → backtest (writes artifacts) → optional Telegram (use `--send-telegram` and env tokens)
- Incremental feature append (without full rebuild):
  - `PYTHONPATH=src python -m cryptostorm feature configs/example.yaml --data data --out features --update-last`

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

## Telegram Alerts (Optional)
- Create a bot and obtain credentials:
  - `TELEGRAM_BOT_TOKEN` from BotFather
  - `TELEGRAM_CHAT_ID` (your chat or channel ID)
- Send alerts from the latest run:
  - `PYTHONPATH=src python -m cryptostorm alert-telegram configs/example.yaml --kinds storm --only-new`
  - Use `--kinds storm,pre_alert` to include pre‑alerts as well
  - Add `--dry-run` to preview messages without sending
- Inline credentials from files if preferred:
  - `TELEGRAM_BOT_TOKEN_FILE`, `TELEGRAM_CHAT_ID_FILE`
- E2E convenience (send after pipeline):
  - `SEND_TELEGRAM=1 TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... bash scripts/run_e2e.sh -c configs/example.yaml`

## Verify 30‑Day Coverage
- Audit that each dataset has enough rows in the rolling 30‑day window:
  - `PYTHONPATH=src python -m cryptostorm audit configs/example.yaml --data data --min-ratio 0.95`
- Output shows observed vs expected counts per symbol/dataset and fails if any ratio falls below the threshold.
