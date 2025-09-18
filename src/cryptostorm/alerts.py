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


def _read_send_log_list(artifacts_root: Path) -> list[dict]:
    arr: list[dict] = []
    fp = artifacts_root / "alerts" / "telegram_send.jsonl"
    if not fp.exists():
        return arr
    try:
        with fp.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                arr.append(obj)
    except Exception:
        return arr
    return arr


def _infer_since_ts_ms(cfg: Mapping[str, Any], artifacts_root: Path) -> int | None:
    try:
        mfp = artifacts_root / "metrics" / "realtime.jsonl"
        if not mfp.exists():
            return None
        lines = mfp.read_text(encoding="utf-8").splitlines()
        if not lines:
            return None
        last = json.loads(lines[-1])
        bar_s = float(last.get("bar_ts") or 0.0)
        if bar_s <= 0:
            return None
        # Infer step from config futures_ohlcv
        step_ms = 15 * 60 * 1000
        try:
            acq = cfg.get("acquisition") or {}
            cg = (acq or {}).get("coinglass") or {}
            iv = str((cg.get("intervals") or {}).get("futures_ohlcv") or "15m").lower()
            if iv.endswith("m") and iv[:-1].isdigit():
                step_ms = int(iv[:-1]) * 60 * 1000
        except Exception:
            pass
        return int(bar_s * 1000) - step_ms
    except Exception:
        return None


def _simulate_selection(
    cfg: Mapping[str, Any],
    eff: EffectiveConfig,
    *,
    artifacts_root: Path,
    since_ts: int | None,
    kinds: list[str] | None,
    only_new: bool,
    cooldown_min: int | None,
    limit: int | None,
    no_filters: bool,
) -> dict:
    alerts_dir = artifacts_root / "alerts"
    # Defaults from config.telegram if not provided
    tg_cfg = ((cfg.get("notifications") or {}).get("telegram") or {}) if isinstance(cfg.get("notifications"), dict) else {}
    kinds_eff = kinds[:] if kinds else []
    if not kinds_eff:
        s = str(tg_cfg.get("kinds") or "storm").strip()
        kinds_eff = [x.strip() for x in s.split(",") if x.strip()]
    if not kinds_eff:
        kinds_eff = ["storm"]
    only_new_eff = False if no_filters else bool(tg_cfg.get("only_new", False) or only_new)
    cd_min_eff = None if no_filters else (int(cooldown_min) if cooldown_min is not None else (int(tg_cfg.get("cooldown_min")) if isinstance(tg_cfg.get("cooldown_min"), (int, float)) else None))
    limit_eff = None if no_filters else (int(limit) if (limit is not None and int(limit) > 0) else (int(tg_cfg.get("limit")) if isinstance(tg_cfg.get("limit"), (int, float)) and int(tg_cfg.get("limit")) > 0 else None))

    # Load generated
    scanned = 0
    in_range = 0
    in_kind = 0
    earliest_all: int | None = None
    latest_all: int | None = None
    earliest_in_range: int | None = None
    latest_in_range: int | None = None
    latest_before_since: int | None = None
    counts_in_range_by_kind: dict[str, int] = {}
    removed_kind = 0
    removed_kind_samples: list[dict] = []
    candidates: list[dict] = []
    for sym in eff.symbols:
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
                    scanned += 1
                    # Track global bounds
                    if earliest_all is None or ts < earliest_all:
                        earliest_all = ts
                    if latest_all is None or ts > latest_all:
                        latest_all = ts
                    if since_ts is not None and ts < int(since_ts):
                        if latest_before_since is None or ts > latest_before_since:
                            latest_before_since = ts
                    
                    if since_ts is not None and ts < int(since_ts):
                        continue
                    in_range += 1
                    if earliest_in_range is None or ts < earliest_in_range:
                        earliest_in_range = ts
                    if latest_in_range is None or ts > latest_in_range:
                        latest_in_range = ts
                    kind = str((r.get("kind") or "pre_alert")).strip() or "pre_alert"
                    counts_in_range_by_kind[kind] = counts_in_range_by_kind.get(kind, 0) + 1
                    if kinds_eff and kind not in kinds_eff:
                        removed_kind += 1
                        if len(removed_kind_samples) < 5:
                            removed_kind_samples.append({"sym": sym, "kind": kind, "ts": ts})
                        continue
                    in_kind += 1
                    # prepared item
                    it = {
                        "sym": sym,
                        "ts": ts,
                        "kind": kind,
                        "score": (r.get("score") if r.get("score") not in (None, "") else None),
                        "thr": (r.get("threshold") if r.get("threshold") not in (None, "") else None),
                        "key": f"{sym}:{kind}:{ts}",
                    }
                    candidates.append(it)
        except Exception:
            continue

    removed_only_new = 0
    removed_cooldown = 0
    removed_only_new_samples: list[dict] = []
    removed_cooldown_samples: list[dict] = []
    planned: list[dict] = []

    # Only-new and cooldown from send logs/registry
    sent_keys = set()
    last_by_symbol: dict[str, int] = {}
    for obj in _read_send_log_list(artifacts_root):
        try:
            ts = int(obj.get("bar_ts") or 0)
            sym = str(obj.get("symbol") or "")
            kind = str((obj.get("kind") or "pre_alert"))
            if since_ts is not None and ts < int(since_ts):
                continue
            if sym:
                k = f"{sym}:{kind}:{ts}"
                sent_keys.add(k)
                last_by_symbol[sym] = max(last_by_symbol.get(sym, 0), ts)
        except Exception:
            continue
    # Fallback registry
    if not sent_keys:
        try:
            reg_fp = artifacts_root / "alerts" / "telegram_sent.json"
            if reg_fp.exists():
                obj = json.loads(reg_fp.read_text(encoding="utf-8") or "{}")
                for k, v in obj.items():
                    if k == "_meta":
                        continue
                    sent_keys.add(k)
                meta = obj.get("_meta", {}) if isinstance(obj, dict) else {}
                lbt = meta.get("last_symbol_ts", {}) if isinstance(meta, dict) else {}
                for s, t in lbt.items():
                    try:
                        last_by_symbol[str(s)] = int(t)
                    except Exception:
                        continue
        except Exception:
            pass

    # Sort by priority (storm first), newest first
    def _prio(k: str) -> int:
        return 0 if k == "storm" else 1 if k == "pre_alert" else 2

    candidates.sort(key=lambda x: (_prio(x["kind"]), -int(x["ts"])) )

    cooldown_ms = int(cd_min_eff) * 60 * 1000 if isinstance(cd_min_eff, int) and cd_min_eff > 0 else None
    for it in candidates:
        if only_new_eff and it["key"] in sent_keys:
            removed_only_new += 1
            if len(removed_only_new_samples) < 5:
                removed_only_new_samples.append({k: it[k] for k in ("sym","kind","ts")})
            continue
        if cooldown_ms is not None:
            last_ts = last_by_symbol.get(it["sym"]) if isinstance(last_by_symbol, dict) else None
            if isinstance(last_ts, int) and (int(it["ts"]) - last_ts) < cooldown_ms:
                removed_cooldown += 1
                if len(removed_cooldown_samples) < 5:
                    removed_cooldown_samples.append({**{k: it[k] for k in ("sym","kind","ts")}, "last_ts": last_ts})
                continue
        planned.append(it)
        if isinstance(limit_eff, int) and limit_eff > 0 and len(planned) >= limit_eff:
            break

    return {
        "scanned": scanned,
        "in_range": in_range,
        "in_kind": in_kind,
        "earliest_all": earliest_all,
        "latest_all": latest_all,
        "latest_before_since": latest_before_since,
        "earliest_in_range": earliest_in_range,
        "latest_in_range": latest_in_range,
        "counts_in_range_by_kind": counts_in_range_by_kind,
        "planned": planned,
        "removed_only_new": removed_only_new,
        "removed_only_new_samples": removed_only_new_samples,
        "removed_cooldown": removed_cooldown,
        "removed_cooldown_samples": removed_cooldown_samples,
        "removed_kind": removed_kind,
        "removed_kind_samples": removed_kind_samples,
        "kinds": kinds_eff,
        "since_ts": since_ts,
        "only_new": only_new_eff,
        "cooldown_min": cd_min_eff or 0,
        "limit": (limit_eff if limit_eff is not None else "none"),
    }


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

    p_sim = sub.add_parser("simulate", help="Simulate realtime alert sending (no send); uses config telegram params")
    p_sim.add_argument("config", type=str)
    p_sim.add_argument("--artifacts", type=str)
    p_sim.add_argument("--since-ts", type=int, default=None)
    p_sim.add_argument("--kinds", type=str, default=None)
    p_sim.add_argument("--no-filters", action="store_true")
    p_sim.add_argument("--only-new", action="store_true", help="Override: simulate only-new even if config disables it")
    p_sim.add_argument("--cooldown-min", type=int, default=None)
    p_sim.add_argument("--limit", type=int, default=None)
    p_sim.add_argument("--json", action="store_true")

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
    if args.cmd == "simulate":
        cfg = load_config(args.config)
        eff, warns, errs = validate_config(cfg, require_env=False)
        if errs:
            for e in errs:
                print(f"error: {e}")
            return 2
        run_id = (cfg.get("run") or {}).get("run_id") or eff.run_id
        artifacts_root = Path(args.artifacts) if args.artifacts else Path((cfg.get("run") or {}).get("artifacts_root", "./artifacts")) / str(run_id)
        since_ts = int(args.since_ts) if args.since_ts is not None else _infer_since_ts_ms(cfg, artifacts_root)
        kinds = [s.strip() for s in str(args.kinds).split(",")] if args.kinds else None
        res = _simulate_selection(
            cfg,
            eff,
            artifacts_root=artifacts_root,
            since_ts=since_ts,
            kinds=kinds,
            only_new=bool(args.only_new),
            cooldown_min=(int(args.cooldown_min) if args.cooldown_min is not None else None),
            limit=(int(args.limit) if args.limit is not None else None),
            no_filters=bool(args.no_filters),
        )
        if args.json:
            import datetime as _dt
            out = dict(res)
            out["planned"] = [
                {
                    **it,
                    "ts_iso": _dt.datetime.utcfromtimestamp(int(it["ts"]) / 1000).strftime("%Y-%m-%d %H:%M:%SZ"),
                }
                for it in res["planned"]
            ]
            # Add ISO hints
            for k in ("earliest_all", "latest_all", "latest_before_since", "earliest_in_range", "latest_in_range"):
                v = res.get(k)
                if isinstance(v, int) and v > 0:
                    out[k + "_iso"] = _dt.datetime.utcfromtimestamp(int(v) / 1000).strftime("%Y-%m-%d %H:%M:%SZ")
            out["counts_in_range_by_kind"] = res.get("counts_in_range_by_kind", {})
            out["removed_kind"] = res.get("removed_kind", 0)
            out["removed_kind_samples"] = res.get("removed_kind_samples", [])
            out["removed_only_new_samples"] = res.get("removed_only_new_samples", [])
            out["removed_cooldown_samples"] = res.get("removed_cooldown_samples", [])
            print(json.dumps(out, indent=2))
            return 0
        # Text
        kinds_str = ",".join(res["kinds"]) if res.get("kinds") else "-"
        print(
            f"alerts-sim: scanned={res['scanned']} in_range={res['in_range']} in_kind={res['in_kind']} kinds={kinds_str} since_ts={res['since_ts'] or '-'} only_new={'on' if res['only_new'] else 'off'} removed_only_new={res['removed_only_new']} cooldown_min={res['cooldown_min']} removed_cooldown={res['removed_cooldown']} limit={res['limit']} to_send={len(res['planned'])}"
        )
        if not res["planned"]:
            # Heuristics to explain an empty plan
            if int(res.get("in_range") or 0) == 0:
                print("reason: no alerts at or after since_ts for current symbols; try lowering --since-ts")
                # Show nearest hints
                try:
                    import datetime as _dt
                    lb = res.get("latest_before_since")
                    ea = res.get("earliest_all")
                    if isinstance(lb, int) and lb > 0:
                        print("  nearest_before:", _dt.datetime.utcfromtimestamp(lb/1000).strftime("%Y-%m-%d %H:%M:%SZ"))
                    if isinstance(ea, int) and ea > 0:
                        print("  earliest_all:", _dt.datetime.utcfromtimestamp(ea/1000).strftime("%Y-%m-%d %H:%M:%SZ"))
                except Exception:
                    pass
            elif int(res.get("in_kind") or 0) == 0:
                print("reason: no alerts of requested kinds in range; adjust --kinds or disable --kinds filter")
                # Show counts by kind present
                ck = res.get("counts_in_range_by_kind", {}) or {}
                if ck:
                    print("  kinds_in_range:", ", ".join([f"{k}={ck[k]}" for k in sorted(ck.keys())]))
                rk = res.get("removed_kind_samples", []) or []
                for it in rk:
                    try:
                        import datetime as _dt
                        iso = _dt.datetime.utcfromtimestamp(int(it.get('ts',0))/1000).strftime("%Y-%m-%d %H:%M:%SZ")
                    except Exception:
                        iso = str(it.get('ts'))
                    print(f"  sample_excluded_by_kind: {it.get('sym')} {it.get('kind')} ts={iso}")
            elif res.get("only_new") and int(res.get("removed_only_new") or 0) > 0:
                print("reason: all candidates already sent (only-new active); drop --only-new or clear telegram_sent.json")
                for it in res.get("removed_only_new_samples", [])[:3]:
                    try:
                        import datetime as _dt
                        iso = _dt.datetime.utcfromtimestamp(int(it.get('ts',0))/1000).strftime("%Y-%m-%d %H:%M:%SZ")
                    except Exception:
                        iso = str(it.get('ts'))
                    print(f"  sample_only_new: {it.get('sym')} {it.get('kind')} ts={iso}")
            elif int(res.get("removed_cooldown") or 0) > 0:
                print("reason: suppressed by per-symbol cooldown; decrease --cooldown-min or widen window")
                for it in res.get("removed_cooldown_samples", [])[:3]:
                    try:
                        import datetime as _dt
                        iso = _dt.datetime.utcfromtimestamp(int(it.get('ts',0))/1000).strftime("%Y-%m-%d %H:%M:%SZ")
                        liso = _dt.datetime.utcfromtimestamp(int(it.get('last_ts',0))/1000).strftime("%Y-%m-%d %H:%M:%SZ")
                    except Exception:
                        iso = str(it.get('ts'))
                        liso = str(it.get('last_ts'))
                    print(f"  sample_cooldown: {it.get('sym')} {it.get('kind')} ts={iso} last_sent={liso}")
            else:
                print("reason: no eligible candidates after filters; consider --no-filters --limit 5 to inspect newest")
        for it in res["planned"][:20]:
            try:
                import datetime as _dt
                iso = _dt.datetime.utcfromtimestamp(int(it["ts"]) / 1000).strftime("%Y-%m-%d %H:%M:%SZ")
            except Exception:
                iso = str(it["ts"])
            print(f"  - {it['sym']} {it['kind']} ts={iso}")
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
