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
      --top <n>                Top N symbols to select on each refresh (default: 200)
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
TOP="200"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -c|--config) CONFIG="$2"; shift 2;;
    -f|--features) FEATURES_DIR="$2"; shift 2;;
    --features-interval) FEATURES_INTERVAL="$2"; shift 2;;
    -w|--workers) WORKERS="$2"; shift 2;;
    --sleep-hours) SLEEP_HOURS="$2"; shift 2;;
    --top) TOP="$2"; shift 2;;
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

# Auto-merge tuned threshold overlay into config if present
merge_threshold_overlay() {
  local cfg="$1"
  # Default overlay location produced by scripts/tune_thresholds.py
  local overlay="artifacts/tuning/overlay_thresholds.yaml"
  if [[ -n "${CRYPTOSTORM_TUNING_OVERLAY:-}" ]]; then
    overlay="${CRYPTOSTORM_TUNING_OVERLAY}"
  fi
  if [[ ! -f "$overlay" ]]; then
    return
  fi
  echo "[trainer] merging tuned thresholds overlay -> $cfg ($overlay)"
  python - "$cfg" "$overlay" <<'PY' || true
import sys, json
from pathlib import Path
cfgp, ovp = Path(sys.argv[1]), Path(sys.argv[2])
try:
  txt = cfgp.read_text(encoding='utf-8')
  if cfgp.suffix.lower() in {'.yaml','.yml'}:
    import yaml  # type: ignore
    cfg = yaml.safe_load(txt) or {}
  else:
    cfg = json.loads(txt)
  ovtxt = ovp.read_text(encoding='utf-8')
  if ovp.suffix.lower() in {'.yaml','.yml'}:
    import yaml  # type: ignore
    overlay = yaml.safe_load(ovtxt) or {}
  else:
    overlay = json.loads(ovtxt)
  tgt = (cfg.setdefault('model', {})
             .setdefault('threshold_q_per_symbol', {}))
  src = (overlay.get('model') or {}).get('threshold_q_per_symbol') or {}
  if isinstance(src, dict):
    tgt.update(src)
  # Write back, preserving YAML when possible
  if cfgp.suffix.lower() in {'.yaml','.yml'}:
    import yaml  # type: ignore
    cfgp.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding='utf-8')
  else:
    cfgp.write_text(json.dumps(cfg, indent=2), encoding='utf-8')
  print(f"merged {len(src)} symbol thresholds")
except Exception as e:
  print(f"merge overlay failed: {e}")
PY
}

notify_telegram() {
  local text="$1"
  local token="${TELEGRAM_BOT_TOKEN:-}"
  local chat_raw="${TELEGRAM_CHAT_ID:-}"
  if [[ -z "$token" && -n "${TELEGRAM_BOT_TOKEN_FILE:-}" && -f "$TELEGRAM_BOT_TOKEN_FILE" ]]; then
    token=$(cat "$TELEGRAM_BOT_TOKEN_FILE" 2>/dev/null || true)
  fi
  if [[ -z "$chat_raw" && -n "${TELEGRAM_CHAT_ID_FILE:-}" && -f "$TELEGRAM_CHAT_ID_FILE" ]]; then
    chat_raw=$(cat "$TELEGRAM_CHAT_ID_FILE" 2>/dev/null || true)
  fi
  if [[ -z "$token" || -z "$chat_raw" ]]; then
    echo "trainer: telegram credentials not set; skipping notify"
    return 0
  fi
  # Support multiple chat IDs: comma or newline separated
  # Normalize delimiters to spaces
  local chats
  chats=$(echo "$chat_raw" | tr ',\n' '  ')
  for cid in $chats; do
    curl -sS -X POST "https://api.telegram.org/bot${token}/sendMessage" \
      -d chat_id="${cid}" --data-urlencode text="$text" -d disable_notification=false >/dev/null || true
    sleep 0.2
  done
}

trap 'rm -f "$LOCK_FILE"' EXIT INT TERM
while true; do
  echo "[trainer] $(date -u +%F\ %T) acquiring training lock at $LOCK_FILE"
  mkdir -p "$LOCK_DIR"
  date -u +%F\ %T >"$LOCK_FILE" || true
  # Optionally refresh universe.symbols before retrain (default on)
  if [[ "${TRAINER_REFRESH_SYMBOLS:-1}" == "1" ]]; then
    # Snapshot symbols before refresh
  python - "$CONFIG" "$LOCK_DIR" <<'PY' || true
import json,sys
from pathlib import Path
try:
  p=Path(sys.argv[1]); txt=p.read_text(encoding='utf-8')
  if p.suffix.lower() in {'.yaml','.yml'}:
    import yaml
    cfg=yaml.safe_load(txt) or {}
  else:
    cfg=json.loads(txt)
  syms=list((cfg.get('universe') or {}).get('symbols') or [])
  out={'symbols': syms}
  lockdir=Path(sys.argv[2]); lockdir.mkdir(parents=True, exist_ok=True)
  (lockdir/ 'symbols_before.json').write_text(json.dumps(out, separators=(',',':')), encoding='utf-8')
except Exception:
  pass
PY
    # Determine target Top-N for refresh: CLI override (TOP) wins; default 200
    TOP_N="${TOP:-200}"
    echo "[trainer] refreshing symbols (top=$TOP_N)"
    python -m cryptostorm binance-top --top "$TOP_N" --out "$CONFIG" --print || true
    # Guard: ensure refreshed list still has TOP_N symbols when possible.
    # If the refresh produced fewer than TOP_N but the previous list had TOP_N,
    # restore the previous list to keep the target universe size stable.
    python - "$CONFIG" "$LOCK_DIR" "$TOP_N" <<'PY' || true
import json, sys
from pathlib import Path
cfgp, lockdir, top_str = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
try:
    top_n = int(top_str)
except Exception:
    top_n = 0
try:
    txt = cfgp.read_text(encoding='utf-8')
    if cfgp.suffix.lower() in {'.yaml','.yml'}:
        import yaml  # type: ignore
        cfg = yaml.safe_load(txt) or {}
    else:
        cfg = json.loads(txt)
    uni = cfg.get('universe') or {}
    new_syms = list(uni.get('symbols') or [])
except Exception:
    new_syms = []
before_syms = []
try:
    obj = json.loads((lockdir/'symbols_before.json').read_text(encoding='utf-8'))
    before_syms = list(obj.get('symbols') or [])
except Exception:
    before_syms = []
# Restore only when previous had target size and new has fewer than target
if top_n > 0 and len(new_syms) < top_n and len(before_syms) == top_n:
    try:
        if cfgp.suffix.lower() in {'.yaml','.yml'}:
            import yaml  # type: ignore
            cfg = yaml.safe_load(cfgp.read_text(encoding='utf-8')) or {}
            uni = cfg.setdefault('universe', {})
            uni['symbols'] = before_syms
            cfgp.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding='utf-8')
        else:
            cfg = json.loads(cfgp.read_text(encoding='utf-8'))
            cfg.setdefault('universe', {})['symbols'] = before_syms
            cfgp.write_text(json.dumps(cfg, indent=2), encoding='utf-8')
        print(f"[trainer] refresh produced {len(new_syms)}< {top_n}; restored previous {len(before_syms)} symbols")
    except Exception as e:
        print(f"[trainer] failed to restore previous symbols: {e}")
else:
    # Log current size for visibility
    print(f"[trainer] refresh size now {len(new_syms)} (target {top_n})")
PY
    # Compute symbol delta and write metrics
    python - "$CONFIG" "$ARTIFACTS_DIR" <<'PY' || true
import json,sys,datetime as dt
from pathlib import Path
cfgp=Path(sys.argv[1]); art=Path(sys.argv[2])
lockdir=art/'.locks'
try:
  before=json.loads((lockdir/'symbols_before.json').read_text(encoding='utf-8'))
  before_set=set(before.get('symbols') or [])
except Exception:
  before_set=set()
try:
  txt=cfgp.read_text(encoding='utf-8')
  if cfgp.suffix.lower() in {'.yaml','.yml'}:
    import yaml
    cfg=yaml.safe_load(txt) or {}
  else:
    cfg=json.loads(txt)
  after_set=set((cfg.get('universe') or {}).get('symbols') or [])
except Exception:
  after_set=set()
added=sorted(after_set-before_set)
removed=sorted(before_set-after_set)
metrics_dir=art/'metrics'
metrics_dir.mkdir(parents=True, exist_ok=True)
stamp=int(dt.datetime.utcnow().timestamp())
delta={'ts':stamp,'added_count':len(added),'removed_count':len(removed),'added':added,'removed':removed}
(metrics_dir/'symbol_delta.json').write_text(json.dumps(delta, indent=2), encoding='utf-8')
with (metrics_dir/'symbol_delta.csv').open('w', encoding='utf-8') as f:
  f.write('action,symbol\n')
  for s in added:
    f.write(f'added,{s}\n')
  for s in removed:
    f.write(f'removed,{s}\n')
print(f"SYMBOL_DELTA added={len(added)} removed={len(removed)}")
PY
  fi
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
  # Apply tuned thresholds overlay (if available)
  merge_threshold_overlay "$CONFIG"
  echo "[trainer] $(date -u +%F\ %T) running backtest (train)"
  TRAIN_START=$(date -u +%s)
  python -m cryptostorm backtest "$CONFIG" --features "$FEATURES_DIR" --features-interval "$FEATURES_INTERVAL" --workers "$WORKERS" || true
  rm -f "$LOCK_FILE" || true
  # Build threshold-change summary (best-effort)
  TH_MSG="$(python - "$ARTIFACTS_DIR" <<'PY'
import json, os, glob, sys, math, datetime as dt
from pathlib import Path
art = Path(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1] else Path(os.environ.get('ARTIFACTS_DIR','artifacts/run'))
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
  )"
  NOW_UTC=$(date -u +%F\ %T)
  # Read symbol delta summary line (if present)
  SYM_LINE=$(grep -m1 '^SYMBOL_DELTA ' "$ARTIFACTS_DIR/.locks/thresholds_before.json" 2>/dev/null || true)
  # Compute train duration and append to metrics log
  TRAIN_END=$(date -u +%s)
  TRAIN_DUR=$((TRAIN_END-TRAIN_START))
  # Persist trainer run metrics
  mkdir -p "$ARTIFACTS_DIR/metrics"
  python - "$ARTIFACTS_DIR" "$TRAIN_START" "$TRAIN_END" <<'PY' || true
import json,sys,datetime as dt
from pathlib import Path
art=Path(sys.argv[1])
start=int(sys.argv[2]); end=int(sys.argv[3])
row={'ts':end,'start_ts':start,'end_ts':end,'duration_s':end-start}
with (art/'metrics'/'trainer_runs.jsonl').open('a', encoding='utf-8') as f:
  f.write(json.dumps(row)+"\n")
PY
  # Compose final message: threshold changes + symbol delta + duration
  DUR_MIN=$((TRAIN_DUR/60)); DUR_SEC=$((TRAIN_DUR%60))
  # Read symbol delta counts from metrics if present (suppress errors quietly)
  SYM_COUNTS="$(
    python - "$ARTIFACTS_DIR" <<'PY' 2>/dev/null
import json,sys
from pathlib import Path
art=Path(sys.argv[1])
delta=art/'metrics'/'symbol_delta.json'
try:
  obj=json.loads(delta.read_text(encoding='utf-8'))
  add=obj.get('added_count',0) or 0
  rem=obj.get('removed_count',0) or 0
  print(f"symbols: +{int(add)} −{int(rem)}")
except Exception:
  pass
PY
  )"
  if [[ -n "$SYM_COUNTS" ]]; then
    FINAL_MSG="$TH_MSG\n$SYM_COUNTS\ntrain_duration: ${DUR_MIN}m ${DUR_SEC}s"
  else
    FINAL_MSG="$TH_MSG\ntrain_duration: ${DUR_MIN}m ${DUR_SEC}s"
  fi
  notify_telegram "$FINAL_MSG"
  echo "[trainer] $(date -u +%F\ %T) sleeping ${SLEEP_HOURS_EFF}h"
  sleep $(( SLEEP_HOURS_EFF * 3600 ))
done
