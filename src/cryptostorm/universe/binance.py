from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
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


def _sum_30d_quote_volume(symbol: str, rps_delay_s: float) -> Tuple[float, float]:
    # Use daily klines for 30 days; fields: [openTime, open, high, low, close, volume, closeTime, quoteAssetVolume, ...]
    # Also fetch 24h ticker for a quick 24h quote volume snapshot
    import urllib.error

    vol30 = 0.0
    vol24 = 0.0
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

    time.sleep(max(0.0, rps_delay_s))

    # 30d daily klines (limit 30)
    try:
        arr = _http_get(f"{BINANCE_FAPI}/fapi/v1/klines", {"symbol": symbol, "interval": "1d", "limit": 30})
        if isinstance(arr, list):
            for row in arr:
                if isinstance(row, list) and len(row) >= 8:
                    qv = row[7]
                    try:
                        vol30 += float(qv)
                    except Exception:
                        continue
    except urllib.error.HTTPError:
        pass
    except Exception:
        pass
    return vol30, vol24


def select_top_binance_perps(top: int, *, rps: float = 5.0) -> List[RankRow]:
    symbols = _futures_usdt_perp_symbols()
    if not symbols:
        return []
    delay = 1.0 / max(1.0, float(rps))
    rows: List[RankRow] = []
    for i, sym in enumerate(symbols):
        vol30, vol24 = _sum_30d_quote_volume(sym, delay)
        rows.append(RankRow(sym, vol30, vol24))
        # basic pacing
        time.sleep(max(0.0, delay))
    rows.sort(key=lambda r: (r.vol30d_quote, r.vol24h_quote), reverse=True)
    return rows[: max(1, int(top))]


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
    args = parser.parse_args(argv)

    rows = select_top_binance_perps(args.top, rps=args.rps)
    syms = [r.symbol for r in rows]
    if args.out:
        _update_config_symbols(Path(args.out), syms)
        print(f"Updated {args.out} with {len(syms)} symbols")
    if args.print or not args.out:
        print(json.dumps({"symbols": syms}, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

