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

## Backtest (Item 4)
- Walk-forward IsolationForest on features:
  - `PYTHONPATH=src python -m cryptostorm backtest configs/example.yaml --features features --artifacts-root artifacts/$(date +%Y%m%d-%H%M%S)`
- Outputs under `artifacts/<RUN_ID>/` by default (or `--artifacts-root` override):
  - `alerts/<SYM>.csv`, `scores/<SYM>.csv`, `metrics/metrics.json`.

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

## Verify 30‑Day Coverage
- Audit that each dataset has enough rows in the rolling 30‑day window:
  - `PYTHONPATH=src python -m cryptostorm audit configs/example.yaml --data data --min-ratio 0.95`
- Output shows observed vs expected counts per symbol/dataset and fails if any ratio falls below the threshold.
