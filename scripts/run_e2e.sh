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
      --features-interval 5m|15m|auto  Backtest features cadence (default: 15m)
      --soft-fail         Do not fail pipeline on audit coverage failures
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
FEATURES_INTERVAL="15m"
SOFT_FAIL_AUDIT="1"
AUTO_WORKERS=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    -c|--config) CONFIG="$2"; shift 2;;
    -d|--data) DATA_DIR="$2"; shift 2;;
    -f|--features) FEATURES_DIR="$2"; shift 2;;
    -m|--min-coverage) MIN_COVERAGE="$2"; shift 2;;
    -l|--log-level) LOG_LEVEL="$2"; shift 2;;
    --features-interval) FEATURES_INTERVAL="$2"; shift 2;;
    --soft-fail) SOFT_FAIL_AUDIT="1"; shift;;
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

# Detect max workers (Linux/macOS) unless overridden by env CRYPTOSTORM_WORKERS
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
if [[ -z "$WORKERS" || "$WORKERS" -lt 1 ]]; then WORKERS=1; fi

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

echo "[3/6] Building 15m features -> $FEATURES_DIR"
python -m cryptostorm feature "$CONFIG" --data "$DATA_DIR" --out "$FEATURES_DIR" --interval 15m --workers "$WORKERS" --log-level "$LOG_LEVEL"

echo "[4/6] Running backtest ($FEATURES_INTERVAL) -> artifacts/<RUN_ID>/"
python -m cryptostorm backtest "$CONFIG" --features "$FEATURES_DIR" --features-interval "$FEATURES_INTERVAL" --workers "$WORKERS"

echo "[5/6] Auditing 30d coverage (min_ratio=$MIN_COVERAGE)"
if [[ "$SOFT_FAIL_AUDIT" == "1" ]]; then
  python -m cryptostorm audit "$CONFIG" --data "$DATA_DIR" --min-ratio "$MIN_COVERAGE" --soft-fail
else
  python -m cryptostorm audit "$CONFIG" --data "$DATA_DIR" --min-ratio "$MIN_COVERAGE"
fi

echo "[6/6] Generating interactive price+alerts (Plotly) -> reports/"
python -m cryptostorm report-price "$CONFIG" --data "$DATA_DIR" --out reports

# Optional: send Telegram alerts (storm only) if credentials are present and SEND_TELEGRAM=1
if [[ "${SEND_TELEGRAM:-0}" == "1" && -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
  echo "[opt] Sending Telegram alerts (storm)"
  python -m cryptostorm alert-telegram "$CONFIG" --kinds storm --only-new || true
fi

echo "E2E pipeline completed successfully."
