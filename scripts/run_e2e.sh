#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  scripts/run_e2e.sh -c <config> [options]

Options:
  -c, --config <path>     Path to YAML/JSON config (required)
  -d, --data <dir>        Data output directory (default: data)
  -f, --features <dir>    Features output directory (default: features)
  -m, --min-coverage <r>  Min coverage ratio for audit (default: 0.95)
  -l, --log-level <lvl>   Log level for retrieval (default: INFO)
      --features-interval 5m|15m|auto  Backtest features cadence (default: auto)
      --http-debug        Enable HTTP request/response debug logs for retrieve
      --dry-run           Dry-run retrieval (skips feature and audit)
  -h, --help              Show this help

Notes:
  - API key is resolved by the config (env var or acquisition.coinglass.api_key_file).
  - On --dry-run, only validate + retrieve are executed.
USAGE
}

CONFIG=""
DATA_DIR="data"
FEATURES_DIR="features"
MIN_COVERAGE="0.95"
LOG_LEVEL="INFO"
DRY_RUN="0"
HTTP_DEBUG="0"
FEATURES_INTERVAL="auto"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -c|--config) CONFIG="$2"; shift 2;;
    -d|--data) DATA_DIR="$2"; shift 2;;
    -f|--features) FEATURES_DIR="$2"; shift 2;;
    -m|--min-coverage) MIN_COVERAGE="$2"; shift 2;;
    -l|--log-level) LOG_LEVEL="$2"; shift 2;;
    --features-interval) FEATURES_INTERVAL="$2"; shift 2;;
    --dry-run) DRY_RUN="1"; shift;;
    --http-debug) HTTP_DEBUG="1"; shift;;
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

echo "[1/4] Validating config: $CONFIG"
if [[ "$DRY_RUN" == "1" ]]; then
  python -m cryptostorm validate "$CONFIG" --no-require-env
else
  python -m cryptostorm validate "$CONFIG"
fi

echo "[2/4] Retrieving raw data -> $DATA_DIR (dry_run=$DRY_RUN)"
if [[ "$DRY_RUN" == "1" ]]; then
  CRYPTOSTORM_HTTP_DEBUG="$HTTP_DEBUG" python -m cryptostorm retrieve "$CONFIG" --out "$DATA_DIR" --dry-run --log-level "$LOG_LEVEL"
  echo "Dry-run mode: skipping features build and audit"
  exit 0
else
  CRYPTOSTORM_HTTP_DEBUG="$HTTP_DEBUG" python -m cryptostorm retrieve "$CONFIG" --out "$DATA_DIR" --log-level "$LOG_LEVEL"
fi

echo "[3/6] Building features -> $FEATURES_DIR"
python -m cryptostorm feature "$CONFIG" --data "$DATA_DIR" --out "$FEATURES_DIR"

echo "[4/6] Running backtest ($FEATURES_INTERVAL) -> artifacts/<RUN_ID>/"
python -m cryptostorm backtest "$CONFIG" --features "$FEATURES_DIR" --features-interval "$FEATURES_INTERVAL"

echo "[5/6] Auditing 30d coverage (min_ratio=$MIN_COVERAGE)"
python -m cryptostorm audit "$CONFIG" --data "$DATA_DIR" --min-ratio "$MIN_COVERAGE"

echo "[6/6] Generating interactive price+alerts (Plotly) -> reports/"
python -m cryptostorm report-price "$CONFIG" --data "$DATA_DIR" --out reports

# Optional: send Telegram alerts (storm only) if credentials are present and SEND_TELEGRAM=1
if [[ "${SEND_TELEGRAM:-0}" == "1" && -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
  echo "[opt] Sending Telegram alerts (storm)"
  python -m cryptostorm alert-telegram "$CONFIG" --kinds storm --only-new || true
fi

echo "E2E pipeline completed successfully."
