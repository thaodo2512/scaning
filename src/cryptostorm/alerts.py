from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .config import load_config, validate_config, EffectiveConfig


def _merge_alerts(artifacts_root: Path) -> Path:
    alerts_dir = artifacts_root / "alerts"
    alerts_dir.mkdir(parents=True, exist_ok=True)
    rows: List[Dict[str, Any]] = []
    if not alerts_dir.exists():
        return alerts_dir / "all_alerts.csv"
    for fp in sorted(alerts_dir.glob("*.csv")):
        name = fp.name.lower()
        if name in {"all_alerts.csv"}:
            continue
        # skip non-symbol CSVs if any
        try:
            with fp.open("r", encoding="utf-8") as f:
                rdr = csv.DictReader(f)
                for r in rdr:
                    try:
                        rows.append({
                            "ts": int(r.get("ts") or 0),
                            "symbol": str(r.get("symbol") or ""),
                            "kind": str(r.get("kind") or ""),
                            "score": (r.get("score") if r.get("score") not in (None, "") else ""),
                            "threshold": (r.get("threshold") if r.get("threshold") not in (None, "") else ""),
                        })
                    except Exception:
                        continue
        except Exception:
            continue
    rows.sort(key=lambda x: (x.get("ts") or 0, x.get("symbol") or "", x.get("kind") or ""))
    out_fp = alerts_dir / "all_alerts.csv"
    with out_fp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["ts", "symbol", "kind", "score", "threshold"])
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return out_fp


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="CryptoStorm alerts utilities")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_merge = sub.add_parser("merge", help="Merge all per-symbol alerts into alerts/all_alerts.csv")
    p_merge.add_argument("config", type=str)
    p_merge.add_argument("--artifacts", type=str, help="Artifacts root override; defaults to run.artifacts_root/run_id")

    args = parser.parse_args(argv)

    if args.cmd == "merge":
        cfg = load_config(args.config)
        eff, warns, errs = validate_config(cfg, require_env=False)
        if errs:
            for e in errs:
                print(f"error: {e}")
            return 2
        run_id = (cfg.get("run") or {}).get("run_id") or eff.run_id
        artifacts_root = Path(args.artifacts) if args.artifacts else Path((cfg.get("run") or {}).get("artifacts_root", "./artifacts")) / str(run_id)
        out_fp = _merge_alerts(artifacts_root)
        print(str(out_fp))
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

