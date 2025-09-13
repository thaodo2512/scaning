from __future__ import annotations

import argparse
import json
import math
import os
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


BINANCE_FAPI = "https://fapi.binance.com"

# Defaults per spec (can be overridden via env)
SPREAD_MAX_BPS_DEFAULT = float(os.getenv("CRYPTOSTORM_SPREAD_MAX_BPS", "5"))
DEPTH_MIN_USD_DEFAULT = float(os.getenv("CRYPTOSTORM_DEPTH_MIN_USD", "20000"))
RV_CAP_DEFAULT = float(os.getenv("CRYPTOSTORM_RV_CAP", "2.0"))
SHARPE_MIN_DEFAULT: Optional[float] = (
    float(os.getenv("CRYPTOSTORM_SHARPE_MIN")) if os.getenv("CRYPTOSTORM_SHARPE_MIN") else -0.25
)
POOL_MAX_DEFAULT = int(os.getenv("CRYPTOSTORM_POOL_MAX", "150"))
N_OI_DEFAULT = int(os.getenv("CRYPTOSTORM_N_OI", "50"))
OI_MIN_USD_DEFAULT = float(os.getenv("CRYPTOSTORM_OI_MIN_USD", "5000000"))
OI_TO_TURNOVER_MIN_DEFAULT = float(os.getenv("CRYPTOSTORM_OI_TO_TURNOVER_MIN", "0.10"))
REGION_DEFAULT = os.getenv("CRYPTOSTORM_REGION", "global")

ART_DIR = Path("artifacts/universe")
ART_JSON = ART_DIR / "binance_top.json"
ART_CSV = ART_DIR / "binance_top.csv"


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


def _read_region_profile(region: str) -> Dict[str, List[str]]:
    # Try configs/region_profile.yaml then region_profile.yaml
    import yaml  # type: ignore

    for p in (Path("configs/region_profile.yaml"), Path("region_profile.yaml")):
        if p.exists():
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                return {"deny_symbols": [], "deny_bases": [], "deny_keywords": []}
            # If it contains regions, pick region key; otherwise treat root as profile
            prof = data.get(region) if region in data else data
            return {
                "deny_symbols": list(prof.get("deny_symbols") or []),
                "deny_bases": list(prof.get("deny_bases") or []),
                "deny_keywords": list(prof.get("deny_keywords") or []),
            }
    return {"deny_symbols": [], "deny_bases": [], "deny_keywords": []}


def _futures_usdt_perp_symbols(verbose: bool = False) -> List[Dict[str, Any]]:
    if verbose:
        print("[binance-top] Fetching Binance exchange info (USDT‑M PERPETUAL, linear)…", flush=True)
    data = _http_get(f"{BINANCE_FAPI}/fapi/v1/exchangeInfo")
    out: List[Dict[str, Any]] = []
    for s in (data.get("symbols") or []):  # type: ignore[union-attr]
        try:
            if (
                s.get("status") == "TRADING"
                and s.get("quoteAsset") == "USDT"
                and s.get("marginAsset") == "USDT"
                and s.get("contractType") == "PERPETUAL"
            ):
                out.append(s)
        except Exception:
            continue
    out.sort(key=lambda d: d.get("symbol") or "")
    return out


@dataclass
class RankRow:
    symbol: str
    base: str
    vol30d_quote: float
    vol7d_quote: float
    vol24h_quote: float
    ret30d: float
    rv30d: float
    valid_days: int
    zero_volume_days: int
    spread_bps: float
    top_of_book_bid_usd: float
    top_of_book_ask_usd: float
    mid_price: float
    oi_usd: Optional[float] = None


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


def _book_ticker(symbol: str) -> Tuple[float, float, float, float, float]:
    """Return spread_bps, bid_usd, ask_usd, mid_price, ok_flag(mid>0 as 1/0)."""
    try:
        t = _http_get(f"{BINANCE_FAPI}/fapi/v1/ticker/bookTicker", {"symbol": symbol})
        bid = float(t.get("bidPrice") or 0)
        ask = float(t.get("askPrice") or 0)
        bqty = float(t.get("bidQty") or 0)
        aqty = float(t.get("askQty") or 0)
        mid = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else 0.0
        spread_bps = ((ask - bid) / mid * 10000.0) if mid > 0 else float("inf")
        bid_usd = bid * bqty
        ask_usd = ask * aqty
        return spread_bps, bid_usd, ask_usd, mid, 1.0 if mid > 0 else 0.0
    except Exception:
        return float("inf"), 0.0, 0.0, 0.0, 0.0


def _open_interest_usd(symbol: str, price: float) -> Optional[float]:
    try:
        oi = _http_get(f"{BINANCE_FAPI}/fapi/v1/openInterest", {"symbol": symbol})
        raw = oi.get("openInterest") if isinstance(oi, dict) else None
        if raw is None:
            return None
        val = float(raw)
        if price <= 0:
            return None
        return val * price
    except Exception:
        return None


def select_top_binance_perps(
    top: int,
    *,
    rps: float = 5.0,
    verbose: bool = False,
    region: str = REGION_DEFAULT,
    spread_max_bps: float = SPREAD_MAX_BPS_DEFAULT,
    depth_min_usd: float = DEPTH_MIN_USD_DEFAULT,
    rv_cap: float = RV_CAP_DEFAULT,
    sharpe_min: Optional[float] = SHARPE_MIN_DEFAULT,
    pool_max: int = POOL_MAX_DEFAULT,
    n_oi: int = N_OI_DEFAULT,
    oi_min_usd: float = OI_MIN_USD_DEFAULT,
    oi_to_turnover_min: float = OI_TO_TURNOVER_MIN_DEFAULT,
) -> List[RankRow]:
    # Base + compliance filters
    symbols_meta = _futures_usdt_perp_symbols(verbose=verbose)
    if not symbols_meta:
        if verbose:
            print("[binance-top] No symbols fetched.", flush=True)
        return []
    prof = _read_region_profile(region)
    deny_syms = set(s.upper() for s in prof.get("deny_symbols", []))
    deny_bases = set(s.upper() for s in prof.get("deny_bases", []))
    deny_kw = [s.upper() for s in prof.get("deny_keywords", [])]

    filtered: List[Dict[str, Any]] = []
    for s in symbols_meta:
        sym = str(s.get("symbol") or "").upper()
        base = str(s.get("baseAsset") or "").upper()
        if sym in deny_syms or base in deny_bases:
            continue
        up = sym.upper()
        if any(k in up for k in deny_kw):
            continue
        filtered.append(s)
    # Optional test limiter to reduce calls in dev/CI
    try:
        max_cand = int(os.getenv("CRYPTOSTORM_UNIVERSE_MAX_CANDIDATES", "0") or "0")
    except Exception:
        max_cand = 0
    if max_cand > 0:
        filtered = filtered[: max_cand]
    if verbose:
        print(f"[binance-top] Candidates after compliance filter: {len(filtered)}", flush=True)

    # Gather stats + microstructure with pacing
    delay = 0.0 if float(rps) <= 0 else 1.0 / float(rps)
    end_ms = _last_closed_day_end_ms()
    rows: List[RankRow] = []
    n = len(filtered)
    step = max(1, n // 20)
    t0 = time.time()
    # Diagnostics counters
    c_dq_drop = 0
    c_micro_spread = 0
    c_micro_depth = 0
    c_micro_other = 0

    for i, meta in enumerate(filtered):
        sym = str(meta.get("symbol") or "").upper()
        base = str(meta.get("baseAsset") or "").upper()
        vol30, vol7, ret30, rv30, valid, zero_days, vol24 = _metrics_for_symbol(sym, end_ms, delay)
        if delay > 0:
            time.sleep(delay)
        # Data quality guards
        if valid < 25 or zero_days > 2:
            c_dq_drop += 1
            continue
        # Microstructure snapshot
        spread_bps, bid_usd, ask_usd, mid, ok = _book_ticker(sym)
        if delay > 0:
            time.sleep(delay)
        depth_min = min(bid_usd, ask_usd)
        if not ok:
            c_micro_other += 1
            continue
        if spread_bps > spread_max_bps:
            c_micro_spread += 1
            continue
        if depth_min < depth_min_usd:
            c_micro_depth += 1
            continue
        rows.append(
            RankRow(
                symbol=sym,
                base=base,
                vol30d_quote=vol30,
                vol7d_quote=vol7,
                vol24h_quote=vol24,
                ret30d=ret30,
                rv30d=rv30,
                valid_days=valid,
                zero_volume_days=zero_days,
                spread_bps=spread_bps,
                top_of_book_bid_usd=bid_usd,
                top_of_book_ask_usd=ask_usd,
                mid_price=mid,
            )
        )
        if verbose and (i % step == 0 or i == n - 1):
            pct = int((i + 1) * 100 / n)
            elapsed = time.time() - t0
            print(f"[binance-top] Processing {i+1}/{n} ({pct}%) elapsed={elapsed:.1f}s", end="\r", flush=True)
    if verbose:
        print()

    if not rows:
        return []

    # Health gate
    gated: List[RankRow] = []
    c_health_rv = 0
    c_health_sharpe = 0
    for r in rows:
        if r.rv30d > rv_cap:
            c_health_rv += 1
            continue
        if sharpe_min is not None:
            # Approximate Sharpe from 30d daily rets
            # ret30d is sum of log returns; mean_daily = ret30d/len
            # If rv30d is annualized, convert daily stdev back: rv_daily = rv30d/sqrt(365)
            rv_daily = r.rv30d / math.sqrt(365.0) if r.rv30d > 0 else 0.0
            mean_daily = r.ret30d / 30.0
            sharpe = (mean_daily * math.sqrt(365.0) / rv_daily) if rv_daily > 0 else -1e9
            if sharpe < float(sharpe_min):
                c_health_sharpe += 1
                continue
        gated.append(r)

    if not gated:
        return []

    # Primary rank (deterministic)
    gated.sort(key=lambda r: (-r.vol30d_quote, -r.vol7d_quote, -r.vol24h_quote, r.symbol))

    # OI add-on on top N
    check_count = min(len(gated), max(n_oi, top))
    finalists: List[RankRow] = []
    c_oi_fail = 0
    for r in gated[:check_count]:
        oi_usd = _open_interest_usd(r.symbol, r.mid_price)
        r.oi_usd = oi_usd
        avg_daily_quote_7d = (r.vol7d_quote / 7.0) if r.vol7d_quote > 0 else 0.0
        if (
            (oi_usd is not None and oi_usd >= oi_min_usd)
            or (oi_usd is not None and avg_daily_quote_7d > 0 and (oi_usd / avg_daily_quote_7d) >= oi_to_turnover_min)
        ):
            finalists.append(r)
        if len(finalists) >= top:
            break
        if oi_usd is None or not (
            (oi_usd >= oi_min_usd) or (avg_daily_quote_7d > 0 and (oi_usd / avg_daily_quote_7d) >= oi_to_turnover_min)
        ):
            c_oi_fail += 1

    # Fallback: if not enough pass OI, just take top by rank without OI constraint (still health/microstructure-gated)
    if len(finalists) < top:
        add_more = [x for x in gated if x not in finalists]
        finalists.extend(add_more[: max(0, top - len(finalists))])

    # Diagnostics summary
    if verbose:
        try:
            print(
                "[binance-top] Drop summary: data_quality=%d, spread=%d, depth=%d, micro_other=%d, rv_cap=%d, sharpe=%d, oi_fail=%d; survivors=%d"
                % (c_dq_drop, c_micro_spread, c_micro_depth, c_micro_other, c_health_rv, c_health_sharpe, c_oi_fail, len(finalists)),
                flush=True,
            )
        except Exception:
            pass

    return finalists[: max(1, int(top))]


def _openai_chat_rank(candidates: List[RankRow], top: int, *, model: str, api_key: str, verbose: bool = False) -> List[str]:
    # Prepare a compact JSON for the model (limit to pool_max candidates to keep prompt size reasonable)
    k = min(len(candidates), POOL_MAX_DEFAULT)
    objs = [
        {
            "symbol": r.symbol,
            "vol30d_quote": round(float(r.vol30d_quote), 3),
            "vol7d_quote": round(float(r.vol7d_quote), 3),
            "vol24h_quote": round(float(r.vol24h_quote), 3),
            "ret30d": round(float(r.ret30d), 6),
            "rv30d": round(float(r.rv30d), 6),
            "spread_bps": round(float(r.spread_bps), 3),
            "depth_min_usd": round(float(min(r.top_of_book_bid_usd, r.top_of_book_ask_usd)), 2),
            "oi_usd": (round(float(r.oi_usd), 2) if r.oi_usd is not None else None),
        }
        for r in candidates[:k]
    ]
    sys = (
        "You are a crypto market assistant. Given candidate USDT perpetual futures symbols "
        "with metrics (30d/7d/24h quote volume, 30d return, 30d realized volatility, spreads, depth, optional OI), "
        "select the most interesting top symbols for systematic intraday research this month. Favor high liquidity, healthy volatility, "
        "and maintain diversity if ties. Return only symbols from the provided candidates."
    )
    user = {
        "task": "Select top symbols",
        "criteria": [
            "High 30d quote volume (primary)",
            "High 7d and 24h quote volume (secondary)",
            "Healthy realized volatility (avoid extreme tails)",
            "Diverse sectors if ties",
        ],
        "top": int(top),
        "candidates": objs,
        "output": "Return a JSON object with {symbols:[...]} with exactly 'top' symbols from candidates, in ranked order.",
    }
    import urllib.request
    import urllib.error
    import json as _json

    try:
        if verbose:
            print(f"[binance-top] Calling OpenAI model={model} for AI ranking on {len(objs)} candidates…", flush=True)
        body = _json.dumps({
            "model": model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": sys},
                {"role": "user", "content": _json.dumps(user)},
            ],
            "response_format": {"type": "json_object"},
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = _json.loads(resp.read().decode("utf-8", errors="replace"))
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "{}")
        parsed = _json.loads(content)
        out = [s for s in (parsed.get("symbols") or []) if isinstance(s, str)]
        # validate subset
        allow = {r.symbol for r in candidates}
        out = [s for s in out if s in allow]
        if verbose:
            print(f"[binance-top] AI selected {len(out)} symbols.", flush=True)
        return out[: max(1, int(top))]
    except Exception:
        return []


def _update_config_symbols(path: Path, symbols: Sequence[str]) -> None:
    # Load and write YAML; avoid adding dependency by expecting PyYAML already installed in project
    import yaml  # type: ignore

    cfg = {}
    if path.exists():
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if "universe" not in cfg or not isinstance(cfg["universe"], dict):
        cfg["universe"] = {}
    cfg["universe"]["symbols"] = list(symbols)
    path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")


def _emit_artifact(
    rows: List[RankRow],
    *,
    params: Dict[str, Any],
) -> None:
    ART_DIR.mkdir(parents=True, exist_ok=True)
    # Deterministic input hash based on rows' metric fields
    payload_rows = [
        {
            "symbol": r.symbol,
            "base": r.base,
            "vol30d_quote": r.vol30d_quote,
            "vol7d_quote": r.vol7d_quote,
            "vol24h_quote": r.vol24h_quote,
            "rv30d": r.rv30d,
            "ret30d": r.ret30d,
            "valid_days": r.valid_days,
            "zero_volume_days": r.zero_volume_days,
            "spread_bps": r.spread_bps,
            "top_of_book_bid_usd": r.top_of_book_bid_usd,
            "top_of_book_ask_usd": r.top_of_book_ask_usd,
            "oi_usd": r.oi_usd,
        }
        for r in rows
    ]
    h = sha256(json.dumps(payload_rows, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    now = datetime.utcnow().replace(tzinfo=timezone.utc)
    doc = {
        "generated_at": now.isoformat(),
        "region": params.get("region"),
        "k_final": int(params.get("top", 0)),
        "rps": float(params.get("rps", 0)),
        "thresholds": {
            "spread_max_bps": float(params.get("spread_max_bps")),
            "depth_min_usd": float(params.get("depth_min_usd")),
            "rv_cap": float(params.get("rv_cap")),
            "sharpe_min": params.get("sharpe_min"),
            "n_oi": int(params.get("n_oi")),
            "oi_min_usd": float(params.get("oi_min_usd")),
            "oi_to_turnover_min": float(params.get("oi_to_turnover_min")),
        },
        "inputs_hash": h,
        "rows": payload_rows,
        "symbols": [r["symbol"] for r in payload_rows],
    }
    ART_JSON.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    # Also emit CSV
    try:
        import csv

        with ART_CSV.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(
                f,
                fieldnames=list(payload_rows[0].keys()) if payload_rows else [
                    "symbol",
                    "base",
                    "vol30d_quote",
                    "vol7d_quote",
                    "vol24h_quote",
                    "rv30d",
                    "ret30d",
                    "valid_days",
                    "zero_volume_days",
                    "spread_bps",
                    "top_of_book_bid_usd",
                    "top_of_book_ask_usd",
                    "oi_usd",
                ],
            )
            w.writeheader()
            for row in payload_rows:
                w.writerow(row)
    except Exception:
        pass


def _maybe_load_frozen(top: int, region: str) -> Optional[Dict[str, Any]]:
    try:
        if not ART_JSON.exists():
            return None
        doc = json.loads(ART_JSON.read_text(encoding="utf-8"))
        gen = datetime.fromisoformat(doc.get("generated_at")).astimezone(timezone.utc)
        if datetime.utcnow().replace(tzinfo=timezone.utc) - gen < timedelta(hours=24):
            # If artifact has at least 'top' symbols, we can subset deterministically
            syms = [s for s in (doc.get("symbols") or []) if isinstance(s, str)]
            if syms and len(syms) >= 1:
                return {"symbols": syms[: max(1, int(top))], "doc": doc}
        return None
    except Exception:
        return None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Select top Binance USDT‑perp symbols and update config")
    parser.add_argument("--top", type=int, default=100, help="How many symbols to select")
    parser.add_argument("--rps", type=float, default=5.0, help="Binance API requests per second pacing")
    parser.add_argument("--out", type=str, help="Path to YAML config to write (updates universe.symbols)")
    parser.add_argument("--print", action="store_true", help="Print the symbol list to stdout")
    parser.add_argument("--ai", action="store_true", help="Use OpenAI to rank the candidates (requires OPENAI_API_KEY)")
    parser.add_argument("--openai-model", type=str, default="gpt-4o-mini", help="OpenAI chat model for ranking")
    args = parser.parse_args(argv)

    # Freeze for 24h if artifact exists; subset to top if needed
    frozen = _maybe_load_frozen(args.top, REGION_DEFAULT)
    if frozen:
        syms = list(frozen["symbols"])
        print(f"[binance-top] Using frozen universe ({len(syms)} symbols) from artifacts/universe (≤24h old)", flush=True)
        if args.out:
            _update_config_symbols(Path(args.out), syms)
            print(f"[binance-top] Updated {args.out} with {len(syms)} symbols", flush=True)
        if args.print or not args.out:
            print(json.dumps({"symbols": syms}, indent=2))
        return 0

    # Build according to v2.1 spec
    rows = select_top_binance_perps(
        args.top,
        rps=args.rps,
        verbose=True,
        region=REGION_DEFAULT,
        spread_max_bps=SPREAD_MAX_BPS_DEFAULT,
        depth_min_usd=DEPTH_MIN_USD_DEFAULT,
        rv_cap=RV_CAP_DEFAULT,
        sharpe_min=SHARPE_MIN_DEFAULT,
        pool_max=POOL_MAX_DEFAULT,
        n_oi=N_OI_DEFAULT,
        oi_min_usd=OI_MIN_USD_DEFAULT,
        oi_to_turnover_min=OI_TO_TURNOVER_MIN_DEFAULT,
    )

    # Optional AI refinement over a bounded pool (top * 2 up to POOL_MAX)
    if args.ai and rows:
        # Build pool from additional survivors by relaxing OI fallback handling: take ranked candidates from the selection flow
        pool = rows  # already ranked/gated list; small by top; in future we could expand pool by re-running rank without OI
        api_key = os.getenv("OPENAI_API_KEY") or ""
        ai_syms: List[str] = []
        if api_key:
            ai_syms = _openai_chat_rank(pool, args.top, model=args.openai_model, api_key=api_key, verbose=True)
        if ai_syms:
            rows = [r for r in rows if r.symbol in set(ai_syms)]
            # Preserve AI order
            rows.sort(key=lambda r: ai_syms.index(r.symbol))

    syms = [r.symbol for r in rows[: args.top]]

    # Emit artifact (JSON + CSV)
    _emit_artifact(
        rows[: args.top],
        params={
            "region": REGION_DEFAULT,
            "top": args.top,
            "rps": args.rps,
            "spread_max_bps": SPREAD_MAX_BPS_DEFAULT,
            "depth_min_usd": DEPTH_MIN_USD_DEFAULT,
            "rv_cap": RV_CAP_DEFAULT,
            "sharpe_min": SHARPE_MIN_DEFAULT,
            "n_oi": N_OI_DEFAULT,
            "oi_min_usd": OI_MIN_USD_DEFAULT,
            "oi_to_turnover_min": OI_TO_TURNOVER_MIN_DEFAULT,
        },
    )

    print(f"[binance-top] Selected {len(syms)} symbols.", flush=True)
    if args.out:
        _update_config_symbols(Path(args.out), syms)
        print(f"[binance-top] Updated {args.out} with {len(syms)} symbols", flush=True)
    if args.print or not args.out:
        print(json.dumps({"symbols": syms}, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
