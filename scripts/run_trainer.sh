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
  python - "$cfg" <<'PY' || echo 8
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
}

SLEEP_HOURS_EFF=$(resolve_sleep_hours "$CONFIG")
if ! [[ "$SLEEP_HOURS_EFF" =~ ^[0-9]+$ ]]; then SLEEP_HOURS_EFF=8; fi
if [[ "$SLEEP_HOURS_EFF" -lt 1 ]]; then SLEEP_HOURS_EFF=8; fi

echo "trainer: config=$CONFIG features_interval=$FEATURES_INTERVAL workers=$WORKERS sleep_hours=$SLEEP_HOURS_EFF"

# Resolve artifacts root/run_id for lock and notifications
resolve_artifacts_root() {
  python - "$CONFIG" <<'PY'
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
  run=cfg.get('run') or {}
  root=(run.get('artifacts_root') or './artifacts')
  run_id=(run.get('run_id') or 'run')
  print(f"{root}/{run_id}")
except Exception:
  print('./artifacts/run')
PY
}

ARTIFACTS_DIR=$(resolve_artifacts_root)
LOCK_DIR="$ARTIFACTS_DIR/.locks"
LOCK_FILE="$LOCK_DIR/retraining.lock"

notify_telegram() {
  local text="$1"
  local token="${TELEGRAM_BOT_TOKEN:-}"
  local chat="${TELEGRAM_CHAT_ID:-}"
  if [[ -z "$token" && -n "${TELEGRAM_BOT_TOKEN_FILE:-}" && -f "$TELEGRAM_BOT_TOKEN_FILE" ]]; then
    token=$(cat "$TELEGRAM_BOT_TOKEN_FILE" 2>/dev/null || true)
  fi
  if [[ -z "$chat" && -n "${TELEGRAM_CHAT_ID_FILE:-}" && -f "$TELEGRAM_CHAT_ID_FILE" ]]; then
    chat=$(cat "$TELEGRAM_CHAT_ID_FILE" 2>/dev/null || true)
  fi
  if [[ -z "$token" || -z "$chat" ]]; then
    echo "trainer: telegram credentials not set; skipping notify"
    return 0
  fi
  curl -sS -X POST "https://api.telegram.org/bot${token}/sendMessage" \
    -d chat_id="${chat}" --data-urlencode text="$text" -d disable_notification=false >/dev/null || true
}

trap 'rm -f "$LOCK_FILE"' EXIT INT TERM
while true; do
  echo "[trainer] $(date -u +%F\ %T) acquiring training lock at $LOCK_FILE"
  mkdir -p "$LOCK_DIR"
  date -u +%F\ %T >"$LOCK_FILE" || true
  # Snapshot current thresholds before retrain (best-effort)
  python - "$ARTIFACTS_DIR" <<'PY' || true
import json,sys,glob,os
from pathlib import Path
art=Path(sys.argv[1])
models=art/ 'models'
m={}
for fp in sorted(models.glob('*.json')):
  try:
    obj=json.loads(fp.read_text(encoding='utf-8') or '{}')
    th=obj.get('threshold')
    if isinstance(th,(int,float)):
      m[fp.stem]=float(th)
  except Exception:
    pass
lockdir=art/'.locks'
lockdir.mkdir(parents=True, exist_ok=True)
(lockdir/'thresholds_before.json').write_text(json.dumps(m, separators=(',',':')), encoding='utf-8')
PY
  echo "[trainer] $(date -u +%F\ %T) running backtest (train)"
  python -m cryptostorm backtest "$CONFIG" --features "$FEATURES_DIR" --features-interval "$FEATURES_INTERVAL" --workers "$WORKERS" || true
  rm -f "$LOCK_FILE" || true
  # Build threshold-change summary (best-effort)
  TH_MSG=$(python - <<'PY'
import json, os, glob, sys, math, datetime as dt
from pathlib import Path
art = Path(os.environ.get('ARTIFACTS_DIR','artifacts/run'))
models = art/ 'models'
def load_map():
    m = {}
    for fp in sorted(models.glob('*.json')):
        try:
            obj=json.loads(fp.read_text(encoding='utf-8') or '{}')
            th = obj.get('threshold')
            if isinstance(th,(int,float)):
                m[fp.stem]=float(th)
        except Exception:
            continue
    return m
before_fp = art/'.locks'/'thresholds_before.json'
try:
    before = json.loads(before_fp.read_text(encoding='utf-8')) if before_fp.exists() else {}
except Exception:
    before = {}
after = load_map()
total = len(after)
changes = []
for sym, new in after.items():
    old = before.get(sym)
    if isinstance(old,(int,float)):
        if not math.isclose(old, new, rel_tol=1e-9, abs_tol=1e-12):
            dpct = (new-old)/old*100.0 if old!=0 else float('inf')
            changes.append((sym, old, new, dpct))
    else:
        # new symbol
        changes.append((sym, float('nan'), new, float('inf')))
changes.sort(key=lambda x: (abs(x[3]) if math.isfinite(x[3]) else 1e9), reverse=True)
ts = dt.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
run_id = os.path.basename(str(art))
lines = [f"⚙️ Thresholds updated — {run_id} (UTC {ts})", f"changed: {len(changes)}/{total} symbols"]
top = changes[:10]
if top:
    lines.append('Top changes:')
    for sym, old, new, dpct in top:
        if math.isfinite(dpct) and not math.isnan(old):
            lines.append(f"{sym} {old:.3f}→{new:.3f} ({dpct:+.1f}%)")
        else:
            lines.append(f"{sym} new→{new:.3f}")
print("\n".join(lines))
PY
  )
  NOW_UTC=$(date -u +%F\ %T)
  notify_telegram "$TH_MSG"
  echo "[trainer] $(date -u +%F\ %T) sleeping ${SLEEP_HOURS_EFF}h"
  sleep $(( SLEEP_HOURS_EFF * 3600 ))
done
