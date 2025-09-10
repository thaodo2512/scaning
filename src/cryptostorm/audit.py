from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

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


def audit_coverage(eff: EffectiveConfig, data_root: Path, min_ratio: float) -> Tuple[List[Coverage], List[Coverage]]:
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
    return all_rows, failures


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="CryptoStorm data coverage audit")
    parser.add_argument("config", type=str, help="Path to YAML/JSON config")
    parser.add_argument("--data", type=str, default="data", help="Data root (data/<SYM>/...)")
    parser.add_argument("--min-ratio", type=float, default=0.95, help="Min coverage ratio to pass")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    eff, warns, errs = validate_config(cfg, require_env=False)
    if errs:
        for e in errs:
            print(f"error: {e}")
        return 2

    rows, failures = audit_coverage(eff, Path(args.data), args.min_ratio)
    # Print concise summary
    for r in rows:
        print(
            f"{r.symbol} {r.dataset}: observed={r.observed} expected={r.expected} ratio={r.ratio:.3f} first={_ms_to_iso(r.first_ts)} last={_ms_to_iso(r.last_ts)} file={r.file}"
        )
    if failures:
        print(f"FAIL: {len(failures)} dataset(s) below ratio {args.min_ratio}")
        return 1
    print("OK: coverage meets threshold")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
