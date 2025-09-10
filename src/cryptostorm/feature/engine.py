from __future__ import annotations

import argparse
import csv
import json
import math
import time
from dataclasses import dataclass
from collections import deque
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


def _as_float(v: Any) -> Optional[float]:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except Exception:
            return None
    return None


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
        if isinstance(pl, Mapping):
            for k in ("value", "fundingRate", "rate", "close", "v"):
                if k in pl:
                    val = _as_float(pl[k])
                    break
        elif isinstance(pl, list) and pl:
            # Heuristic: take second element if numeric
            cand = _as_float(pl[1] if len(pl) > 1 else pl[0])
            val = cand
        if isinstance(ts, (int, float)) and isinstance(val, (int, float)):
            out[int(ts)] = float(val)
    return out


def _ohlcv_series(data_dir: Path) -> Dict[int, Dict[str, float]]:
    fp = data_dir / _output_filename("futures_ohlcv_5m")
    out: Dict[int, Dict[str, float]] = {}
    for rec in _read_jsonl(fp):
        ts = rec.get("ts")
        pl = rec.get("payload", {})
        if not isinstance(ts, (int, float)):
            continue
        row: Dict[str, float] = {}
        if isinstance(pl, Mapping):
            # Try multiple key styles
            key_map = {
                "open": ("open", "o", "openPrice"),
                "high": ("high", "h", "highPrice"),
                "low": ("low", "l", "lowPrice"),
                "close": ("close", "c", "closePrice"),
                "volume": ("volume", "v", "baseVolume", "volume_usd"),
            }
            for outk, candidates in key_map.items():
                for ck in candidates:
                    v = _as_float(pl.get(ck))
                    if v is not None:
                        row[outk] = v
                        break
        elif isinstance(pl, list):
            # Heuristic: assume [t?, open, high, low, close, volume]
            vals = [x for x in pl]
            try:
                if len(vals) >= 5:
                    # detect if first is timestamp-like (large int); if yes, shift indices by 1
                    idx = 1 if isinstance(vals[0], (int, float)) and int(vals[0]) > 1_000_000_000_000 else 0
                    o = vals[idx + 0]
                    h = vals[idx + 1]
                    l = vals[idx + 2]
                    c = vals[idx + 3]
                    v = vals[idx + 4] if len(vals) > idx + 4 else None
                    for name, val in (("open", o), ("high", h), ("low", l), ("close", c)):
                        fv = _as_float(val)
                        if fv is not None:
                            row[name] = fv
                    fv = _as_float(v)
                    if fv is not None:
                        row["volume"] = fv
            except Exception:
                pass
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
            for k in ("value", "close", "oi", "openInterest"):
                if k in pl:
                    val = _as_float(pl[k])
                    break
        elif isinstance(pl, list) and pl:
            cand = _as_float(pl[1] if len(pl) > 1 else pl[0])
            val = cand
        if isinstance(ts, (int, float)) and isinstance(val, (int, float)):
            out[int(ts)] = float(val)
    return out


def _spot_close_series(data_dir: Path) -> Dict[int, float]:
    fp = data_dir / _output_filename("spot_ohlcv_5m")
    out: Dict[int, float] = {}
    for rec in _read_jsonl(fp):
        ts = rec.get("ts")
        pl = rec.get("payload", {})
        if isinstance(ts, (int, float)) and isinstance(pl, Mapping):
            v = _as_float(pl.get("close"))
            if isinstance(v, (int, float)):
                out[int(ts)] = float(v)
    return out


def _taker_series(data_dir: Path, dataset: str) -> Dict[int, Tuple[float, float]]:
    fp = data_dir / _output_filename(dataset)
    out: Dict[int, Tuple[float, float]] = {}
    for rec in _read_jsonl(fp):
        ts = rec.get("ts")
        pl = rec.get("payload", {})
        if not isinstance(ts, (int, float)) or not isinstance(pl, Mapping):
            continue
        # try common key variants
        buy = None
        sell = None
        for k in ("takerBuyVol", "buyVol", "buy", "takerBuyVolume"):
            vb = _as_float(pl.get(k))
            if vb is not None:
                buy = float(vb)
                break
        for k in ("takerSellVol", "sellVol", "sell", "takerSellVolume"):
            vs = _as_float(pl.get(k))
            if vs is not None:
                sell = float(vs)
                break
        if buy is not None and sell is not None:
            out[int(ts)] = (buy, sell)
    return out


def _funding_pred_series(data_dir: Path) -> Dict[int, float]:
    fp = data_dir / _output_filename("funding_pred_5m")
    out: Dict[int, float] = {}
    for rec in _read_jsonl(fp):
        ts = rec.get("ts")
        pl = rec.get("payload", {})
        if isinstance(ts, (int, float)) and isinstance(pl, Mapping):
            for k in ("value", "close", "rate"):
                v = _as_float(pl.get(k))
                if v is not None:
                    out[int(ts)] = float(v)
                    break
    return out


def _liq_series(data_dir: Path) -> Dict[int, Tuple[float, float]]:
    fp = data_dir / _output_filename("liquidation_5m")
    out: Dict[int, Tuple[float, float]] = {}
    for rec in _read_jsonl(fp):
        ts = rec.get("ts")
        pl = rec.get("payload", {})
        if not isinstance(ts, (int, float)) or not isinstance(pl, Mapping):
            continue
        notional = None
        for k in ("notional", "value", "close", "amount", "sumNotional"):
            vn = _as_float(pl.get(k))
            if vn is not None:
                notional = float(vn)
                break
        count = None
        for k in ("count", "liquidationCount", "sumCount"):
            vc = _as_float(pl.get(k))
            if vc is not None:
                count = float(vc)
                break
        if notional is not None:
            out[int(ts)] = (notional, float(count) if isinstance(count, (int, float)) else math.nan)
    return out


def _debug_payload_samples(sym_dir: Path) -> None:
    LOG = logging.getLogger("cryptostorm.feature")
    for fname in (
        "futures_ohlcv_5m.jsonl",
        "funding_8h_ohlc.jsonl",
        "oi_5m_ohlc.jsonl",
    ):
        fp = sym_dir / fname
        if not fp.exists():
            continue
        try:
            with fp.open("r", encoding="utf-8") as f:
                for i in range(3):
                    line = f.readline()
                    if not line:
                        break
                    obj = json.loads(line)
                    pl = obj.get("payload")
                    ptype = type(pl).__name__
                    keys = list(pl.keys())[:6] if isinstance(pl, dict) else (f"len={len(pl)}" if isinstance(pl, list) else "")
                    LOG.debug("sample %s: payload type=%s keys=%s", fname, ptype, keys)
        except Exception:
            pass


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


def _orderbook_depth_ratio(data_dir: Path, bar_ts: int, max_age_s: int, range_bp: Optional[int]) -> float:
    if not isinstance(range_bp, int) or range_bp <= 0:
        return math.nan
    fp = data_dir / _output_filename("orderbook_futures_5m")
    latest: Optional[Tuple[int, Mapping[str, Any]]] = None
    for rec in _read_jsonl(fp):
        ts = rec.get("ts")
        if not isinstance(ts, (int, float)):
            continue
        t = int(ts)
        if t <= bar_ts and (latest is None or t > latest[0]):
            latest = (t, rec.get("payload", {}))
    if latest is None:
        return math.nan
    snap_ts, payload = latest
    if bar_ts - snap_ts > max_age_s * 1000:
        return math.nan
    bids = payload.get("bids") if isinstance(payload, Mapping) else None
    asks = payload.get("asks") if isinstance(payload, Mapping) else None
    # Extract best prices
    def _first_price(side):
        if isinstance(side, list) and side:
            top = side[0]
            if isinstance(top, (list, tuple)) and len(top) >= 1 and isinstance(top[0], (int, float)):
                return float(top[0])
            if isinstance(top, Mapping):
                p = top.get("price")
                if isinstance(p, (int, float)):
                    return float(p)
        return math.nan
    best_bid = _first_price(bids)
    best_ask = _first_price(asks)
    if math.isnan(best_bid) or math.isnan(best_ask) or best_bid <= 0 or best_ask <= 0:
        return math.nan
    mid = 0.5 * (best_bid + best_ask)
    lo = mid * (1 - range_bp / 1e4)
    hi = mid * (1 + range_bp / 1e4)

    def _sum_size(side, is_bid: bool) -> float:
        total = 0.0
        if isinstance(side, list):
            for lvl in side:
                price = None
                size = None
                if isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
                    price, size = lvl[0], lvl[1]
                elif isinstance(lvl, Mapping):
                    price = lvl.get("price")
                    size = lvl.get("size") or lvl.get("qty") or lvl.get("volume")
                if isinstance(price, (int, float)) and isinstance(size, (int, float)):
                    p = float(price)
                    s = float(size)
                    if is_bid and p >= lo:
                        total += s
                    if not is_bid and p <= hi:
                        total += s
        return total

    bid_depth = _sum_size(bids, True)
    ask_depth = _sum_size(asks, False)
    denom = bid_depth + ask_depth
    if denom <= 0:
        return math.nan
    return (bid_depth - ask_depth) / denom


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
        spot_close = _spot_close_series(sym_dir)
        taker_perp = _taker_series(sym_dir, "taker_futures_5m")
        taker_spot = _taker_series(sym_dir, "taker_spot_5m")
        liq = _liq_series(sym_dir)
        fund_pred = _funding_pred_series(sym_dir)
        LOG.info("%s inputs: %s | %s | %s", sym, _series_stats("ohlcv", ohlcv.keys()), _series_stats("funding_8h", fnd.keys()), _series_stats("oi", oi.keys()))
        _debug_payload_samples(sym_dir)
        LOG.debug(
            "%s input alignment: %s | %s | %s",
            sym,
            _alignment_stats("ohlcv", ohlcv.keys()),
            _alignment_stats("funding_8h", fnd.keys(), 8 * 60 * 60 * 1000),
            _alignment_stats("oi", oi.keys()),
        )

        rows: List[Dict[str, Any]] = []
        # Precompute distributions for percentile features (30d window == current grid)
        funding_vals_sorted = sorted([v for v in fnd.values() if isinstance(v, (int, float))])
        oi_vals_sorted = sorted([v for v in oi.values() if isinstance(v, (int, float))])
        def _pctile_rank(sorted_vals: List[float], x: Optional[float]) -> float:
            if not sorted_vals or x is None or not isinstance(x, (int, float)):
                return math.nan
            # rank = fraction <= x
            import bisect
            i = bisect.bisect_right(sorted_vals, float(x))
            return i / len(sorted_vals)

        # Helpers for rolling windows
        step = 5 * 60 * 1000
        def _twap(ts: int, series: Mapping[int, float], bars: int) -> float:
            acc = 0.0
            n = 0
            t = ts - (bars - 1) * step
            while t <= ts:
                v = series.get(t)
                if isinstance(v, (int, float)) and not math.isnan(v):
                    acc += float(v)
                    n += 1
                t += step
            return (acc / n) if n > 0 else math.nan
        def _roll_sum(ts: int, series: Mapping[int, float], bars: int) -> float:
            acc = 0.0
            n = 0
            t = ts - (bars - 1) * step
            while t <= ts:
                v = series.get(t)
                if isinstance(v, (int, float)) and not math.isnan(v):
                    acc += float(v)
                    n += 1
                t += step
            return acc if n > 0 else math.nan

        # State for flows and returns
        cvd_cum = 0.0
        last_three_delta: deque = deque(maxlen=3)
        last_close: Optional[float] = None
        last_three_rets: deque = deque(maxlen=3)
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
                "basis_now": math.nan,
                "basis_TWAP_60m": math.nan,
                "basis_TWAP_120m": math.nan,
                "funding_pctile_30d": math.nan,
                "funding_pred_twap_60m": math.nan,
                "oi_pctile_30d": math.nan,
                "delta_taker_5m": math.nan,
                "cvd_perp_5m": math.nan,
                "cvd_perp_15m": math.nan,
                "perp_share_60m": math.nan,
                "depth_ratio": math.nan,
                "liq_notional_5m": math.nan,
                "liq_count_5m": math.nan,
                "liq_notional_60m": math.nan,
                "rv_15m": math.nan,
                "exch_reserve_flag": False,
                "etf_flow_flag": False,
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
            # funding percentile (use settlement distribution over window)
            row["funding_pctile_30d"] = _pctile_rank(funding_vals_sorted, last_funding)
            # predicted funding TWAP 60m (if available)
            row["funding_pred_twap_60m"] = _twap(ts, fund_pred, 12)

            # oi as snapshot (no ffill)
            if ts in oi:
                row["oi_now"] = oi[ts]
            else:
                missing_oi += 1
            row["oi_pctile_30d"] = _pctile_rank(oi_vals_sorted, oi.get(ts))

            # order book spread; NaN if stale
            max_age = eff.max_ob_age_s or 60
            spread = _orderbook_spread_bps(sym_dir, ts, max_age)
            if math.isnan(spread):
                row["data_ok"] = False
                ob_stale += 1
            row["spread_bps"] = spread
            # optional depth ratio within +/- orderbook_range_bp
            try:
                row["depth_ratio"] = _orderbook_depth_ratio(sym_dir, ts, max_age, eff.orderbook_range_bp)
            except Exception:
                pass

            # basis proxy if spot close present
            sc = spot_close.get(ts)
            fc = ohlcv.get(ts, {}).get("close")
            if isinstance(sc, (int, float)) and isinstance(fc, (int, float)) and sc != 0:
                basis_now = (fc - sc) / sc
                row["basis_now"] = basis_now
                row["basis_TWAP_60m"] = _twap(ts, {t: (ohlcv.get(t, {}).get("close") - spot_close.get(t)) / spot_close.get(t) for t in grid if isinstance(spot_close.get(t), (int, float)) and spot_close.get(t) != 0 and isinstance(ohlcv.get(t, {}).get("close"), (int, float))}, 12)
                row["basis_TWAP_120m"] = _twap(ts, {t: (ohlcv.get(t, {}).get("close") - spot_close.get(t)) / spot_close.get(t) for t in grid if isinstance(spot_close.get(t), (int, float)) and spot_close.get(t) != 0 and isinstance(ohlcv.get(t, {}).get("close"), (int, float))}, 24)

            # taker flows (perps)
            if ts in taker_perp:
                buy, sell = taker_perp[ts]
                if isinstance(buy, (int, float)) and isinstance(sell, (int, float)):
                    delta = float(buy) - float(sell)
                    row["delta_taker_5m"] = delta
                    cvd_cum += delta
                    row["cvd_perp_5m"] = cvd_cum
                    last_three_delta.append(delta)
                    row["cvd_perp_15m"] = sum(list(last_three_delta)) if len(last_three_delta) > 0 else math.nan
            # perp share over 60m (optional, requires taker_spot)
            if taker_spot:
                # sum of abs(perp delta) over 12 bars vs total (perp+spot)
                def _sum_abs(series: Mapping[int, Tuple[float, float]]) -> float:
                    acc = 0.0
                    t = ts - 11 * step
                    while t <= ts:
                        if t in series:
                            b, s = series[t]
                            acc += abs(float(b) - float(s))
                        t += step
                    return acc
                perp_abs = _sum_abs(taker_perp)
                spot_abs = _sum_abs(taker_spot)
                denom = perp_abs + spot_abs
                row["perp_share_60m"] = (perp_abs / denom) if denom > 0 else math.nan

            # liquidations
            if ts in liq:
                notional, count = liq[ts]
                row["liq_notional_5m"] = notional
                row["liq_count_5m"] = count
            # 60m window sum
            row["liq_notional_60m"] = _roll_sum(ts, {t: liq[t][0] for t in liq}, 12)

            # realized variance 15m (3 bars)
            c = ohlcv.get(ts, {}).get("close")
            if isinstance(c, (int, float)) and isinstance(last_close, (int, float)) and last_close > 0:
                r = math.log(float(c) / float(last_close))
                last_three_rets.append(r)
                row["rv_15m"] = sum(v * v for v in last_three_rets) if len(last_three_rets) > 0 else math.nan
            if isinstance(c, (int, float)):
                last_close = float(c)

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
            "funding_pctile_30d",
            "funding_pred_twap_60m",
            "oi_now",
            "oi_pctile_30d",
            "spread_bps",
            "depth_ratio",
            "basis_now",
            "basis_TWAP_60m",
            "basis_TWAP_120m",
            "delta_taker_5m",
            "cvd_perp_5m",
            "cvd_perp_15m",
            "perp_share_60m",
            "liq_notional_5m",
            "liq_count_5m",
            "liq_notional_60m",
            "rv_15m",
            "exch_reserve_flag",
            "etf_flow_flag",
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
