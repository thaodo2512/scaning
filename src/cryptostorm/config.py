from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, MutableMapping, Optional, Tuple


# -------------------------
# Public data structures
# -------------------------


@dataclass
class DatasetConfig:
    name: str
    interval: Optional[str]
    mode: Optional[str]  # "exchange" | "aggregated" | None
    enabled: bool


@dataclass
class EffectiveConfig:
    run_id: str
    artifacts_root: Path
    symbols: List[str]
    symbol_to_coin: Dict[str, str]
    api_key_env: str
    api_key_file: Optional[Path]
    api_key_present: bool
    orderbook_range_bp: Optional[int]
    max_ob_age_s: Optional[int]
    days: int
    datasets: Dict[str, DatasetConfig] = field(default_factory=dict)


# -------------------------
# Loading helpers
# -------------------------


def _load_yaml_or_json(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore
        except Exception as exc:  # pragma: no cover - import error path
            raise RuntimeError(
                "PyYAML is required to load YAML configs. Install with 'pip install pyyaml'."
            ) from exc
        return dict(yaml.safe_load(text) or {})
    # Fallback to JSON
    return json.loads(text)


# -------------------------
# Normalization helpers
# -------------------------


def _norm_intervals(raw: Mapping[str, Any]) -> Dict[str, Any]:
    intervals = dict(raw or {})
    out: Dict[str, Any] = {}
    synonyms = {
        "futures_ohlcv": "futures_ohlcv_5m",
        "spot_ohlcv": "spot_ohlcv_5m",
        "funding_5m": "funding_pred_5m",
        "funding_pred_5m": "funding_pred_5m",
        "funding_8h": "funding_8h",
        "oi_ohlc": "oi_5m_ohlc",
        "oi_5m_ohlc": "oi_5m_ohlc",
        "taker_volume": "taker_futures_5m",
        "taker_futures_5m": "taker_futures_5m",
        "taker_spot_5m": "taker_spot_5m",
        "liquidation": "liquidation_5m",
        "liquidation_5m": "liquidation_5m",
        "orderbook_sample": "orderbook_sample",
        "orderbook_range_bp": "orderbook_range_bp",
    }
    for k, v in intervals.items():
        key = synonyms.get(k, k)
        out[key] = v
    return out


def _norm_per_series_mode(raw: Mapping[str, Any]) -> Dict[str, Any]:
    modes = dict(raw or {})
    out: Dict[str, Any] = {}
    synonyms = {
        "funding_5m": "funding_pred_5m",
        "funding_pred_5m": "funding_pred_5m",
        "oi": "oi_5m",
        "oi_5m": "oi_5m",
        "orderbook_futures": "orderbook",
        "orderbook_spot": "orderbook",
    }
    for k, v in modes.items():
        key = synonyms.get(k, k)
        out[key] = v
    return out


def _bool_get(d: Mapping[str, Any], key: str, default: bool) -> bool:
    v = d.get(key, default)
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.lower() in {"1", "true", "yes", "on"}
    if isinstance(v, (int, float)):
        return bool(v)
    return default


def _compute_datasets(
    intervals: Mapping[str, Any],
    modes: Mapping[str, Any],
    enable: Mapping[str, Any],
) -> Dict[str, DatasetConfig]:
    # canonical dataset keys we recognize
    dataset_keys: List[Tuple[str, str, str]] = [
        ("futures_ohlcv_5m", "futures_ohlcv", "core"),
        ("spot_ohlcv_5m", "spot_ohlcv", "optional"),
        ("funding_8h", "funding_8h", "core"),
        ("funding_pred_5m", "funding_pred_5m", "optional"),
        ("oi_5m_ohlc", "oi_5m", "core"),
        ("taker_futures_5m", "taker_futures", "core"),
        ("taker_spot_5m", "taker_spot", "optional"),
        ("liquidation_5m", "liquidation", "core"),
        ("orderbook_futures_5m", "orderbook", "core"),
        ("orderbook_spot_5m", "orderbook", "optional"),
    ]

    datasets: Dict[str, DatasetConfig] = {}
    for interval_key, mode_key, tier in dataset_keys:
        # Special handling: both orderbook datasets share the same interval key
        if interval_key in {"orderbook_futures_5m", "orderbook_spot_5m"}:
            interval = intervals.get("orderbook_sample")
        else:
            interval = intervals.get(interval_key)
        mode = modes.get(mode_key)

        # Determine whether the dataset appears configured at all
        configured = (interval is not None) and (mode is not None)

        # enabled: if enable flag present, respect it; otherwise:
        # - default True only for core datasets that are configured;
        # - default False otherwise (prevents requiring unconfigured datasets).
        default_enabled = (tier == "core") and configured
        enabled = _bool_get(enable, interval_key, default_enabled)

        # Special-case some enable flags with different names in spec
        if interval_key == "funding_pred_5m":
            enabled = _bool_get(enable, "funding_pred_5m", enabled)
        if interval_key == "spot_ohlcv_5m":
            enabled = _bool_get(enable, "spot_ohlcv_5m", enabled)
        if interval_key == "taker_spot_5m":
            enabled = _bool_get(enable, "taker_spot_5m", enabled)
        if interval_key == "orderbook_spot_5m":
            enabled = _bool_get(enable, "orderbook_spot_5m", enabled)

        datasets[interval_key] = DatasetConfig(
            name=interval_key,
            interval=str(interval) if interval is not None else None,
            mode=str(mode) if mode is not None else None,
            enabled=enabled,
        )
    return datasets


# -------------------------
# Public API
# -------------------------


def load_config(path: str | Path) -> Dict[str, Any]:
    """Load a config file (YAML or JSON) and return as a nested dict.

    Does not validate.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config not found: {p}")
    return _load_yaml_or_json(p)


def validate_config(
    cfg: Mapping[str, Any], *, require_env: bool = True
) -> Tuple[EffectiveConfig, List[str], List[str]]:
    """Validate and normalize configuration.

    Returns a tuple of (effective_config, warnings, errors).
    - errors non-empty indicates config is invalid.
    """
    warnings: List[str] = []
    errors: List[str] = []

    run = dict(cfg.get("run", {}))
    universe = dict(cfg.get("universe", {}))
    acquisition = dict(cfg.get("acquisition", {}))
    conventions = dict(cfg.get("conventions", {}))

    # Run
    run_id = run.get("run_id")
    artifacts_root = run.get("artifacts_root", "./artifacts")
    if not run_id or not isinstance(run_id, str):
        errors.append("run.run_id must be a non-empty string (e.g., YYYYMMDD-HHMMSS)")
    artifacts_path = Path(artifacts_root)

    # Universe
    symbols = universe.get("symbols") or []
    if not isinstance(symbols, list) or not all(isinstance(s, str) for s in symbols):
        errors.append("universe.symbols must be a list of strings")

    # Acquisition
    days = acquisition.get("days", 30)
    if not isinstance(days, int) or days <= 0:
        errors.append("acquisition.days must be a positive integer")

    coinglass = dict(acquisition.get("coinglass", {}))
    api_key_env = coinglass.get("api_key_env", "COINGLASS_API_KEY")
    api_key_file_raw = coinglass.get("api_key_file")
    api_key_file: Optional[Path] = None
    if isinstance(api_key_file_raw, str) and api_key_file_raw.strip():
        api_key_file = Path(api_key_file_raw)
    if not isinstance(api_key_env, str) or not api_key_env:
        errors.append("acquisition.coinglass.api_key_env must be a non-empty string")
    env_present = os.getenv(api_key_env) is not None
    file_present = False
    if api_key_file is not None and api_key_file.exists():
        try:
            content = api_key_file.read_text(encoding="utf-8").strip()
            file_present = bool(content)
        except Exception:
            file_present = False
    api_key_present = env_present or file_present
    if require_env and not api_key_present:
        msg = (
            f"No API key found: set env {api_key_env} or provide acquisition.coinglass.api_key_file"
        )
        errors.append(msg)

    # Intervals & modes
    intervals = _norm_intervals(dict(coinglass.get("intervals", {})))
    modes = _norm_per_series_mode(dict(coinglass.get("per_series_mode", {})))
    enable = dict(acquisition.get("enable", {}) or cfg.get("enable", {}))

    # Orderbook params
    orderbook_range_bp = intervals.get("orderbook_range_bp")
    if orderbook_range_bp is not None and (not isinstance(orderbook_range_bp, int) or orderbook_range_bp <= 0):
        errors.append("intervals.orderbook_range_bp must be a positive integer (bps)")

    # Conventions
    symbol_to_coin = dict(conventions.get("symbol_to_coin", {}))
    missing_map = [s for s in symbols if s not in symbol_to_coin]
    if missing_map:
        warnings.append(
            f"conventions.symbol_to_coin missing mappings for: {', '.join(missing_map)}"
        )
    units = dict(conventions.get("units", {}))
    if units.get("taker_volume") not in {None, "base"}:
        warnings.append("units.taker_volume should be 'base' per spec; got %r" % units.get("taker_volume"))

    # Compute datasets
    datasets = _compute_datasets(intervals, modes, enable)

    # Required datasets sanity: only enforce requirements for datasets that are enabled
    required_keys = [
        "futures_ohlcv_5m",
        "funding_8h",
        "oi_5m_ohlc",
        "taker_futures_5m",
        "liquidation_5m",
    ]
    for k in required_keys:
        ds = datasets.get(k)
        if not ds:
            errors.append(f"missing dataset config for {k}")
            continue
        if not ds.enabled:
            # If not enabled, do not require interval/mode
            continue
        if not ds.interval:
            errors.append(f"intervals.{k} must be defined (e.g., '5m'/'8h')")
        if not ds.mode:
            errors.append(f"per_series_mode for {k} must be defined ('exchange'/'aggregated')")

    # Build effective
    eff = EffectiveConfig(
        run_id=str(run_id) if run_id else "",
        artifacts_root=artifacts_path,
        symbols=[str(s) for s in symbols],
        api_key_env=api_key_env,
        api_key_file=api_key_file,
        api_key_present=api_key_present,
        orderbook_range_bp=int(orderbook_range_bp) if isinstance(orderbook_range_bp, int) else None,
        max_ob_age_s=int(conventions.get("orderbook", {}).get("max_snapshot_age_s", 60))
        if isinstance(conventions.get("orderbook"), Mapping)
        else None,
        days=int(days),
        symbol_to_coin={str(k): str(v) for k, v in symbol_to_coin.items()},
        datasets=datasets,
    )

    return eff, warnings, errors


# -------------------------
# CLI
# -------------------------


def _format_summary(eff: EffectiveConfig) -> str:
    lines = []
    lines.append(f"run_id: {eff.run_id}")
    lines.append(f"artifacts_root: {eff.artifacts_root}")
    lines.append(f"symbols: {', '.join(eff.symbols) if eff.symbols else '(none)'}")
    lines.append(
        f"api_key_env: {eff.api_key_env} (present={eff.api_key_present})"
    )
    lines.append(f"days: {eff.days}")
    if eff.orderbook_range_bp is not None:
        lines.append(f"orderbook_range_bp: {eff.orderbook_range_bp}")
    if eff.max_ob_age_s is not None:
        lines.append(f"orderbook_max_snapshot_age_s: {eff.max_ob_age_s}")
    lines.append("datasets:")
    for k in sorted(eff.datasets.keys()):
        ds = eff.datasets[k]
        lines.append(
            f"  - {ds.name}: enabled={ds.enabled} interval={ds.interval} mode={ds.mode}"
        )
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="CryptoStorm config validator")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_val = sub.add_parser("validate", help="Validate and summarize a config file")
    p_val.add_argument("path", type=str, help="Path to YAML/JSON config")
    p_val.add_argument(
        "--no-require-env",
        action="store_true",
        help="Do not require API key env var to be set",
    )
    args = parser.parse_args(argv)

    if args.cmd == "validate":
        cfg = load_config(args.path)
        eff, warns, errs = validate_config(cfg, require_env=not args.no_require_env)
        for w in warns:
            print(f"warning: {w}")
        if errs:
            for e in errs:
                print(f"error: {e}")
            return 2
        print(_format_summary(eff))
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
