from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Tuple
from collections import Counter
import os

from .config import EffectiveConfig, load_config, validate_config
from .retrieve.coinglass import _output_filename


def _utc_now_ms() -> int:
    return int(time.time() * 1000)


def _interval_ms(name: str, interval_value: Optional[str]) -> Optional[int]:
    # Prefer explicit mapping by dataset name; fall back to parsing interval strings
    five_min_ds = {
        "futures_ohlcv_5m",
        "spot_ohlcv_5m",
        "funding_pred_5m",
        "oi_5m_ohlc",
        "taker_futures_5m",
        "taker_spot_5m",
        "liquidation_5m",
        "orderbook_futures_5m",
        "orderbook_spot_5m",
    }
    if name in five_min_ds:
        return 5 * 60 * 1000
    if name == "funding_8h":
        return 8 * 60 * 60 * 1000
    # Parse common strings like '5m', '8h'
    if isinstance(interval_value, str):
        s = interval_value.strip().lower()
        if s == "last_of_5m":
            return 5 * 60 * 1000
        if s.endswith("m") and s[:-1].isdigit():
            return int(s[:-1]) * 60 * 1000
        if s.endswith("h") and s[:-1].isdigit():
            return int(s[:-1]) * 60 * 60 * 1000
    return None


def _ms_to_iso(ms: Optional[int]) -> str:
    if ms is None:
        return "-"
    try:
        import datetime as _dt

        return _dt.datetime.utcfromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M:%SZ")
    except Exception:
        return str(ms)


def _read_ts_in_window(fp: Path, start_ms: int, end_ms: int) -> List[int]:
    if not fp.exists():
        return []
    out: List[int] = []
    try:
        for line in fp.read_text(encoding="utf-8").splitlines():
            try:
                obj = json.loads(line)
            except Exception:
                continue
            ts = obj.get("ts")
            if isinstance(ts, (int, float)):
                v = int(ts)
                if start_ms <= v <= end_ms:
                    out.append(v)
    except Exception:
        return out
    return out


@dataclass
class Coverage:
    dataset: str
    symbol: str
    expected: int
    observed: int
    ratio: float
    first_ts: Optional[int]
    last_ts: Optional[int]
    file: Path


def _rema_stats(ts_list: List[int], step_ms: int) -> Tuple[float, int, List[Tuple[int, int]]]:
    if step_ms <= 0 or not ts_list:
        return 0.0, 0, []
    rema = [int(t) % int(step_ms) for t in ts_list]
    c = Counter(rema)
    most_rema, freq = c.most_common(1)[0]
    pct = 100.0 * freq / len(ts_list)
    top3 = c.most_common(3)
    return pct, most_rema, top3


def _file_stat_str(fp: Path) -> str:
    try:
        if not fp.exists():
            return "exists=False"
        st = fp.stat()
        mtime = _ms_to_iso(int(st.st_mtime * 1000))
        return f"exists=True size={st.st_size}B mtime={mtime}"
    except Exception:
        return "exists=?"


def _missing_grid_samples(start_ms: int, end_ms: int, step_ms: Optional[int], observed: List[int], limit: int) -> List[str]:
    if not step_ms or step_ms <= 0 or limit <= 0:
        return []
    s = (start_ms // step_ms) * step_ms
    if s < start_ms:
        s += step_ms
    e = (end_ms // step_ms) * step_ms
    obs = set(int(t) for t in observed)
    missing: List[str] = []
    t = s
    while t <= e and len(missing) < limit:
        if t not in obs:
            missing.append(_ms_to_iso(t))
        t += step_ms
    return missing


def audit_coverage(eff: EffectiveConfig, data_root: Path, min_ratio: float, *, debug: bool = False, show_missing: int = 0) -> Tuple[List[Coverage], List[Coverage]]:
    end_ms = _utc_now_ms()
    start_ms = end_ms - eff.days * 24 * 60 * 60 * 1000
    all_rows: List[Coverage] = []
    failures: List[Coverage] = []

    for ds_key, ds_cfg in eff.datasets.items():
        if not ds_cfg.enabled:
            continue
        int_ms = _interval_ms(ds_key, ds_cfg.interval)
        if not int_ms:
            # Unknown cadence; skip from coverage
            continue
        expected = max(1, math.floor((end_ms - start_ms) / int_ms))
        for sym in eff.symbols:
            fp = data_root / sym / _output_filename(ds_key)
            ts_list = sorted(set(_read_ts_in_window(fp, start_ms, end_ms)))
            observed = len(ts_list)
            ratio = (observed / expected) if expected else 0.0
            row = Coverage(
                dataset=ds_key,
                symbol=sym,
                expected=expected,
                observed=observed,
                ratio=ratio,
                first_ts=ts_list[0] if ts_list else None,
                last_ts=ts_list[-1] if ts_list else None,
                file=fp,
            )
            all_rows.append(row)
            if ratio < min_ratio:
                failures.append(row)
                if debug:
                    step_ms = int_ms or 0
                    pct_aligned, most_rema, top3 = _rema_stats(ts_list, step_ms)
                    total_file = 0
                    outside_before = 0
                    outside_after = 0
                    try:
                        if fp.exists():
                            for line in fp.read_text(encoding="utf-8").splitlines():
                                try:
                                    obj = json.loads(line)
                                except Exception:
                                    continue
                                tsv = obj.get("ts")
                                if isinstance(tsv, (int, float)):
                                    total_file += 1
                                    v = int(tsv)
                                    if v < start_ms:
                                        outside_before += 1
                                    elif v > end_ms:
                                        outside_after += 1
                    except Exception:
                        pass
                    missing_samples = _missing_grid_samples(start_ms, end_ms, int_ms, ts_list, int(show_missing))
                    print(
                        f"  debug: file=({_file_stat_str(fp)}) interval_ms={int_ms} expected={expected} total_in_file={total_file} in_window={observed} outside_before={outside_before} outside_after={outside_after}"
                    )
                    if int_ms:
                        top3_str = ", ".join([f"{r}:{c}" for r, c in top3])
                        print(
                            f"  debug: alignment most_remainder={most_rema}ms aligned~{pct_aligned:.1f}% top_rema=[{top3_str}]"
                        )
                    if missing_samples:
                        head = ", ".join(missing_samples[: min(5, len(missing_samples))])
                        print(f"  debug: first_missing_bars={head} (+{max(0, len(missing_samples)-5)} more)")
    return all_rows, failures


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="CryptoStorm data coverage audit")
    parser.add_argument("config", type=str, help="Path to YAML/JSON config")
    parser.add_argument("--data", type=str, default="data", help="Data root (data/<SYM>/...)")
    parser.add_argument("--min-ratio", type=float, default=0.95, help="Min coverage ratio to pass")
    parser.add_argument("--debug", action="store_true", help="Print extra diagnostics for failing rows")
    parser.add_argument("--show-missing", type=int, default=5, help="Show first N missing 5m bars for failing rows (0=off)")
    parser.add_argument("--soft-fail", action="store_true", help="Do not exit non-zero on coverage failures; print results and exit 0")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    eff, warns, errs = validate_config(cfg, require_env=False)
    if errs:
        for e in errs:
            print(f"error: {e}")
        return 2

    data_root = Path(args.data)
    rows, failures = audit_coverage(eff, data_root, args.min_ratio, debug=bool(args.debug), show_missing=int(args.show_missing))
    # Print window header
    end_ms = _utc_now_ms()
    start_ms = end_ms - eff.days * 24 * 60 * 60 * 1000
    print(
        f"Audit window: start={_ms_to_iso(start_ms)} end={_ms_to_iso(end_ms)} days={eff.days} min_ratio={args.min_ratio}"
    )
    # Detailed per-dataset summary
    for r in rows:
        missing = max(0, r.expected - r.observed)
        needed_for_pass = max(0, int((args.min_ratio * r.expected) + 0.5) - r.observed)
        span = (
            f"{_ms_to_iso(r.first_ts)} .. {_ms_to_iso(r.last_ts)}"
            if (r.first_ts is not None and r.last_ts is not None)
            else "-"
        )
        print(
            f"{r.symbol} {r.dataset}: observed={r.observed} expected={r.expected} ratio={r.ratio:.3f} missing={missing} needed_for_pass={needed_for_pass} span={span} file={r.file}"
        )
    if failures:
        print(f"FAIL: {len(failures)} dataset(s) below ratio {args.min_ratio}")
        if not args.soft_fail:
            return 1
        # Soft-fail requested: exit 0 after reporting
        return 0
    print("OK: coverage meets threshold")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
