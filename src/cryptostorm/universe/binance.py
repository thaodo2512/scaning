from __future__ import annotations

import argparse
import json
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


BINANCE_FAPI = "https://fapi.binance.com"


def _http_get(url: str, params: Optional[Dict[str, Any]] = None, timeout: float = 20.0) -> Any:
    import urllib.parse
    import urllib.request

    q = urllib.parse.urlencode(params or {})
    full = f"{url}?{q}" if q else url
    req = urllib.request.Request(full, headers={"Accept": "application/json", "User-Agent": "cryptostorm/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except Exception:
            return body


def _last_closed_day_end_ms(now: Optional[datetime] = None) -> int:
    dt = (now or datetime.utcnow().replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    start_today = datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)
    end_prev = start_today - timedelta(milliseconds=1)
    return int(end_prev.timestamp() * 1000)

def _futures_usdt_perp_symbols(verbose: bool = False) -> List[str]:
    if verbose:
        print("[binance-top] Fetching Binance exchange info (USDT‑M PERPETUAL)…", flush=True)
    data = _http_get(f"{BINANCE_FAPI}/fapi/v1/exchangeInfo")
    out: List[str] = []
    for s in (data.get("symbols") or []):  # type: ignore[union-attr]
        try:
            if (
                s.get("status") == "TRADING"
                and s.get("quoteAsset") == "USDT"
                and s.get("contractType") == "PERPETUAL"
            ):
                sym = s.get("symbol")
                if isinstance(sym, str):
                    out.append(str(sym))
        except Exception:
            continue
    return sorted(out)


def _symbol_to_base_map(verbose: bool = False) -> Dict[str, str]:
    """Return {symbol: baseAsset} for USDT‑M PERPETUAL trading pairs from exchangeInfo."""
    if verbose:
        print("[binance-top] Fetching Binance exchange info for base assets…", flush=True)
    data = _http_get(f"{BINANCE_FAPI}/fapi/v1/exchangeInfo")
    m: Dict[str, str] = {}
    for s in (data.get("symbols") or []):  # type: ignore[union-attr]
        try:
            if (
                s.get("status") == "TRADING"
                and s.get("quoteAsset") == "USDT"
                and s.get("contractType") == "PERPETUAL"
            ):
                sym = s.get("symbol")
                base = s.get("baseAsset")
                if isinstance(sym, str) and isinstance(base, str):
                    m[str(sym)] = str(base)
        except Exception:
            continue
    return m


@dataclass
class RankRow:
    symbol: str
    vol30d_quote: float
    vol7d_quote: float
    vol24h_quote: float
    ret30d: float = 0.0
    rv30d: float = 0.0
    valid_days: int = 0
    zero_volume_days: int = 0


def _metrics_for_symbol(symbol: str, end_ms: int, rps_delay_s: float) -> Tuple[float, float, float, float, int, int, float]:
    """Return vol30, vol7, ret30, rv30, valid_days, zero_vol_days, vol24h."""
    import urllib.error

    vol30 = 0.0
    vol7 = 0.0
    vol24 = 0.0
    ret30 = 0.0
    rv30 = 0.0
    valid = 0
    zero_days = 0

    # 24h ticker (single request)
    try:
        t = _http_get(f"{BINANCE_FAPI}/fapi/v1/ticker/24hr", {"symbol": symbol})
        v = t.get("quoteVolume") if isinstance(t, dict) else None
        if isinstance(v, (int, float)) or (isinstance(v, str) and v.replace(".", "", 1).isdigit()):
            vol24 = float(v)
    except urllib.error.HTTPError:
        pass
    except Exception:
        pass

    if rps_delay_s > 0:
        time.sleep(rps_delay_s)

    # 30d daily klines ending at last closed day
    try:
        arr = _http_get(
            f"{BINANCE_FAPI}/fapi/v1/klines",
            {"symbol": symbol, "interval": "1d", "limit": 30, "endTime": end_ms},
        )
        if isinstance(arr, list):
            closes: List[float] = []
            qvols: List[float] = []
            for row in arr:
                if isinstance(row, list) and len(row) >= 8:
                    qv = row[7]
                    c = row[4]
                    ok = True
                    try:
                        qvf = float(qv)
                    except Exception:
                        ok = False
                        qvf = 0.0
                    try:
                        cf = float(c)
                    except Exception:
                        ok = False
                        cf = 0.0
                    if ok:
                        valid += 1
                    if qvf == 0.0:
                        zero_days += 1
                    qvols.append(qvf)
                    closes.append(cf)
            vol30 = float(sum(qvols))
            vol7 = float(sum(qvols[-7:])) if qvols else 0.0
            # compute 30d return and realized vol from closes
            if len(closes) >= 2:
                rets: List[float] = []
                for i in range(1, len(closes)):
                    if closes[i - 1] > 0 and closes[i] > 0:
                        rets.append(math.log(closes[i] / closes[i - 1]))
                if rets:
                    ret30 = float(sum(rets))
                    m = sum(rets) / len(rets)
                    rv_daily = (sum((x - m) ** 2 for x in rets) / len(rets)) ** 0.5
                    rv30 = float(rv_daily * math.sqrt(365.0))
    except urllib.error.HTTPError:
        pass
    except Exception:
        pass

    return vol30, vol7, ret30, rv30, valid, zero_days, vol24

def _symbols_from_data(data_root: Path, verbose: bool = False) -> List[str]:
    out: List[str] = []
    if not data_root.exists():
        return out
    for p in sorted(data_root.iterdir()):
        if not p.is_dir():
            continue
        name = p.name
        if not name.endswith("USDT"):
            continue
        # Consider symbols that have futures OHLCV captured
        if (p / "futures_ohlcv_15m.jsonl").exists() or (p / "futures_ohlcv_5m.jsonl").exists():
            out.append(name)
    if verbose:
        print(f"[binance-top] Found {len(out)} symbols from local Coinglass data at {data_root}", flush=True)
    return out


def _coinglass_local_metrics(symbol: str, data_root: Path, now_ms: Optional[int] = None) -> Tuple[float, float, float, float, int, int]:
    """Compute vol30d (USD), vol24h (USD), vol7d(USD), ret30d (log), rv30d (annualized), valid_days, zero_days
    from local Coinglass JSONL (prefer 15m, fallback 5m).
    """
    root = data_root / symbol
    path = root / "futures_ohlcv_15m.jsonl"
    interval_min = 15
    if not path.exists():
        path = root / "futures_ohlcv_5m.jsonl"
        interval_min = 5
    if not path.exists():
        return 0.0, 0.0, 0.0, 0.0, 0, 0

    end_ms = int(now_ms or (time.time() * 1000))
    start30 = end_ms - 30 * 24 * 60 * 60 * 1000
    start7 = end_ms - 7 * 24 * 60 * 60 * 1000
    start24 = end_ms - 24 * 60 * 60 * 1000

    vol30 = 0.0
    vol7 = 0.0
    vol24 = 0.0
    closes: List[float] = []
    days_seen: set = set()
    zero_days: Dict[str, float] = {}
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                ts = int(obj.get("ts") or 0)
                if ts < start30:
                    continue
                payload = obj.get("payload") or {}
                try:
                    v_usd = float(payload.get("volume_usd") or 0.0)
                except Exception:
                    v_usd = 0.0
                try:
                    close = float(payload.get("close") or 0.0)
                except Exception:
                    close = 0.0
                # day key
                try:
                    d = datetime.utcfromtimestamp(ts / 1000).strftime("%Y-%m-%d")
                except Exception:
                    d = ""
                if d:
                    days_seen.add(d)
                    zero_days[d] = zero_days.get(d, 0.0) + v_usd
                if v_usd > 0:
                    vol30 += v_usd
                    if ts >= start7:
                        vol7 += v_usd
                    if ts >= start24:
                        vol24 += v_usd
                if close > 0:
                    closes.append(close)
    except Exception:
        return 0.0, 0.0, 0.0, 0.0, 0, 0

    # Compute returns/rv from bar closes (approximate from 15m/5m)
    ret30 = 0.0
    rv30 = 0.0
    if len(closes) >= 2:
        rets: List[float] = []
        for i in range(1, len(closes)):
            c0 = closes[i - 1]
            c1 = closes[i]
            if c0 > 0 and c1 > 0:
                rets.append(math.log(c1 / c0))
        if rets:
            ret30 = float(sum(rets))
            m = sum(rets) / len(rets)
            rv_bar = (sum((x - m) ** 2 for x in rets) / len(rets)) ** 0.5
            bars_per_year = 365.0 * 24.0 * 60.0 / float(interval_min)
            rv30 = float(rv_bar * math.sqrt(bars_per_year))
    valid_days = len(days_seen)
    zero_count = sum(1 for _, vv in zero_days.items() if vv == 0.0)
    return vol30, vol24, vol7, ret30, rv30, valid_days


def select_top_binance_perps(top: int, *, rps: float = 5.0, verbose: bool = False, data_root: Optional[Path] = None) -> List[RankRow]:
    """Select top USDT‑perp symbols by quote volume.

    Behavior change: Binance-first. Fetch the candidate list from Binance regardless
    of local data presence so we can return exactly `--top` when possible. If the
    Binance call fails or returns nothing (e.g., network issues/418), fall back to
    local Coinglass data under `data/`.

    Metric sourcing remains hybrid: when computing per-symbol metrics, prefer local
    Coinglass JSONL for symbols that exist under `data/` to keep determinism; otherwise
    compute from Binance endpoints.
    """
    # Binance-first candidate set
    symbols: List[str] = _futures_usdt_perp_symbols(verbose=verbose)
    if not symbols and data_root is not None:
        # Fallback to local data only if Binance list is unavailable
        if verbose:
            print("[binance-top] Binance candidates unavailable; falling back to local data", flush=True)
        symbols = _symbols_from_data(data_root, verbose=verbose)
    if not symbols:
        if verbose:
            print("[binance-top] No symbols available from Binance or local data", flush=True)
        return []
    # At this point, 'symbols' contains either local candidates from data/ or Binance list
    # Optional test limiter to reduce calls in dev/CI
    try:
        max_cand = int(os.getenv("CRYPTOSTORM_UNIVERSE_MAX_CANDIDATES", "0") or "0")
    except Exception:
        max_cand = 0
    if max_cand > 0:
        symbols = symbols[: max_cand]
    if verbose:
        print(f"[binance-top] Candidates after base filter: {len(symbols)}", flush=True)

    delay = 0.0 if float(rps) <= 0 else 1.0 / float(rps)
    end_ms = _last_closed_day_end_ms()
    rows: List[RankRow] = []
    n = len(symbols)
    step = max(1, n // 20)
    t0 = time.time()
    for i, sym in enumerate(symbols):
        if data_root is not None and (data_root / sym).exists():
            vol30, vol24, vol7, ret30, rv30, valid = _coinglass_local_metrics(sym, data_root, now_ms=end_ms)
        else:
            vol30, vol7, ret30, rv30, valid, zero_days, vol24 = _metrics_for_symbol(sym, end_ms, delay)
        rows.append(RankRow(symbol=sym, vol30d_quote=vol30, vol7d_quote=vol7, vol24h_quote=vol24, ret30d=ret30, rv30d=rv30, valid_days=valid))
        if delay > 0 and (data_root is None or not (data_root / sym).exists()):
            time.sleep(delay)
        if verbose and (i % step == 0 or i == n - 1):
            pct = int((i + 1) * 100 / n)
            elapsed = time.time() - t0
            print(f"[binance-top] Processing {i+1}/{n} ({pct}%) elapsed={elapsed:.1f}s", end="\r", flush=True)
    if verbose:
        print()  # newline after progress
        print("[binance-top] Ranking candidates by 30d/24h volume…", flush=True)
    rows.sort(key=lambda r: (-r.vol30d_quote, -r.vol24h_quote, r.symbol))
    return rows[: max(1, int(top))]


# AI ranking removed; deterministic ranking only


def _update_config_symbols_and_mapping(path: Path, symbols: Sequence[str], sym_to_base: Mapping[str, str]) -> None:
    # Load and write YAML; avoid adding dependency by expecting PyYAML already installed in project
    import yaml  # type: ignore

    cfg = {}
    if path.exists():
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if "universe" not in cfg or not isinstance(cfg["universe"], dict):
        cfg["universe"] = {}
    cfg["universe"]["symbols"] = list(symbols)
    # Update conventions.symbol_to_coin mapping for selected symbols
    conv = cfg.get("conventions") or {}
    if not isinstance(conv, dict):
        conv = {}
    sym_map = conv.get("symbol_to_coin") or {}
    if not isinstance(sym_map, dict):
        sym_map = {}
    for sym in symbols:
        base = sym_to_base.get(sym) or (sym[:-4] if sym.endswith("USDT") else sym)
        try:
            sym_map[str(sym)] = str(base)
        except Exception:
            pass
    conv["symbol_to_coin"] = sym_map
    cfg["conventions"] = conv
    path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Select top Binance USDT‑perp symbols and update config")
    parser.add_argument("--top", type=int, default=100, help="How many symbols to select")
    parser.add_argument("--data", type=str, default="data", help="Path to local Coinglass data directory (preferred)")
    parser.add_argument("--rps", type=float, default=5.0, help="Binance API requests per second pacing (only used if no local data)")
    parser.add_argument("--out", type=str, help="Path to YAML config to write (updates universe.symbols)")
    parser.add_argument("--print", action="store_true", help="Print the symbol list to stdout")
    # AI ranking removed
    args = parser.parse_args(argv)

    # Build simple deterministic ranking (prefer local Coinglass data)
    data_root = Path(args.data) if args.data else None
    rows = select_top_binance_perps(args.top, rps=args.rps, verbose=True, data_root=data_root)
    syms = [r.symbol for r in rows]

    print(f"[binance-top] Selected {len(syms)} symbols.", flush=True)
    if args.out:
        # Build base map (fallback to strip 'USDT' if not found)
        base_map = _symbol_to_base_map(verbose=True)
        _update_config_symbols_and_mapping(Path(args.out), syms, base_map)
        print(f"[binance-top] Updated {args.out} with {len(syms)} symbols", flush=True)
    if args.print or not args.out:
        print(json.dumps({"symbols": syms}, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
