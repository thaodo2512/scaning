from __future__ import annotations

import argparse
import csv
import json
import math
import time
from dataclasses import dataclass
import logging
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


def _ms_to_iso(ms: int) -> str:
    try:
        import datetime as _dt

        return _dt.datetime.utcfromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M:%SZ")
    except Exception:
        return str(ms)


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


def _series_stats(name: str, series_keys: Iterable[int]) -> str:
    xs = list(series_keys)
    if not xs:
        return f"{name}: count=0"
    mn, mx = min(xs), max(xs)
    return f"{name}: count={len(xs)} range=[{_ms_to_iso(mn)} .. {_ms_to_iso(mx)}]"


def _alignment_stats(name: str, series_keys: Iterable[int], step_ms: int = 5 * 60 * 1000) -> str:
    try:
        from collections import Counter

        xs = list(series_keys)
        if not xs:
            return f"{name}: aligned=0% (no data)"
        rema = [int(k) % int(step_ms) for k in xs]
        c = Counter(rema)
        most_common_rema, freq = c.most_common(1)[0]
        pct = 100.0 * freq / len(xs)
        return f"{name}: most_remainder={most_common_rema}ms aligned~{pct:.1f}%"
    except Exception:
        return f"{name}: alignment=n/a"


def build_features(
    eff: EffectiveConfig,
    *,
    data_root: Path,
    out_root: Path,
    now_ms: Optional[int] = None,
) -> None:
    LOG = logging.getLogger("cryptostorm.feature")
    now = now_ms if isinstance(now_ms, int) else _utc_now_ms()
    grid = _make_grid(now, eff.days)
    LOG.info("grid: %d bars from %s to %s", len(grid), _ms_to_iso(grid[0] if grid else now), _ms_to_iso(grid[-1] if grid else now))
    for sym in eff.symbols:
        sym_dir = data_root / sym
        # load inputs
        fnd = _funding_series(sym_dir)
        ohlcv = _ohlcv_series(sym_dir)
        oi = _oi_series(sym_dir)
        LOG.info("%s inputs: %s | %s | %s", sym, _series_stats("ohlcv", ohlcv.keys()), _series_stats("funding_8h", fnd.keys()), _series_stats("oi", oi.keys()))
        LOG.debug(
            "%s input alignment: %s | %s | %s",
            sym,
            _alignment_stats("ohlcv", ohlcv.keys()),
            _alignment_stats("funding_8h", fnd.keys(), 8 * 60 * 60 * 1000),
            _alignment_stats("oi", oi.keys()),
        )

        rows: List[Dict[str, Any]] = []
        last_funding: Optional[float] = None
        missing_ohlcv = 0
        missing_oi = 0
        ob_stale = 0
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
                missing_ohlcv += 1

            # funding ffill only
            if ts in fnd:
                last_funding = fnd[ts]
            if last_funding is not None:
                row["funding_now"] = last_funding

            # oi as snapshot (no ffill)
            if ts in oi:
                row["oi_now"] = oi[ts]
            else:
                missing_oi += 1

            # order book spread; NaN if stale
            max_age = eff.max_ob_age_s or 60
            spread = _orderbook_spread_bps(sym_dir, ts, max_age)
            if math.isnan(spread):
                row["data_ok"] = False
                ob_stale += 1
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

        # Coverage summary
        def cov(key: str) -> float:
            import math as _math

            vals = [r.get(key) for r in rows]
            ok = sum(1 for v in vals if isinstance(v, (int, float)) and not _math.isnan(v))
            return ok / len(rows) if rows else 0.0

        LOG.info(
            "%s coverage: ohlcv=%.3f funding=%.3f oi=%.3f ob_spread=%.3f data_ok=%.3f out=%s",
            sym,
            cov("close"),
            cov("funding_now"),
            cov("oi_now"),
            cov("spread_bps"),
            sum(1 for r in rows if r.get("data_ok") is True) / len(rows) if rows else 0.0,
            out_fp,
        )
        LOG.info(
            "%s missing bars: ohlcv=%d oi=%d ob_stale=%d of %d",
            sym,
            missing_ohlcv,
            missing_oi,
            ob_stale,
            len(rows),
        )
        # If debugging, print a few example rows
        if logging.getLogger("cryptostorm.feature").isEnabledFor(logging.DEBUG):
            for label, idx in (("first", 0), ("second", 1), ("last", len(rows) - 1)):
                if 0 <= idx < len(rows):
                    r = rows[idx]
                    LOG.debug(
                        "%s row %s ts=%s close=%s funding=%s oi=%s spread_bps=%s data_ok=%s",
                        sym,
                        label,
                        _ms_to_iso(int(r["ts"])),
                        r.get("close"),
                        r.get("funding_now"),
                        r.get("oi_now"),
                        r.get("spread_bps"),
                        r.get("data_ok"),
                    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="CryptoStorm feature engineering")
    parser.add_argument("config", type=str)
    parser.add_argument("--data", type=str, default="data", help="Input data root")
    parser.add_argument("--out", type=str, default="features", help="Output features root")
    parser.add_argument("--log-level", type=str, default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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
