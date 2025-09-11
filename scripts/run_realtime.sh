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
      --report-engine <eng>  Report engine: plotly|lightweight|price (default: plotly)

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
REPORT_ENGINE="plotly"

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

echo "[2/3] Realtime $( [[ "$ONCE" == "1" ]] && echo once || echo watch ) (online=$ONLINE)"
RT_ARGS=("$CONFIG" --data "$DATA_DIR" --features "$FEATURES_DIR" --poll-offset-s "$POLL_OFFSET" --jitter-s "$JITTER" --log-level "$LOG_LEVEL")
if [[ -n "$ARTIFACTS_DIR" ]]; then RT_ARGS+=(--artifacts "$ARTIFACTS_DIR"); fi
if [[ "$ONCE" == "1" ]]; then RT_ARGS+=(--once); fi
if [[ "$ONLINE" == "1" ]]; then RT_ARGS+=(--online-scoring); fi
if [[ "$SEND_TG" == "1" ]]; then RT_ARGS+=(--send-telegram --telegram-kinds "$TG_KINDS"); fi
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
