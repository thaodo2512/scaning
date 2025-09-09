from __future__ import annotations

import argparse
import csv
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from ..config import EffectiveConfig, load_config, validate_config
from ..retrieve.coinglass import _output_filename


def _utc_now_ms() -> int:
    return int(time.time() * 1000)


def _align_to_5m_close(ms: int) -> int:
    # Align to previous multiple of 5 minutes
    step = 5 * 60 * 1000
    return ms - (ms % step)


def _read_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:
                    continue
    except Exception:
        return []


def _make_grid(end_ms: int, days: int) -> List[int]:
    end_aligned = _align_to_5m_close(end_ms)
    start_ms = end_aligned - days * 24 * 60 * 60 * 1000
    start_aligned = _align_to_5m_close(start_ms)
    step = 5 * 60 * 1000
    grid = list(range(start_aligned, end_aligned + 1, step))
    return grid


def _funding_series(data_dir: Path) -> Dict[int, float]:
    fp = data_dir / _output_filename("funding_8h")
    out: Dict[int, float] = {}
    for rec in _read_jsonl(fp):
        ts = rec.get("ts")
        pl = rec.get("payload", {})
        val = None
        for k in ("value", "fundingRate", "close", "v"):
            if isinstance(pl, Mapping) and k in pl:
                val = pl[k]
                break
        if isinstance(ts, (int, float)) and isinstance(val, (int, float)):
            out[int(ts)] = float(val)
    return out


def _ohlcv_series(data_dir: Path) -> Dict[int, Dict[str, float]]:
    fp = data_dir / _output_filename("futures_ohlcv_5m")
    out: Dict[int, Dict[str, float]] = {}
    for rec in _read_jsonl(fp):
        ts = rec.get("ts")
        pl = rec.get("payload", {})
        if isinstance(ts, (int, float)) and isinstance(pl, Mapping):
            row: Dict[str, float] = {}
            for k in ("open", "high", "low", "close", "volume"):
                v = pl.get(k)
                if isinstance(v, (int, float)):
                    row[k] = float(v)
            if row:
                out[int(ts)] = row
    return out


def _oi_series(data_dir: Path) -> Dict[int, float]:
    fp = data_dir / _output_filename("oi_5m_ohlc")
    out: Dict[int, float] = {}
    for rec in _read_jsonl(fp):
        ts = rec.get("ts")
        pl = rec.get("payload", {})
        val = None
        if isinstance(pl, Mapping):
            for k in ("value", "close", "oi"):
                if k in pl:
                    val = pl[k]
                    break
        if isinstance(ts, (int, float)) and isinstance(val, (int, float)):
            out[int(ts)] = float(val)
    return out


def _orderbook_spread_bps(data_dir: Path, bar_ts: int, max_age_s: int) -> float:
    fp = data_dir / _output_filename("orderbook_futures_5m")
    best_spread_bps = math.nan
    # Find the latest snapshot at or before bar_ts within age
    latest: Optional[Tuple[int, Mapping[str, Any]]] = None
    for rec in _read_jsonl(fp):
        ts = rec.get("ts")
        if not isinstance(ts, (int, float)):
            continue
        t = int(ts)
        if t <= bar_ts and (latest is None or t > latest[0]):
            latest = (t, rec.get("payload", {}))
    if latest is None:
        return best_spread_bps
    snap_ts, payload = latest
    if bar_ts - snap_ts > max_age_s * 1000:
        return best_spread_bps
    bids = payload.get("bids") if isinstance(payload, Mapping) else None
    asks = payload.get("asks") if isinstance(payload, Mapping) else None
    def _first_price(side):
        if isinstance(side, list) and side:
            top = side[0]
            if isinstance(top, (list, tuple)) and len(top) >= 1 and isinstance(top[0], (int, float)):
                return float(top[0])
            if isinstance(top, Mapping):
                # support {price:..., size:...}
                p = top.get("price")
                if isinstance(p, (int, float)):
                    return float(p)
        return math.nan
    best_bid = _first_price(bids)
    best_ask = _first_price(asks)
    if math.isnan(best_bid) or math.isnan(best_ask) or best_bid <= 0 or best_ask <= 0:
        return math.nan
    mid = 0.5 * (best_bid + best_ask)
    spread_bps = (best_ask - best_bid) / mid * 1e4
    return spread_bps


def build_features(
    eff: EffectiveConfig,
    *,
    data_root: Path,
    out_root: Path,
    now_ms: Optional[int] = None,
) -> None:
    now = now_ms if isinstance(now_ms, int) else _utc_now_ms()
    grid = _make_grid(now, eff.days)
    for sym in eff.symbols:
        sym_dir = data_root / sym
        # load inputs
        fnd = _funding_series(sym_dir)
        ohlcv = _ohlcv_series(sym_dir)
        oi = _oi_series(sym_dir)

        rows: List[Dict[str, Any]] = []
        last_funding: Optional[float] = None
        for ts in grid:
            row: Dict[str, Any] = {
                "ts": ts,
                "symbol": sym,
                "open": math.nan,
                "high": math.nan,
                "low": math.nan,
                "close": math.nan,
                "volume": math.nan,
                "funding_now": math.nan,
                "oi_now": math.nan,
                "spread_bps": math.nan,
                "data_ok": True,
            }
            if ts in ohlcv:
                for k, v in ohlcv[ts].items():
                    row[k] = v
            else:
                row["data_ok"] = False

            # funding ffill only
            if ts in fnd:
                last_funding = fnd[ts]
            if last_funding is not None:
                row["funding_now"] = last_funding

            # oi as snapshot (no ffill)
            if ts in oi:
                row["oi_now"] = oi[ts]

            # order book spread; NaN if stale
            max_age = eff.max_ob_age_s or 60
            spread = _orderbook_spread_bps(sym_dir, ts, max_age)
            if math.isnan(spread):
                row["data_ok"] = False
            row["spread_bps"] = spread

            rows.append(row)

        # Write CSV
        out_dir = out_root / sym
        out_dir.mkdir(parents=True, exist_ok=True)
        out_fp = out_dir / "features_5m.csv"
        cols = [
            "ts",
            "symbol",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "funding_now",
            "oi_now",
            "spread_bps",
            "data_ok",
        ]
        with out_fp.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow(r)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="CryptoStorm feature engineering")
    parser.add_argument("config", type=str)
    parser.add_argument("--data", type=str, default="data", help="Input data root")
    parser.add_argument("--out", type=str, default="features", help="Output features root")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    eff, warns, errs = validate_config(cfg, require_env=False)
    if errs:
        for e in errs:
            print(f"error: {e}")
        return 2
    build_features(eff, data_root=Path(args.data), out_root=Path(args.out))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

