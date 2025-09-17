#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  scripts/run_realtime.sh -c <config> [options]

Options:
  -c, --config <path>        Path to YAML/JSON config (required)
  -d, --data <dir>           Data directory (default: data)
  -f, --features <dir>       Features directory (default: features)
  -a, --artifacts <dir>      Artifacts root override (default from config)
  -r, --reports <dir>        Reports directory (default: reports)
      --online               Use online scoring (no retrain) in realtime loop
      --once                 Run a single realtime cycle (default)
      --watch                Run continuous 5m-aligned loop (omit --once)
      --poll-offset-s <sec>  Offset seconds after bar close (default: 15)
      --jitter-s <sec>       Random jitter seconds (default: 2)
      --send-telegram        Send alerts via Telegram (env creds required)
      --telegram-kinds <k>   Kinds to send (storm,pre_alert) (default: storm)
      --log-level <lvl>      Log level for realtime (default: INFO)
      --report-engine <eng>  Report engine: plotly|lightweight|price (default: price)
      --build-reports        Build reports after each realtime cycle and update index.html
      --bootstrap-tuned      One-time bootstrap before realtime: binance-top -> retrieve -> features(15m) -> tune q -> merge overlay -> backtest
      --top <n>              Top N symbols for bootstrap binance-top (default: 200)
      --q-grid <list>        Custom q-grid for tuner (comma-separated, default tuner grid)
      --ensure-data          Audit 30d coverage and backfill missing raw data
      --ensure-min-ratio <r> Coverage threshold for ensure step (default: 0.95)
      --ensure-workers <n>   Workers for ensure backfill (default: 1)
      --ensure-rps <x>       Global RPS limit for ensure backfill (default: 3)
      --monitor              Launch console monitor after realtime step
      --monitor-view <v>     Monitor view: data|alerts (default: alerts)
      --monitor-symbols <n>  Symbols to show in monitor (default: 20)
      --reload-config        Reload config each cycle if the file changed (universe/tuning updates)

Env:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (for --send-telegram)

Notes:
  - This script uses the unified 'realtime' CLI to do retrieve → features(update-last) → score.
  - After a single cycle (--once), it also rebuilds reports.
USAGE
}

CONFIG=""
DATA_DIR="data"
FEATURES_DIR="features"
ARTIFACTS_DIR=""
REPORTS_DIR="reports"
ONLINE="0"
ONCE="1"
POLL_OFFSET="10"
JITTER="2"
SEND_TG="0"
TG_KINDS="storm"
LOG_LEVEL="INFO"
REPORT_ENGINE="price"
BUILD_REPORTS="0"
ENSURE_DATA="0"
ENSURE_MIN_RATIO="0.95"
ENSURE_WORKERS="1"
ENSURE_RPS="3"
MONITOR="0"
MONITOR_VIEW="alerts"
MONITOR_SYMBOLS="20"
WORKERS_AUTO=""
RELOAD_CFG="0"
BOOTSTRAP_TUNED="0"
TOP_N="200"
Q_GRID=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    -c|--config) CONFIG="$2"; shift 2;;
    -d|--data) DATA_DIR="$2"; shift 2;;
    -f|--features) FEATURES_DIR="$2"; shift 2;;
    -a|--artifacts) ARTIFACTS_DIR="$2"; shift 2;;
    -r|--reports) REPORTS_DIR="$2"; shift 2;;
    --online) ONLINE="1"; shift;;
    --once) ONCE="1"; shift;;
    --watch) ONCE="0"; shift;;
    --poll-offset-s) POLL_OFFSET="$2"; shift 2;;
    --jitter-s) JITTER="$2"; shift 2;;
    --send-telegram) SEND_TG="1"; shift;;
    --telegram-kinds) TG_KINDS="$2"; shift 2;;
    --log-level) LOG_LEVEL="$2"; shift 2;;
    --report-engine) REPORT_ENGINE="$2"; shift 2;;
    --build-reports) BUILD_REPORTS="1"; shift;;
    --bootstrap-tuned) BOOTSTRAP_TUNED="1"; shift;;
    --top) TOP_N="$2"; shift 2;;
    --q-grid) Q_GRID="$2"; shift 2;;
    --ensure-data) ENSURE_DATA="1"; shift;;
    --ensure-min-ratio) ENSURE_MIN_RATIO="$2"; shift 2;;
    --ensure-workers) ENSURE_WORKERS="$2"; shift 2;;
    --ensure-rps) ENSURE_RPS="$2"; shift 2;;
    --monitor) MONITOR="1"; shift;;
    --monitor-view) MONITOR_VIEW="$2"; shift 2;;
    --monitor-symbols) MONITOR_SYMBOLS="$2"; shift 2;;
    --reload-config) RELOAD_CFG="1"; shift;;
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

# Detect max workers (Linux/macOS) unless overridden via CRYPTOSTORM_WORKERS
if [[ -n "${CRYPTOSTORM_WORKERS:-}" ]]; then
  WORKERS=${CRYPTOSTORM_WORKERS}
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
if [[ -z "$WORKERS" || "$WORKERS" -lt 1 ]]; then WORKERS=1; fi

echo "[1/3] Validate config"
python -m cryptostorm validate "$CONFIG" --no-require-env || true

# Helpers reused from trainer
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

merge_threshold_overlay_inline() {
  local cfg="$1"
  local overlay="artifacts/tuning/overlay_thresholds.yaml"
  if [[ ! -f "$overlay" ]]; then
    return
  fi
  echo "[bootstrap] merging tuned thresholds overlay -> $cfg ($overlay)"
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

# Optional tuned bootstrap (idempotent via marker)
if [[ "$BOOTSTRAP_TUNED" == "1" ]]; then
  ARTIFACTS_DIR=${ARTIFACTS_DIR:-"$(resolve_artifacts_root)"}
  BOOTSTRAP_MARKER="$ARTIFACTS_DIR/.bootstrapped"
  if [[ -f "$BOOTSTRAP_MARKER" ]]; then
    echo "[bootstrap] marker present at $BOOTSTRAP_MARKER — skipping bootstrap"
  else
    echo "[bootstrap] Selecting top $TOP_N symbols -> $CONFIG"
    python -m cryptostorm binance-top --top "$TOP_N" --out "$CONFIG" --print || true
    echo "[bootstrap] Retrieve (once) -> $DATA_DIR"
    python -m cryptostorm retrieve "$CONFIG" --out "$DATA_DIR" --watch --once --workers "$ENSURE_WORKERS" --rps "$ENSURE_RPS" || true
    echo "[bootstrap] Build 15m features -> $FEATURES_DIR"
    python -m cryptostorm feature "$CONFIG" --data "$DATA_DIR" --out "$FEATURES_DIR" --interval 15m || true
    echo "[bootstrap] Tune thresholds (q-sweep)"
    if [[ -n "$Q_GRID" ]]; then
      python scripts/tune_thresholds.py --config "$CONFIG" --features "$FEATURES_DIR" --features-interval 15m --q-grid "$Q_GRID"
    else
      python scripts/tune_thresholds.py --config "$CONFIG" --features "$FEATURES_DIR" --features-interval 15m
    fi
    merge_threshold_overlay_inline "$CONFIG"
    echo "[bootstrap] Persist tuned artifacts via backtest"
    python -m cryptostorm backtest "$CONFIG" --features "$FEATURES_DIR" --features-interval 15m || true
    mkdir -p "$(dirname "$BOOTSTRAP_MARKER")" && date -u +%F\ %T > "$BOOTSTRAP_MARKER" || true
    echo "[bootstrap] done"
  fi
fi

# Optional ensure-data step: audit and backfill
if [[ "$ENSURE_DATA" == "1" ]]; then
  echo "[1a] Audit data coverage (min_ratio=$ENSURE_MIN_RATIO)"
  set +e
  python -m cryptostorm audit "$CONFIG" --data "$DATA_DIR" --min-ratio "$ENSURE_MIN_RATIO"
  AUDIT_RC=$?
  set -e
  if [[ "$AUDIT_RC" -ne 0 ]]; then
    echo "[1b] Backfill missing data via retrieve --watch --once (workers=$ENSURE_WORKERS rps=$ENSURE_RPS)"
    python -m cryptostorm retrieve "$CONFIG" --out "$DATA_DIR" --watch --once --workers "$ENSURE_WORKERS" --rps "$ENSURE_RPS"
    echo "[1c] Re-run audit"
    python -m cryptostorm audit "$CONFIG" --data "$DATA_DIR" --min-ratio "$ENSURE_MIN_RATIO" || true
  fi
fi

echo "[2/3] Realtime $( [[ "$ONCE" == "1" ]] && echo once || echo watch ) (online=$ONLINE)"
RT_ARGS=("$CONFIG" --data "$DATA_DIR" --features "$FEATURES_DIR" --poll-offset-s "$POLL_OFFSET" --jitter-s "$JITTER" --log-level "$LOG_LEVEL" --workers "$WORKERS")
if [[ -n "$ARTIFACTS_DIR" ]]; then RT_ARGS+=(--artifacts "$ARTIFACTS_DIR"); fi
if [[ "$ONCE" == "1" ]]; then RT_ARGS+=(--once); fi
if [[ "$ONLINE" == "1" ]]; then RT_ARGS+=(--online-scoring); fi
if [[ "$SEND_TG" == "1" ]]; then RT_ARGS+=(--send-telegram --telegram-kinds "$TG_KINDS"); fi
if [[ "$BUILD_REPORTS" == "1" ]]; then RT_ARGS+=(--build-reports --reports "$REPORTS_DIR" --report-engine "$REPORT_ENGINE"); fi
if [[ "$RELOAD_CFG" == "1" ]]; then RT_ARGS+=(--reload-config); fi
python -m cryptostorm realtime "${RT_ARGS[@]}"

if [[ "$ONCE" == "1" ]]; then
  echo "[3/3] Build reports -> $REPORTS_DIR (engine=$REPORT_ENGINE)"
  case "$REPORT_ENGINE" in
    plotly)
      python -m cryptostorm report-plotly "$CONFIG" --data "$DATA_DIR" --out "$REPORTS_DIR" ;;
    lightweight)
      python -m cryptostorm report "$CONFIG" --data "$DATA_DIR" --features "$FEATURES_DIR" --out "$REPORTS_DIR" ;;
    price)
      python -m cryptostorm report-price "$CONFIG" --data "$DATA_DIR" --out "$REPORTS_DIR" ;;
    *)
      echo "Unknown report engine: $REPORT_ENGINE (use plotly|lightweight|price)" >&2; exit 2;;
  esac
  echo "Done. Open $REPORTS_DIR/<SYM>$( [[ "$REPORT_ENGINE" == "plotly" ]] && echo _plotly ).html"
else
  echo "Realtime watch started; press Ctrl+C to stop."
fi

# Optional monitor launch
if [[ "$MONITOR" == "1" ]]; then
  echo "[4/3] Launch console monitor (view=$MONITOR_VIEW)"
  MON_ARGS=("$CONFIG" --data "$DATA_DIR" --features "$FEATURES_DIR" --symbols "$MONITOR_SYMBOLS" --view "$MONITOR_VIEW")
  python -m cryptostorm monitor "${MON_ARGS[@]}"
fi
