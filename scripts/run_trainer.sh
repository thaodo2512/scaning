#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  scripts/run_trainer.sh -c <config> [options]

Options:
  -c, --config <path>          Path to YAML/JSON config (required)
  -f, --features <dir>         Features directory (default: features)
      --features-interval <iv> Features cadence for backtest: 5m|15m|auto (default: 15m)
  -w, --workers <n>            Backtest workers (default: auto from CRYPTOSTORM_WORKERS or CPU)
      --sleep-hours <h>        Sleep hours between training runs (default: auto from model.retrain_every_hours or 8)
  -h, --help                   Show help

Notes:
  - This loops forever: backtest (train) -> sleep -> repeat.
  - Realtime (online scoring) will pick up refreshed artifacts on the next cycle.
USAGE
}

CONFIG=""
FEATURES_DIR="features"
FEATURES_INTERVAL="15m"
WORKERS=""
SLEEP_HOURS="auto"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -c|--config) CONFIG="$2"; shift 2;;
    -f|--features) FEATURES_DIR="$2"; shift 2;;
    --features-interval) FEATURES_INTERVAL="$2"; shift 2;;
    -w|--workers) WORKERS="$2"; shift 2;;
    --sleep-hours) SLEEP_HOURS="$2"; shift 2;;
    -h|--help) usage; exit 0;;
    *) echo "Unknown option: $1" >&2; usage; exit 2;;
  esac
done

if [[ -z "$CONFIG" ]]; then
  echo "Error: --config is required" >&2
  usage
  exit 2
fi

export PYTHONPATH=${PYTHONPATH:-src}

# Resolve workers
if [[ -z "${WORKERS}" ]]; then
  if [[ -n "${CRYPTOSTORM_WORKERS:-}" ]]; then
    WORKERS="${CRYPTOSTORM_WORKERS}"
  else
    if command -v getconf >/dev/null 2>&1; then
      WORKERS=$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)
    elif command -v nproc >/dev/null 2>&1; then
      WORKERS=$(nproc 2>/dev/null || echo 1)
    elif command -v sysctl >/dev/null 2>&1; then
      WORKERS=$(sysctl -n hw.ncpu 2>/dev/null || echo 1)
    else
      WORKERS=1
    fi
  fi
fi

# Resolve sleep hours (default to config's model.retrain_every_hours, else 8)
resolve_sleep_hours() {
  local cfg="$1"
  local sh="$SLEEP_HOURS"
  if [[ "$sh" != "auto" ]]; then
    echo "$sh"
    return
  fi
  python - <<PY || echo 8
import json,sys
from pathlib import Path
try:
  p=Path(sys.argv[1])
  txt=p.read_text(encoding='utf-8')
  if p.suffix.lower() in {'.yaml','.yml'}:
    import yaml
    cfg=yaml.safe_load(txt) or {}
  else:
    cfg=json.loads(txt)
  h=(cfg.get('model') or {}).get('retrain_every_hours')
  print(int(h) if isinstance(h,(int,float)) and int(h)>0 else 8)
except Exception:
  print(8)
PY
  \
  "$cfg"
}

SLEEP_HOURS_EFF=$(resolve_sleep_hours "$CONFIG")
if ! [[ "$SLEEP_HOURS_EFF" =~ ^[0-9]+$ ]]; then SLEEP_HOURS_EFF=8; fi
if [[ "$SLEEP_HOURS_EFF" -lt 1 ]]; then SLEEP_HOURS_EFF=8; fi

echo "trainer: config=$CONFIG features_interval=$FEATURES_INTERVAL workers=$WORKERS sleep_hours=$SLEEP_HOURS_EFF"

while true; do
  echo "[trainer] $(date -u +%F\ %T) running backtest (train)"
  python -m cryptostorm backtest "$CONFIG" --features "$FEATURES_DIR" --features-interval "$FEATURES_INTERVAL" --workers "$WORKERS" || true
  echo "[trainer] $(date -u +%F\ %T) sleeping ${SLEEP_HOURS_EFF}h"
  sleep $(( SLEEP_HOURS_EFF * 3600 ))
done

