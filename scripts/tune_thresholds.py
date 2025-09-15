#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Per-symbol threshold tuner (15m/5m)")
    p.add_argument("--config", required=True, help="Path to base config (YAML/JSON)")
    p.add_argument("--features", required=True, help="Features directory")
    p.add_argument("--features-interval", default="15m", choices=["5m", "15m"], help="Features cadence")
    p.add_argument(
        "--q-grid",
        default="0.972,0.976,0.979,0.982,0.985,0.988,0.990,0.992,0.994,0.996",
        help="Comma-separated q values to sweep (default: 10-run grid)",
    )
    p.add_argument("--artifacts-root", default="artifacts/tuning", help="Artifacts root for sweep outputs")
    p.add_argument("--workers", type=int, default=0, help="Backtest workers (0=auto; default: auto)")
    p.add_argument("--symbols", default="", help="Optional comma-separated allowlist of symbols")
    # Targets
    p.add_argument("--target-storms-day-min", type=float, default=0.5)
    p.add_argument("--target-storms-day-max", type=float, default=2.0)
    p.add_argument("--target-precision-min", type=float, default=0.60)
    p.add_argument("--target-lead-min", type=float, default=15.0)
    p.add_argument("--assume-days", type=float, default=30.0, help="Assumed backtest days if not in metrics")
    # Outputs
    p.add_argument("--out-json", default="artifacts/tuning/recommendations.json")
    p.add_argument("--out-csv", default="artifacts/tuning/recommendations.csv")
    p.add_argument("--overlay-yaml", default="artifacts/tuning/overlay_thresholds.yaml")
    p.add_argument("--skip-backtests", action="store_true", help="Only parse existing q-* artifacts")
    return p.parse_args()


def _load_config(path: Path) -> Dict[str, Any]:
    txt = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        import yaml  # type: ignore

        return dict(yaml.safe_load(txt) or {})
    return json.loads(txt)


def _dump_config(cfg: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() in {".yaml", ".yml"}:
        import yaml  # type: ignore

        path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    else:
        path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _run_backtest_for_q(base_cfg: Path, q: float, args: argparse.Namespace) -> None:
    root = Path(args.artifacts_root) / f"q-{q:.3f}"
    metrics_fp = root / "metrics" / "metrics.json"
    if metrics_fp.exists():
        return
    # Write a temporary config overriding model.threshold_q
    cfg = _load_config(base_cfg)
    model = dict(cfg.get("model", {}))
    model["threshold_q"] = float(q)
    cfg["model"] = model
    tmp_cfg = root / f"config_q_{q:.3f}.yaml"
    _dump_config(cfg, tmp_cfg)
    # Run backtest
    # Resolve workers (default: auto from CPU count)
    try:
        workers = int(args.workers)
        if workers <= 0:
            import os as _os
            workers = int(_os.cpu_count() or 1)
    except Exception:
        workers = 1

    cmd = [
        sys.executable,
        "-m",
        "cryptostorm",
        "backtest",
        str(tmp_cfg),
        "--features",
        args.features,
        "--features-interval",
        args.features_interval,
        "--artifacts-root",
        str(root),
        "--workers",
        str(workers),
    ]
    if args.symbols:
        # Not natively supported by CLI yet; keep placeholder for future expansion
        pass
    subprocess.run(cmd, check=True)


def _storms_per_day(entry: Mapping[str, Any], assume_days: float) -> float:
    storms = float(entry.get("storm_count") or 0.0)
    days = float(assume_days)
    return storms / days if days > 0 else 0.0


def _precision(entry: Mapping[str, Any]) -> float:
    v = entry.get("precision")
    if isinstance(v, (int, float)):
        return float(v)
    tp = float(entry.get("true_positive") or 0.0)
    fp = float(entry.get("false_positive") or 0.0)
    denom = tp + fp
    return (tp / denom) if denom > 0 else 0.0


def _avg_lead_min(entry: Mapping[str, Any]) -> float:
    v = entry.get("avg_lead_min")
    return float(v) if isinstance(v, (int, float)) else 0.0


def _load_metrics(artifacts_root: Path) -> Dict[float, Dict[str, Dict[str, float]]]:
    """Return data[q][symbol] = metrics dict"""
    data: Dict[float, Dict[str, Dict[str, float]]] = {}
    for q_dir in sorted([p for p in artifacts_root.glob("q-*") if p.is_dir()]):
        try:
            q = float(q_dir.name.split("-", 1)[1])
        except Exception:
            continue
        mfp = q_dir / "metrics" / "metrics.json"
        if not mfp.exists():
            continue
        try:
            m = json.loads(mfp.read_text(encoding="utf-8") or "{}")
        except Exception:
            continue
        sym_map = {}
        symbols = m.get("symbols") or {}
        if isinstance(symbols, Mapping):
            for sym, rec in symbols.items():
                try:
                    sym_map[sym] = {
                        "storms_day": _storms_per_day(rec, assume_days=float(30.0)),
                        "precision": _precision(rec),
                        "avg_lead_min": _avg_lead_min(rec),
                        "storm_count": rec.get("storm_count"),
                    }
                except Exception:
                    continue
        data[q] = sym_map
    return data


def _select_q_for_symbol(
    q_grid: List[float],
    records_by_q: Mapping[float, Mapping[str, float]],
    targets: Mapping[str, float],
) -> Tuple[float | None, Mapping[str, float] | None]:
    lo, hi = float(targets["storms_lo"]), float(targets["storms_hi"])
    mid = 0.5 * (lo + hi)

    candidates: List[Tuple[float, float, float, Mapping[str, float]]] = []
    for q in q_grid:
        m = records_by_q.get(q)
        if not m:
            continue
        if m.get("precision", 0.0) >= float(targets["precision_min"]) and m.get("avg_lead_min", 0.0) >= float(
            targets["lead_min"]
        ):
            dist = abs(float(m.get("storms_day", 0.0)) - mid)
            candidates.append((dist, -q, q, m))
    if candidates:
        candidates.sort()
        return candidates[0][2], candidates[0][3]

    # Fallback: favor best precision, then closeness to mid, then higher q
    fallbacks: List[Tuple[float, float, float, float, Mapping[str, float]]] = []
    for q in q_grid:
        m = records_by_q.get(q)
        if not m:
            continue
        fallbacks.append((-float(m.get("precision", 0.0)), abs(float(m.get("storms_day", 0.0)) - mid), -q, q, m))
    if not fallbacks:
        return None, None
    fallbacks.sort()
    return fallbacks[0][3], fallbacks[0][4]


def _write_outputs(
    selections: Mapping[str, Mapping[str, Any]],
    args: argparse.Namespace,
    targets: Mapping[str, float],
    q_grid: List[float],
) -> None:
    Path(args.out_json).parent.mkdir(parents=True, exist_ok=True)
    rec_json = {
        "targets": {
            "storms_day": [targets["storms_lo"], targets["storms_hi"]],
            "precision_min": targets["precision_min"],
            "avg_lead_min": targets["lead_min"],
        },
        "q_grid": q_grid,
        "per_symbol": {sym: rec["q"] for sym, rec in selections.items()},
        "details": selections,
    }
    Path(args.out_json).write_text(json.dumps(rec_json, indent=2, sort_keys=True), encoding="utf-8")

    with Path(args.out_csv).open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "q", "storms_day", "precision", "avg_lead_min", "storm_count"])
        for sym in sorted(selections.keys()):
            rec = selections[sym]
            m = rec["metrics"]
            w.writerow([sym, f"{rec['q']:.6f}", f"{m['storms_day']:.6f}", f"{m['precision']:.6f}", f"{m['avg_lead_min']:.3f}", m.get("storm_count", "")])

    # Minimal overlay YAML
    ov = Path(args.overlay_yaml)
    ov.parent.mkdir(parents=True, exist_ok=True)
    lines = ["model:", "  threshold_q_per_symbol:"]
    for sym in sorted(selections.keys()):
        lines.append(f"    {sym}: {selections[sym]['q']:.6f}")
    ov.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    base_cfg = Path(args.config)
    q_grid = sorted(set(float(s) for s in args.q_grid.split(",") if s.strip()))
    art_root = Path(args.artifacts_root)
    if not args.skip_backtests:
        for q in q_grid:
            _run_backtest_for_q(base_cfg, q, args)

    data = _load_metrics(art_root)

    # Build universe of symbols (union across q)
    sym_set = set()
    for q in q_grid:
        sym_set.update((data.get(q) or {}).keys())
    if args.symbols:
        allow = {s.strip() for s in args.symbols.split(",") if s.strip()}
        sym_list = [s for s in sorted(sym_set) if s in allow]
    else:
        sym_list = sorted(sym_set)

    targets = {
        "storms_lo": float(args.target_storms_day_min),
        "storms_hi": float(args.target_storms_day_max),
        "precision_min": float(args.target_precision_min),
        "lead_min": float(args.target_lead_min),
    }

    selections: Dict[str, Dict[str, Any]] = {}
    for sym in sym_list:
        recs_by_q: Dict[float, Dict[str, float]] = {}
        for q in q_grid:
            sym_map = data.get(q) or {}
            if sym in sym_map:
                recs_by_q[q] = sym_map[sym]
        q_sel, metrics = _select_q_for_symbol(q_grid, recs_by_q, targets)
        if q_sel is None or metrics is None:
            continue
        selections[sym] = {"q": q_sel, "metrics": metrics}

    _write_outputs(selections, args, targets, q_grid)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
