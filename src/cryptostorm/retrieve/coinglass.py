from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from ..config import EffectiveConfig, load_config, validate_config


LOG = logging.getLogger("cryptostorm.retrieve")


def _utc_now_ms() -> int:
    return int(time.time() * 1000)


def _to_ms(ts: Any) -> Optional[int]:
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        # assume seconds if too small
        v = int(ts)
        return v if v > 1_000_000_000_000 else v * 1000
    if isinstance(ts, str):
        try:
            if ts.isdigit():
                v = int(ts)
                return v if v > 1_000_000_000_000 else v * 1000
            # try ISO8601
            dt_obj = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return int(dt_obj.timestamp() * 1000)
        except Exception:
            return None
    return None


def _infer_ts_ms(item: Mapping[str, Any]) -> Optional[int]:
    for k in ("ts", "timestamp", "t", "time", "closeTime", "endTime"):
        if k in item:
            ms = _to_ms(item[k])
            if ms is not None:
                return ms
    return None


def _json_hash(obj: Any) -> str:
    s = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


@dataclass
class Endpoint:
    dataset: str  # normalized dataset key (e.g., futures_ohlcv_5m)
    preferred: str  # path starting with /api/...
    fallback: Optional[str] = None
    level: str = "symbol"  # 'symbol' or 'coin'


ENDPOINTS: Dict[str, Endpoint] = {
    "futures_ohlcv_5m": Endpoint(
        dataset="futures_ohlcv_5m",
        preferred="/api/price/ohlc-history",
        fallback=None,
        level="symbol",
    ),
    "spot_ohlcv_5m": Endpoint(
        dataset="spot_ohlcv_5m",
        preferred="/api/spot/price/history",
        fallback=None,
        level="symbol",
    ),
    "funding_8h": Endpoint(
        dataset="funding_8h",
        preferred="/api/futures/funding-rate/history",
        fallback="/api/futures/fundingRate/ohlc-history",
        level="symbol",
    ),
    "funding_pred_5m": Endpoint(
        dataset="funding_pred_5m",
        preferred="/api/futures/funding-rate/history",
        fallback="/api/futures/fundingRate/ohlc-history",
        level="symbol",
    ),
    "oi_5m_ohlc": Endpoint(
        dataset="oi_5m_ohlc",
        preferred="/api/futures/openInterest/ohlc-aggregated-history",
        fallback="/api/futures/openInterest/ohlc-history",
        level="coin",
    ),
    "taker_futures_5m": Endpoint(
        dataset="taker_futures_5m",
        preferred="/api/futures/taker-buy-sell-volume/history",
        fallback="/api/futures/aggregated-taker-buy-sell-volume/history",
        level="symbol",
    ),
    "taker_spot_5m": Endpoint(
        dataset="taker_spot_5m",
        preferred="/api/spot/taker-buy-sell-volume/history",
        fallback=None,
        level="symbol",
    ),
    "liquidation_5m": Endpoint(
        dataset="liquidation_5m",
        preferred="/api/futures/liquidation/aggregated-history",
        fallback="/api/futures/liquidation/history",
        level="coin",
    ),
    # Orderbook endpoints are placeholders; specifics may vary
    "orderbook_futures_5m": Endpoint(
        dataset="orderbook_futures_5m",
        preferred="/api/futures/orderbook/ask-bids-history",
        fallback=None,
        level="symbol",
    ),
    "orderbook_spot_5m": Endpoint(
        dataset="orderbook_spot_5m",
        preferred="/api/spot/orderbook/ask-bids-history",
        fallback=None,
        level="symbol",
    ),
}


def _default_headers(api_key: str) -> Dict[str, str]:
    # Coinglass docs vary; support common header names
    return {
        "Accept": "application/json",
        "User-Agent": "cryptostorm/0.1",
        "coinglassSecret": api_key,
        "CG-API-KEY": api_key,
        "X-API-KEY": api_key,
    }


def _http_get(base_url: str, path: str, params: Mapping[str, Any], headers: Mapping[str, str], *, backoff_initial: float, backoff_max: float) -> Dict[str, Any]:
    import urllib.parse
    import urllib.request

    url = f"{base_url.rstrip('/')}{path}"
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    full_url = f"{url}?{query}" if query else url
    attempt = 0
    backoff = max(0.1, float(backoff_initial))
    while True:
        attempt += 1
        req = urllib.request.Request(full_url, headers=dict(headers))
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                elapsed = (time.monotonic() - t0) * 1000
                LOG.debug("GET %s -> %s in %.1f ms", full_url, resp.status, elapsed)
                data = json.loads(raw.decode("utf-8"))
                return data
        except Exception as e:  # noqa: BLE001
            # Basic heuristics; treat as retryable up to a limit
            if attempt >= 6:
                raise
            time.sleep(min(backoff, backoff_max))
            backoff = min(backoff * 2, backoff_max)


def _extract_items(resp: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    # Try common envelope shapes
    for key in ("data", "list", "rows", "result", "items"):
        if key in resp and isinstance(resp[key], list):
            return list(resp[key])
    if isinstance(resp, list):  # rarely top-level array
        return list(resp)
    # If response has pagination fields and nested records
    for key, v in resp.items():
        if isinstance(v, list) and v and isinstance(v[0], Mapping):
            return list(v)
    return []


def _page_iter(base_url: str, headers: Mapping[str, str], path: str, params: Dict[str, Any], *, page_limit: int, backoff_initial: float, backoff_max: float) -> Iterable[Mapping[str, Any]]:
    page = 1
    total = 0
    while True:
        page_params = {**params, "page": page, "pageSize": page_limit, "limit": page_limit}
        resp = _http_get(base_url, path, page_params, headers, backoff_initial=backoff_initial, backoff_max=backoff_max)
        items = _extract_items(resp)
        if not items:
            break
        for it in items:
            yield it
        total += len(items)
        LOG.debug("page %s: got %s items (total=%s)", page, len(items), total)
        if len(items) < page_limit:
            break
        page += 1


def _build_params(eff: EffectiveConfig, ds_key: str, symbol: str, *, interval: Optional[str], mode: Optional[str], exchange: str, quote: str, start_ms: int, end_ms: int) -> Tuple[Dict[str, Any], bool]:
    # Return params and a flag whether this is aggregated-level query
    aggregated = (mode or "").lower() == "aggregated"
    params: Dict[str, Any] = {
        "interval": interval,
        "startTime": start_ms,
        "endTime": end_ms,
        "quote": quote,
    }
    if aggregated:
        coin = eff.symbol_to_coin.get(symbol)
        if not coin:
            # naive fallback: strip quote suffix
            coin = symbol[:-len(quote)] if symbol.endswith(quote) else symbol
        params.update({"coin": coin})
    else:
        params.update({"symbol": symbol, "exchange": exchange})
    return params, aggregated


def _output_filename(ds_key: str, exchange_market: str = "futures") -> str:
    mapping = {
        "futures_ohlcv_5m": "futures_ohlcv_5m.jsonl",
        "spot_ohlcv_5m": "spot_ohlcv_5m.jsonl",
        "funding_8h": "funding_8h_ohlc.jsonl",
        "funding_pred_5m": "funding_pred_5m_ohlc.jsonl",
        "oi_5m_ohlc": "oi_5m_ohlc.jsonl",
        "taker_futures_5m": "taker_futures_5m.jsonl",
        "taker_spot_5m": "taker_spot_5m.jsonl",
        "liquidation_5m": "liquidation_5m.jsonl",
        "orderbook_futures_5m": "orderbook_futures_5m.jsonl",
        "orderbook_spot_5m": "orderbook_spot_5m.jsonl",
    }
    return mapping.get(ds_key, f"{ds_key}.jsonl")


def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _load_existing_keys(path: Path) -> Tuple[set, set]:
    ts_keys: set = set()
    hashes: set = set()
    if not path.exists():
        return ts_keys, hashes
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                key = obj.get("ts")
                if key is not None:
                    ts_keys.add(int(key))
                payload = obj.get("payload", obj)
                hashes.add(_json_hash(payload))
    except Exception:
        pass
    return ts_keys, hashes


def _persist_jsonl(out_path: Path, items: Iterable[Mapping[str, Any]], *, symbol: str, interval: Optional[str], mode: Optional[str], endpoint: str, aggregated: bool) -> Tuple[int, int]:
    # returns (written, skipped)
    written = 0
    skipped = 0
    ts_keys, hashes = _load_existing_keys(out_path)

    with out_path.open("a", encoding="utf-8") as fh:
        for it in items:
            ts_ms = _infer_ts_ms(it)
            payload_hash = _json_hash(it)
            if ts_ms is not None and ts_ms in ts_keys:
                skipped += 1
                continue
            if payload_hash in hashes:
                skipped += 1
                continue
            record = {
                "ts": ts_ms,
                "symbol": symbol,
                "interval": interval,
                "mode": mode,
                "source": endpoint,
                "payload": it,
            }
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
            if ts_ms is not None:
                ts_keys.add(ts_ms)
            hashes.add(payload_hash)
            written += 1
    return written, skipped


def run_retrieve(
    eff: EffectiveConfig,
    *,
    base_url: str,
    exchange: str,
    quote: str,
    page_limit: int,
    backoff_initial: float,
    backoff_max: float,
    out_root: Path,
    api_key: Optional[str] = None,
    dry_run: bool = False,
) -> None:
    # Compute time window
    end_ms = _utc_now_ms()
    start_ms = end_ms - eff.days * 24 * 60 * 60 * 1000

    if not api_key and not dry_run:
        raise RuntimeError(
            f"API key missing; set {eff.api_key_env} or provide acquisition.coinglass.api_key_file"
        )
    headers = _default_headers(api_key or "")

    # Iterate datasets based on config enable flags
    ds_items = list(eff.datasets.items())
    for ds_key, ds_cfg in ds_items:
        if not ds_cfg.enabled:
            continue
        ep = ENDPOINTS.get(ds_key)
        if not ep:
            LOG.info("skip unsupported dataset: %s", ds_key)
            continue
        preferred = ep.preferred
        fallback = ep.fallback

        for sym in eff.symbols:
            # Determine level target
            target_value = sym
            if ep.level == "coin":
                # derive coin from mapping if possible
                # eff didn't carry mapping, but we can approximate by stripping quote
                if sym.endswith(quote):
                    target_value = sym[: -len(quote)]
            interval = ds_cfg.interval
            mode = ds_cfg.mode
            params, aggregated = _build_params(
                eff,
                ds_key,
                target_value,
                interval=interval,
                mode=mode,
                exchange=exchange,
                quote=quote,
                start_ms=start_ms,
                end_ms=end_ms,
            )
            # Build output path under data/<SYM>/
            out_dir = out_root / sym
            _ensure_dir(out_dir)
            out_file = out_dir / _output_filename(ds_key)

            def fetch_all(path: str) -> List[Mapping[str, Any]]:
                if dry_run:
                    LOG.info("DRY-RUN: GET %s params=%s", path, params)
                    return []
                return list(
                    _page_iter(
                        base_url,
                        headers,
                        path,
                        params,
                        page_limit=page_limit,
                        backoff_initial=backoff_initial,
                        backoff_max=backoff_max,
                    )
                )

            # Prefer aggregated/exchange as per mode, with fallback when specified
            used_path = preferred
            try:
                items = fetch_all(preferred)
                if not items and fallback:
                    LOG.info("fallback to %s for %s %s", fallback, ds_key, sym)
                    used_path = fallback
                    # Adjust params for fallback (ensure symbol/exchange present)
                    if aggregated and ep.level == "coin":
                        # Switch to exchange-level query with symbol
                        params = {
                            **params,
                            "coin": None,
                            "symbol": sym,
                            "exchange": exchange,
                        }
                    items = fetch_all(fallback)
            except Exception as e:  # noqa: BLE001
                if fallback:
                    LOG.warning("preferred endpoint failed (%s); trying fallback", e)
                    used_path = fallback
                    if aggregated and ep.level == "coin":
                        params = {
                            **params,
                            "coin": None,
                            "symbol": sym,
                            "exchange": exchange,
                        }
                    items = fetch_all(fallback)
                else:
                    raise

            written, skipped = _persist_jsonl(
                out_file,
                items,
                symbol=sym,
                interval=interval,
                mode=mode,
                endpoint=used_path,
                aggregated=aggregated,
            )
            LOG.info(
                "saved %s: +%d (skipped %d) -> %s",
                ds_key,
                written,
                skipped,
                out_file,
            )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="CryptoStorm retriever (Coinglass)")
    parser.add_argument("config", type=str, help="Path to YAML/JSON config")
    parser.add_argument("--out", type=str, default="data", help="Output root for data/<SYM>/")
    parser.add_argument("--dry-run", action="store_true", help="Print planned requests only")
    parser.add_argument("--log-level", type=str, default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cfg = load_config(args.config)
    eff, warns, errs = validate_config(cfg, require_env=not args.dry_run)
    for w in warns:
        LOG.warning(w)
    if errs:
        for e in errs:
            LOG.error(e)
        return 2

    # pull coinglass specifics
    acq = cfg.get("acquisition", {})
    cg = (acq or {}).get("coinglass", {})
    base_url = cg.get("base_url", "https://open-api-v4.coinglass.com")
    exchange = cg.get("exchange", "binance")
    quote = cg.get("quote", "USDT")
    paging = cg.get("paging", {})
    page_limit = int(paging.get("page_limit", 500))
    backoff_initial = float(paging.get("backoff_initial_s", 1))
    backoff_max = float(paging.get("backoff_max_s", 64))
    out_root = Path(args.out)

    # Resolve API key: prefer file if provided, else env
    api_key: Optional[str] = None
    api_key_file = cg.get("api_key_file")
    if isinstance(api_key_file, str) and api_key_file.strip():
        p = Path(api_key_file)
        if p.exists():
            try:
                api_key = p.read_text(encoding="utf-8").strip() or None
            except Exception:
                api_key = None
    if not api_key:
        api_key = os.getenv(eff.api_key_env)

    run_retrieve(
        eff,
        base_url=base_url,
        exchange=exchange,
        quote=quote,
        page_limit=page_limit,
        backoff_initial=backoff_initial,
        backoff_max=backoff_max,
        out_root=out_root,
        dry_run=args.dry_run,
        api_key=api_key,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
