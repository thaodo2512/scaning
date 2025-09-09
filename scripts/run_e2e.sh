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

while [[ $# -gt 0 ]]; do
  case "$1" in
    -c|--config) CONFIG="$2"; shift 2;;
    -d|--data) DATA_DIR="$2"; shift 2;;
    -f|--features) FEATURES_DIR="$2"; shift 2;;
    -m|--min-coverage) MIN_COVERAGE="$2"; shift 2;;
    -l|--log-level) LOG_LEVEL="$2"; shift 2;;
    --dry-run) DRY_RUN="1"; shift;;
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
  python -m cryptostorm retrieve "$CONFIG" --out "$DATA_DIR" --dry-run --log-level "$LOG_LEVEL"
  echo "Dry-run mode: skipping features build and audit"
  exit 0
else
  python -m cryptostorm retrieve "$CONFIG" --out "$DATA_DIR" --log-level "$LOG_LEVEL"
fi

echo "[3/4] Building features -> $FEATURES_DIR"
python -m cryptostorm feature "$CONFIG" --data "$DATA_DIR" --out "$FEATURES_DIR"

echo "[4/5] Auditing 30d coverage (min_ratio=$MIN_COVERAGE)"
python -m cryptostorm audit "$CONFIG" --data "$DATA_DIR" --min-ratio "$MIN_COVERAGE"

echo "[5/5] Generating per-symbol HTML reports -> reports/"
python -m cryptostorm report "$CONFIG" --data "$DATA_DIR" --features "$FEATURES_DIR" --out reports

echo "E2E pipeline completed successfully."
