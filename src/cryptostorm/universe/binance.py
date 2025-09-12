from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
import os
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


def _futures_usdt_perp_symbols() -> List[str]:
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
                    out.append(sym)
        except Exception:
            continue
    return sorted(out)


@dataclass
class RankRow:
    symbol: str
    vol30d_quote: float
    vol24h_quote: float
    ret30d: float = 0.0
    rv30d: float = 0.0


def _sum_30d_quote_volume(symbol: str, rps_delay_s: float) -> Tuple[float, float, float, float]:
    # Use daily klines for 30 days; fields: [openTime, open, high, low, close, volume, closeTime, quoteAssetVolume, ...]
    # Also fetch 24h ticker for a quick 24h quote volume snapshot
    import urllib.error

    vol30 = 0.0
    vol24 = 0.0
    ret30 = 0.0
    rv30 = 0.0
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

    # 30d daily klines (limit 30)
    try:
        arr = _http_get(f"{BINANCE_FAPI}/fapi/v1/klines", {"symbol": symbol, "interval": "1d", "limit": 30})
        if isinstance(arr, list):
            closes: List[float] = []
            for row in arr:
                if isinstance(row, list) and len(row) >= 8:
                    qv = row[7]
                    try:
                        vol30 += float(qv)
                    except Exception:
                        continue
                    try:
                        closes.append(float(row[4]))
                    except Exception:
                        pass
            # compute 30d return and realized vol from closes
            if len(closes) >= 2:
                try:
                    ret30 = (closes[-1] / closes[0]) - 1.0 if closes[0] > 0 else 0.0
                except Exception:
                    ret30 = 0.0
                rets: List[float] = []
                for i in range(1, len(closes)):
                    try:
                        if closes[i - 1] > 0:
                            import math as _m
                            rets.append(_m.log(closes[i] / closes[i - 1]))
                    except Exception:
                        continue
                if rets:
                    # population std of daily log returns as proxy
                    m = sum(rets) / len(rets)
                    rv30 = (sum((x - m) ** 2 for x in rets) / len(rets)) ** 0.5
    except urllib.error.HTTPError:
        pass
    except Exception:
        pass
    return vol30, vol24, ret30, rv30


def select_top_binance_perps(top: int, *, rps: float = 5.0) -> List[RankRow]:
    symbols = _futures_usdt_perp_symbols()
    if not symbols:
        return []
    delay = 0.0 if float(rps) <= 0 else 1.0 / float(rps)
    rows: List[RankRow] = []
    for i, sym in enumerate(symbols):
        vol30, vol24, ret30, rv30 = _sum_30d_quote_volume(sym, delay)
        rows.append(RankRow(sym, vol30, vol24, ret30, rv30))
        # basic pacing
        if delay > 0:
            time.sleep(delay)
    rows.sort(key=lambda r: (r.vol30d_quote, r.vol24h_quote), reverse=True)
    return rows[: max(1, int(top))]


def _openai_chat_rank(candidates: List[RankRow], top: int, *, model: str, api_key: str) -> List[str]:
    # Prepare a compact JSON for the model (limit to 150 candidates to keep prompt size reasonable)
    k = min(len(candidates), 150)
    objs = [
        {
            "symbol": r.symbol,
            "vol30d_quote": round(float(r.vol30d_quote), 3),
            "vol24h_quote": round(float(r.vol24h_quote), 3),
            "ret30d": round(float(r.ret30d), 6),
            "rv30d": round(float(r.rv30d), 6),
        }
        for r in candidates[:k]
    ]
    sys = (
        "You are a crypto market assistant. Given candidate USDT perpetual futures symbols "
        "with metrics (30d/24h quote volume, 30d return, 30d realized volatility), select the most interesting top symbols "
        "for systematic intraday research this month. Favor high liquidity (volumes), healthy volatility (rv), and avoid extremely illiquid pairs."
    )
    user = {
        "task": "Select top symbols",
        "criteria": [
            "High 30d quote volume (primary)",
            "High 30d realized volatility (secondary)",
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


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Select top Binance USDT‑perp symbols and update config")
    parser.add_argument("--top", type=int, default=100, help="How many symbols to select")
    parser.add_argument("--rps", type=float, default=5.0, help="Binance API requests per second pacing")
    parser.add_argument("--out", type=str, help="Path to YAML config to write (updates universe.symbols)")
    parser.add_argument("--print", action="store_true", help="Print the symbol list to stdout")
    parser.add_argument("--ai", action="store_true", help="Use OpenAI to rank the candidates (requires OPENAI_API_KEY)")
    parser.add_argument("--openai-model", type=str, default="gpt-4o-mini", help="OpenAI chat model for ranking")
    args = parser.parse_args(argv)

    rows = select_top_binance_perps(args.top * 2, rps=args.rps)  # gather a larger candidate set when AI is enabled
    if args.ai:
        api_key = os.getenv("OPENAI_API_KEY") or ""
        ai_syms: List[str] = []
        if api_key:
            ai_syms = _openai_chat_rank(rows, args.top, model=args.openai_model, api_key=api_key)
        syms = ai_syms or [r.symbol for r in rows[: args.top]]
    else:
        syms = [r.symbol for r in rows]
    if args.out:
        _update_config_symbols(Path(args.out), syms)
        print(f"Updated {args.out} with {len(syms)} symbols")
    if args.print or not args.out:
        print(json.dumps({"symbols": syms}, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
