#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def _utc_iso(ts: Optional[int] = None) -> str:
    import datetime as dt

    if ts is None:
        ts = int(time.time() * 1000)
    try:
        return dt.datetime.utcfromtimestamp(int(ts) / 1000).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return str(ts)


def _atomic_write_text(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _compact_jsonl_file(fp: Path, cutoff_ms: int, *, verbose: bool = False) -> Tuple[int, int, Optional[int]]:
    """Return (kept, dropped, last_ts)."""
    kept_lines: List[str] = []
    last_ts: Optional[int] = None
    if not fp.exists():
        return 0, 0, None
    try:
        with fp.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                    ts = int(obj.get("ts") or 0)
                except Exception:
                    # Keep malformed lines to avoid data loss
                    kept_lines.append(line)
                    continue
                if ts >= cutoff_ms:
                    kept_lines.append(json.dumps(obj, separators=(",", ":")) + "\n")
                    if last_ts is None or ts > last_ts:
                        last_ts = ts
        # Only rewrite if something would drop
        total = sum(1 for _ in fp.open("r", encoding="utf-8"))
        dropped = max(0, total - len(kept_lines))
        if dropped > 0:
            if verbose:
                print(f"compact: {fp} kept={len(kept_lines)} drop={dropped}")
            _atomic_write_text(fp, "".join(kept_lines))
        return len(kept_lines), dropped, last_ts
    except Exception as e:
        print(f"warn: failed to compact {fp}: {e}")
        return 0, 0, None


def _update_state_for_jsonl(jsonl_fp: Path, last_ts: Optional[int], *, verbose: bool = False) -> None:
    if last_ts is None:
        return
    # ds_key is filename without extension
    ds_key = jsonl_fp.stem
    state_dir = jsonl_fp.parent / ".state"
    sp = state_dir / f"{ds_key}.json"
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        payload = {"last_ts": int(last_ts), "updated_at": _utc_iso()}
        _atomic_write_text(sp, json.dumps(payload, separators=(",", ":")))
        if verbose:
            print(f"state: {sp} last_ts={last_ts}")
    except Exception as e:
        print(f"warn: failed to update state {sp}: {e}")


def _compact_csv_by_ts(fp: Path, cutoff_ms: int, *, verbose: bool = False) -> Tuple[int, int, Optional[int]]:
    if not fp.exists():
        return 0, 0, None
    try:
        with fp.open("r", encoding="utf-8") as f:
            rdr = csv.DictReader(f)
            fields = rdr.fieldnames or []
            rows = list(rdr)
        ts_col = "ts" if "ts" in fields else None
        if not ts_col:
            return 0, 0, None
        kept: List[Dict[str, str]] = []
        last_ts: Optional[int] = None
        for r in rows:
            try:
                ts = int(r.get(ts_col) or 0)
            except Exception:
                ts = 0
            if ts >= cutoff_ms:
                kept.append(r)
                if last_ts is None or ts > last_ts:
                    last_ts = ts
        dropped = max(0, len(rows) - len(kept))
        if dropped > 0:
            if verbose:
                print(f"compact: {fp} kept={len(kept)} drop={dropped}")
            tmp = fp.with_suffix(fp.suffix + ".tmp")
            with tmp.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fields)
                w.writeheader()
                for r in kept:
                    w.writerow(r)
            tmp.replace(fp)
        return len(kept), dropped, last_ts
    except Exception as e:
        print(f"warn: failed to compact CSV {fp}: {e}")
        return 0, 0, None


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Compact data/features/artifacts to last N days by ts")
    p.add_argument("--keep-days", type=int, default=45)
    p.add_argument("--data", type=str, default="data")
    p.add_argument("--features", type=str, default="features")
    p.add_argument("--artifacts", type=str, default="", help="Optional artifacts run dir to prune (scores/alerts)")
    p.add_argument("--prune-artifacts", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    cutoff_ms = int(time.time() * 1000) - int(args.keep_days) * 24 * 60 * 60 * 1000
    if args.verbose:
        print(f"cutoff: keep last {args.keep_days} days (ts>={cutoff_ms} ~ {_utc_iso(cutoff_ms)})")

    data_root = Path(args.data)
    features_root = Path(args.features)
    art_root = Path(args.artifacts) if args.artifacts else None

    # Data JSONL
    if data_root.exists():
        for sym_dir in sorted([p for p in data_root.iterdir() if p.is_dir()]):
            for jsonl_fp in sorted(sym_dir.glob("*.jsonl")):
                if args.dry_run:
                    continue
                kept, dropped, last_ts = _compact_jsonl_file(jsonl_fp, cutoff_ms, verbose=args.verbose)
                if dropped > 0 and last_ts is not None:
                    _update_state_for_jsonl(jsonl_fp, last_ts, verbose=args.verbose)

    # Features CSV
    if features_root.exists():
        for sym_dir in sorted([p for p in features_root.iterdir() if p.is_dir()]):
            for csv_fp in sorted(sym_dir.glob("features_*.csv")):
                if args.dry_run:
                    continue
                _compact_csv_by_ts(csv_fp, cutoff_ms, verbose=args.verbose)

    # Artifacts (scores/alerts) CSV
    if args.prune_artifacts and art_root and art_root.exists():
        for sub in ("scores", "alerts"):
            subdir = art_root / sub
            if not subdir.exists():
                continue
            for csv_fp in sorted(subdir.glob("*.csv")):
                if args.dry_run:
                    continue
                _compact_csv_by_ts(csv_fp, cutoff_ms, verbose=args.verbose)

    if args.verbose:
        print("compact: done")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
