#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  scripts/run_pipeline.sh -c <config> [options]

Options:
  -c, --config <path>      Path to YAML/JSON config (default: configs/top.yaml)
  -t, --top <n>            Update universe.symbols with top N (default: 100)
      --no-update          Skip updating symbols (binance-top)
      --no-backfill        Skip backfill/e2e (retrieve+features+backtest+reports)
      --no-tune            Skip threshold tuning sweep
      --features-interval  5m|15m for backfill/tuning (default: 15m)
      --q-grid <list>      Quantile grid (default: 10-run built-in)
      --workers <n>        Workers for backtest/tuner (default: auto)
  -h, --help               Show this help

Notes:
  - This is an orchestration helper. It does not start long-running services.
    After it finishes, run:  docker compose up -d realtime trainer api
USAGE
}

CONFIG="configs/top.yaml"
TOP=100
DO_UPDATE=1
DO_BACKFILL=1
DO_TUNE=1
FEATURES_INTERVAL="15m"
Q_GRID=""
WORKERS=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    -c|--config) CONFIG="$2"; shift 2;;
    -t|--top) TOP="$2"; shift 2;;
    --no-update) DO_UPDATE=0; shift;;
    --no-backfill) DO_BACKFILL=0; shift;;
    --no-tune) DO_TUNE=0; shift;;
    --features-interval) FEATURES_INTERVAL="$2"; shift 2;;
    --q-grid) Q_GRID="$2"; shift 2;;
    --workers) WORKERS="$2"; shift 2;;
    -h|--help) usage; exit 0;;
    *) echo "Unknown option: $1" >&2; usage; exit 2;;
  esac
done

export PYTHONPATH=${PYTHONPATH:-src}

echo "pipeline: config=$CONFIG top=$TOP update=$DO_UPDATE backfill=$DO_BACKFILL tune=$DO_TUNE"

if [[ "$DO_UPDATE" == "1" ]]; then
  echo "[1/3] Update universe.symbols (+symbol_to_coin) -> $CONFIG (top=$TOP)"
  python -m cryptostorm binance-top --top "$TOP" --out "$CONFIG" --print
fi

if [[ "$DO_BACKFILL" == "1" ]]; then
  echo "[2/3] Backfill E2E (retrieve -> features($FEATURES_INTERVAL) -> backtest -> report)"
  scripts/run_e2e.sh -c "$CONFIG" -l INFO --features-interval "$FEATURES_INTERVAL"
else
  echo "[2/3] Backfill skipped"
fi

if [[ "$DO_TUNE" == "1" ]]; then
  echo "[3/3] Threshold tuning sweep (grid=${Q_GRID:-default})"
  TUNE_CMD=(python scripts/tune_thresholds.py --config "$CONFIG" --features features --features-interval "$FEATURES_INTERVAL")
  if [[ -n "$Q_GRID" ]]; then TUNE_CMD+=(--q-grid "$Q_GRID"); fi
  if [[ -n "$WORKERS" ]]; then TUNE_CMD+=(--workers "$WORKERS"); fi
  "${TUNE_CMD[@]}"
  echo "Tuning outputs: artifacts/tuning/recommendations.csv and overlay_thresholds.yaml"
else
  echo "[3/3] Tuning skipped"
fi

echo "Done. To start services:  docker compose up -d realtime trainer api"

