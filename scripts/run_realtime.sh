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
      --poll-offset-s <sec>  Offset seconds after bar close (default: 10)
      --jitter-s <sec>       Random jitter seconds (default: 2)
      --send-telegram        Send alerts via Telegram (env creds required)
      --telegram-kinds <k>   Kinds to send (storm,pre_alert) (default: storm)
      --log-level <lvl>      Log level for realtime (default: INFO)
      --report-engine <eng>  Report engine: plotly|lightweight|price (default: price)
      --build-reports        Build reports after each realtime cycle and update index.html
      --ensure-data          Audit 30d coverage and backfill missing raw data
      --ensure-min-ratio <r> Coverage threshold for ensure step (default: 0.95)
      --ensure-workers <n>   Workers for ensure backfill (default: 8)
      --ensure-rps <x>       Global RPS limit for ensure backfill (default: 3)
      --monitor              Launch console monitor after realtime step
      --monitor-view <v>     Monitor view: data|alerts (default: alerts)
      --monitor-symbols <n>  Symbols to show in monitor (default: 20)

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
ENSURE_WORKERS="8"
ENSURE_RPS="3"
MONITOR="0"
MONITOR_VIEW="alerts"
MONITOR_SYMBOLS="20"

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
    --ensure-data) ENSURE_DATA="1"; shift;;
    --ensure-min-ratio) ENSURE_MIN_RATIO="$2"; shift 2;;
    --ensure-workers) ENSURE_WORKERS="$2"; shift 2;;
    --ensure-rps) ENSURE_RPS="$2"; shift 2;;
    --monitor) MONITOR="1"; shift;;
    --monitor-view) MONITOR_VIEW="$2"; shift 2;;
    --monitor-symbols) MONITOR_SYMBOLS="$2"; shift 2;;
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

echo "[1/3] Validate config"
python -m cryptostorm validate "$CONFIG" --no-require-env || true

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
RT_ARGS=("$CONFIG" --data "$DATA_DIR" --features "$FEATURES_DIR" --poll-offset-s "$POLL_OFFSET" --jitter-s "$JITTER" --log-level "$LOG_LEVEL")
if [[ -n "$ARTIFACTS_DIR" ]]; then RT_ARGS+=(--artifacts "$ARTIFACTS_DIR"); fi
if [[ "$ONCE" == "1" ]]; then RT_ARGS+=(--once); fi
if [[ "$ONLINE" == "1" ]]; then RT_ARGS+=(--online-scoring); fi
if [[ "$SEND_TG" == "1" ]]; then RT_ARGS+=(--send-telegram --telegram-kinds "$TG_KINDS"); fi
if [[ "$BUILD_REPORTS" == "1" ]]; then RT_ARGS+=(--build-reports --reports "$REPORTS_DIR" --report-engine "$REPORT_ENGINE"); fi
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
