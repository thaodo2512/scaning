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


def _parse_generated_alerts(alerts_dir: Path, symbols: list[str], since_ts: int | None = None) -> set[tuple[int, str, str]]:
    keys: set[tuple[int, str, str]] = set()
    for sym in symbols:
        fp = alerts_dir / f"{sym}.csv"
        if not fp.exists():
            continue
        try:
            with fp.open("r", encoding="utf-8") as f:
                rdr = csv.DictReader(f)
                for r in rdr:
                    try:
                        ts = int(r.get("ts") or 0)
                    except Exception:
                        continue
                    if since_ts is not None and ts < int(since_ts):
                        continue
                    k = (ts, str(r.get("symbol") or sym), str((r.get("kind") or "pre_alert")).strip() or "pre_alert")
                    keys.add(k)
        except Exception:
            continue
    # Also honor merged file if present (may include symbols outside current config)
    merged = alerts_dir / "all_alerts.csv"
    if merged.exists():
        try:
            with merged.open("r", encoding="utf-8") as f:
                rdr = csv.DictReader(f)
                for r in rdr:
                    try:
                        ts = int(r.get("ts") or 0)
                    except Exception:
                        continue
                    if since_ts is not None and ts < int(since_ts):
                        continue
                    sym = str(r.get("symbol") or "")
                    if sym and sym not in symbols:
                        # Skip symbols outside current universe for strictness
                        continue
                    k = (ts, sym, str((r.get("kind") or "pre_alert")).strip() or "pre_alert")
                    keys.add(k)
        except Exception:
            pass
    return keys


def _parse_sent_alerts(artifacts_root: Path, since_ts: int | None = None) -> set[tuple[int, str, str]]:
    """Parse alerts actually sent to Telegram from the send log JSONL.

    Uses artifacts/<RUN_ID>/alerts/telegram_send.jsonl entries. Falls back to
    telegram_sent.json registry if the send log is missing.
    """
    out: set[tuple[int, str, str]] = set()
    send_log = artifacts_root / "alerts" / "telegram_send.jsonl"
    if send_log.exists():
        try:
            with send_log.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    ts = obj.get("bar_ts")
                    sym = obj.get("symbol")
                    kind = (obj.get("kind") or "pre_alert")
                    if not isinstance(ts, (int, float)) or not isinstance(sym, str):
                        continue
                    if since_ts is not None and int(ts) < int(since_ts):
                        continue
                    out.add((int(ts), sym, str(kind)))
        except Exception:
            out = set()
    if out:
        return out
    # Fallback to registry keys (only populated when only-new/cooldown are enabled)
    reg_fp = artifacts_root / "alerts" / "telegram_sent.json"
    try:
        if reg_fp.exists():
            obj = json.loads(reg_fp.read_text(encoding="utf-8") or "{}")
            for k in list(obj.keys()):
                if k == "_meta":
                    continue
                try:
                    sym, kind, ts_s = k.split(":", 2)
                    ts = int(ts_s)
                except Exception:
                    continue
                if since_ts is not None and ts < int(since_ts):
                    continue
                out.add((ts, sym, kind))
    except Exception:
        pass
    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="CryptoStorm alerts utilities")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_merge = sub.add_parser("merge", help="Merge all per-symbol alerts into alerts/all_alerts.csv")
    p_merge.add_argument("config", type=str)
    p_merge.add_argument("--artifacts", type=str, help="Artifacts root override; defaults to run.artifacts_root/run_id")

    p_cmp = sub.add_parser("compare", help="Compare generated alerts vs Telegram sent log")
    p_cmp.add_argument("config", type=str)
    p_cmp.add_argument("--artifacts", type=str, help="Artifacts root override; defaults to run.artifacts_root/run_id")
    p_cmp.add_argument("--since-ts", type=int, default=None, help="Only consider alerts with ts >= since (ms)")
    p_cmp.add_argument("--limit", type=int, default=20, help="Max differences to print")
    p_cmp.add_argument("--json", action="store_true", help="Output JSON summary instead of text")

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
    if args.cmd == "compare":
        cfg = load_config(args.config)
        eff, warns, errs = validate_config(cfg, require_env=False)
        if errs:
            for e in errs:
                print(f"error: {e}")
            return 2
        run_id = (cfg.get("run") or {}).get("run_id") or eff.run_id
        artifacts_root = Path(args.artifacts) if args.artifacts else Path((cfg.get("run") or {}).get("artifacts_root", "./artifacts")) / str(run_id)
        alerts_dir = artifacts_root / "alerts"
        gen = _parse_generated_alerts(alerts_dir, eff.symbols, since_ts=(args.since_ts if args.since_ts is not None else None))
        sent = _parse_sent_alerts(artifacts_root, since_ts=(args.since_ts if args.since_ts is not None else None))
        missing = sorted(list(gen - sent))
        extra = sorted(list(sent - gen))
        matched = len(gen & sent)
        summary = {
            "run_id": str(run_id),
            "artifacts": str(artifacts_root),
            "since_ts": (int(args.since_ts) if args.since_ts is not None else None),
            "generated_count": len(gen),
            "sent_count": len(sent),
            "matched_count": matched,
            "missing_count": len(missing),
            "extra_count": len(extra),
            "ok": (len(missing) == 0 and len(extra) == 0),
        }
        if args.json:
            detail = {
                **summary,
                "missing": missing[: max(0, int(args.limit))],
                "extra": extra[: max(0, int(args.limit))],
            }
            print(json.dumps(detail, indent=2))
            return 0 if detail["ok"] else 1
        # Text output
        print(f"compare: run_id={summary['run_id']} artifacts={summary['artifacts']} since_ts={summary['since_ts'] or '-'}")
        print(f"  generated={summary['generated_count']} sent={summary['sent_count']} matched={summary['matched_count']}")
        if summary["ok"]:
            print("OK: all generated alerts were sent and no extras were found.")
            return 0
        lim = max(0, int(args.limit))
        if missing:
            print(f"MISSING (generated but not sent): {len(missing)} (showing {min(len(missing), lim)})")
            for ts, sym, kind in missing[:lim]:
                try:
                    import datetime as _dt
                    iso = _dt.datetime.utcfromtimestamp(int(ts)/1000).strftime("%Y-%m-%d %H:%M:%SZ")
                except Exception:
                    iso = str(ts)
                print(f"  - {sym} {kind} ts={ts} ({iso})")
        if extra:
            print(f"EXTRA (sent but not present in CSVs): {len(extra)} (showing {min(len(extra), lim)})")
            for ts, sym, kind in extra[:lim]:
                try:
                    import datetime as _dt
                    iso = _dt.datetime.utcfromtimestamp(int(ts)/1000).strftime("%Y-%m-%d %H:%M:%SZ")
                except Exception:
                    iso = str(ts)
                print(f"  - {sym} {kind} ts={ts} ({iso})")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
