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
import math

from ..config import EffectiveConfig, load_config, validate_config


LOG = logging.getLogger("cryptostorm.retrieve")
_HTTP_DEBUG = os.getenv("CRYPTOSTORM_HTTP_DEBUG", "").lower() in {"1", "true", "yes", "on"}


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


def _ms_to_iso(ms: int) -> str:
    try:
        import datetime as _dt

        return _dt.datetime.utcfromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M:%SZ")
    except Exception:
        return str(ms)


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
        preferred="/api/futures/price/history",
        fallback="/api/price/ohlc-history",
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
        preferred="/api/futures/open-interest/aggregated-history",
        fallback="/api/futures/openInterest/ohlc-history",
        level="coin",
    ),
    "taker_futures_5m": Endpoint(
        dataset="taker_futures_5m",
        preferred="/api/futures/v2/taker-buy-sell-volume/history",
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


def _path_is_v4(path: str) -> bool:
    v4_patterns = (
        "/api/futures/price/history",
        "/api/spot/price/history",
        "/api/futures/funding-rate/history",
        "/api/futures/v2/taker-buy-sell-volume/history",
        "/api/spot/taker-buy-sell-volume/history",
        "/api/futures/open-interest/aggregated-history",
        "/api/futures/liquidation/aggregated-history",
        "/api/futures/orderbook/ask-bids-history",
        "/api/spot/orderbook/ask-bids-history",
        "/api/futures/orderbook/aggregated-ask-bids-history",
        "/api/spot/orderbook/aggregated-ask-bids-history",
    )
    return any(path.startswith(p) for p in v4_patterns)


def _http_get(base_url: str, path: str, params: Mapping[str, Any], headers: Mapping[str, str], *, backoff_initial: float, backoff_max: float) -> Dict[str, Any]:
    import urllib.parse
    import urllib.request

    url = f"{base_url.rstrip('/')}{path}"
    # Normalize parameter names to match endpoint conventions
    p = dict(params)
    if _path_is_v4(path):
        # startTime/endTime -> start_time/end_time
        if "startTime" in p:
            p["start_time"] = p.pop("startTime")
        if "endTime" in p:
            p["end_time"] = p.pop("endTime")
        # page/pageSize not part of v4; use 'limit'
        if "pageSize" in p:
            p.setdefault("limit", p.get("pageSize"))
            p.pop("pageSize", None)
        p.pop("page", None)
        # exchange casing commonly shown as 'Binance'
        if "exchange" in p and isinstance(p["exchange"], str):
            if p["exchange"].lower() == "binance":
                p["exchange"] = "Binance"
        # quote generally not required when using symbol=BTCUSDT
        p.pop("quote", None)
    # Clean None values
    query = urllib.parse.urlencode({k: v for k, v in p.items() if v is not None})
    full_url = f"{url}?{query}" if query else url
    attempt = 0
    backoff = max(0.1, float(backoff_initial))
    while True:
        attempt += 1
        # Log the requested URL only when HTTP debug is enabled (env) or logger is in DEBUG
        try:
            if _HTTP_DEBUG or LOG.isEnabledFor(logging.DEBUG):
                LOG.debug("HTTP GET %s", full_url)
        except Exception:
            pass
        req = urllib.request.Request(full_url, headers=dict(headers))
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                elapsed = (time.monotonic() - t0) * 1000
                LOG.debug("GET %s -> %s in %.1f ms", full_url, resp.status, elapsed)
                text = raw.decode("utf-8", errors="replace")
                try:
                    data = json.loads(text)
                except Exception:
                    # If the response isn't JSON, log textual snippet and re-raise
                    try:
                        snippet = text if len(text) <= 4000 else text[:4000] + " … (truncated)"
                        if _HTTP_DEBUG or LOG.isEnabledFor(logging.DEBUG):
                            LOG.debug("HTTP RESP (non-JSON) %s %s", full_url, snippet)
                    except Exception:
                        pass
                    raise
                # Log JSON response in compact single-line form (truncated)
                try:
                    js = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
                    if len(js) > 8000:
                        js = js[:8000] + " … (truncated)"
                    if _HTTP_DEBUG or LOG.isEnabledFor(logging.DEBUG):
                        LOG.debug("HTTP RESP %s %s", full_url, js)
                except Exception:
                    # Best-effort logging; ignore failures
                    pass
                return data
        except Exception as e:  # noqa: BLE001
            # Basic heuristics; treat as retryable up to a limit
            if attempt >= 6:
                raise
            time.sleep(min(backoff, backoff_max))
            backoff = min(backoff * 2, backoff_max)


def _extract_items(resp: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """Extract a list of records from various response envelope shapes.

    Supports:
    - { data: [ ... ] }
    - { list: [ ... ] }
    - { data: { list: [ ... ] } }
    - { result: { rows: [ ... ] } }
    - Any nested dict that contains a list of mappings.
    """
    # 1) Direct common keys at top-level
    for key in ("data", "list", "rows", "result", "items"):
        v = resp.get(key)
        if isinstance(v, list):
            return list(v)
        if isinstance(v, Mapping):
            # 2) Nested common keys
            for nk in ("data", "list", "rows", "items", "result"):
                nv = v.get(nk) if hasattr(v, "get") else None
                if isinstance(nv, list):
                    return list(nv)
    # 3) Top-level direct list
    if isinstance(resp, list):
        return list(resp)
    # 4) Fallback: scan nested values shallowly
    for _, v in resp.items():
        if isinstance(v, list) and v and isinstance(v[0], Mapping):
            return list(v)
        if isinstance(v, Mapping):
            for nv in v.values():
                if isinstance(nv, list) and nv and isinstance(nv[0], Mapping):
                    return list(nv)
    return []


def _interval_to_ms(interval: Optional[str]) -> Optional[int]:
    if not isinstance(interval, str):
        return None
    s = interval.lower().strip()
    if s.endswith("ms") and s[:-2].isdigit():
        return int(s[:-2])
    if s.endswith("s") and s[:-1].isdigit():
        return int(s[:-1]) * 1000
    if s.endswith("m") and s[:-1].isdigit():
        return int(s[:-1]) * 60 * 1000
    if s.endswith("h") and s[:-1].isdigit():
        return int(s[:-1]) * 60 * 60 * 1000
    if s.endswith("d") and s[:-1].isdigit():
        return int(s[:-1]) * 24 * 60 * 60 * 1000
    return None


def _floor_to_step(ms: int, step_ms: int) -> int:
    if step_ms <= 0:
        return ms
    return (int(ms) // int(step_ms)) * int(step_ms)


def _ceil_to_step(ms: int, step_ms: int) -> int:
    if step_ms <= 0:
        return ms
    q, r = divmod(int(ms), int(step_ms))
    return q * int(step_ms) if r == 0 else (q + 1) * int(step_ms)


def _canonical_interval_ms(ds_key: str, interval: Optional[str]) -> Optional[int]:
    """Resolve a concrete step size in ms for alignment.

    - Most datasets use their declared interval.
    - Orderbook "last_of_5m" is treated as a 5-minute grid for alignment.
    """
    # Treat orderbook sampling as 5-minute grid
    if ds_key in {"orderbook_futures_5m", "orderbook_spot_5m"}:
        return 5 * 60 * 1000
    return _interval_to_ms(interval)


def _page_iter(base_url: str, headers: Mapping[str, str], path: str, params: Dict[str, Any], *, page_limit: int, backoff_initial: float, backoff_max: float) -> Iterable[Mapping[str, Any]]:
    page = 1
    total = 0
    # Estimate an upper bound for pages to avoid infinite loops
    start_ms = params.get("startTime")
    end_ms = params.get("endTime")
    interval_ms = _interval_to_ms(params.get("interval")) or 5 * 60 * 1000
    expected = 0
    if isinstance(start_ms, (int, float)) and isinstance(end_ms, (int, float)) and interval_ms > 0:
        expected = max(0, int((int(end_ms) - int(start_ms)) // interval_ms))
    max_pages = max(5, (math.ceil(expected / page_limit) if page_limit > 0 else 0) + 5)
    seen_ts: set = set()
    stalled_pages = 0
    while True:
        page_params = {**params, "page": page, "pageSize": page_limit, "limit": page_limit}
        resp = _http_get(base_url, path, page_params, headers, backoff_initial=backoff_initial, backoff_max=backoff_max)
        items = _extract_items(resp)
        if not items:
            if page == 1:
                try:
                    keys = ",".join(list(resp.keys())[:6]) if isinstance(resp, Mapping) else str(type(resp))
                except Exception:
                    keys = "<unavailable>"
                # Prepare a compact snippet of the raw response for debugging
                try:
                    raw_snippet = json.dumps(resp, ensure_ascii=False) if isinstance(resp, (dict, list)) else str(resp)
                except Exception:
                    raw_snippet = str(type(resp))
                max_len = 2000
                if len(raw_snippet) > max_len:
                    raw_snippet = raw_snippet[:max_len] + " … (truncated)"
                logging.getLogger("cryptostorm.retrieve").info(
                    "empty page on preferred call: path=%s keys=%s resp=%s",
                    path,
                    keys,
                    raw_snippet,
                )
            break
        new_in_page = 0
        for it in items:
            # Try to detect progress using timestamp keys
            ts_ms = _infer_ts_ms(it)
            if ts_ms is not None and ts_ms not in seen_ts:
                seen_ts.add(ts_ms)
                new_in_page += 1
            yield it
        total += len(items)
        LOG.debug("page %s: got %s items (new_ts=%s, total=%s)", page, len(items), new_in_page, total)
        if new_in_page == 0:
            stalled_pages += 1
        else:
            stalled_pages = 0
        if len(items) < page_limit:
            break
        if page >= max_pages:
            LOG.warning("stopping pagination: page=%s exceeds max_pages=%s (expected=%s, limit=%s)", page, max_pages, expected, page_limit)
            break
        if stalled_pages >= 2:
            LOG.warning("stopping pagination due to no progress (stalled_pages=%s) path=%s", stalled_pages, path)
            break
        page += 1


def _build_params(
    eff: EffectiveConfig,
    ds_key: str,
    symbol: str,
    *,
    interval: Optional[str],
    mode: Optional[str],
    exchange: str,
    quote: str,
    start_ms: int,
    end_ms: int,
    orderbook_time_enum: Optional[str] = None,
) -> Tuple[Dict[str, Any], bool]:
    # Return params and a flag whether this is aggregated-level query
    aggregated = (mode or "").lower() == "aggregated"
    params: Dict[str, Any] = {
        "interval": interval,
        "startTime": start_ms,
        "endTime": end_ms,
        # 'quote' is used in some v3 endpoints; v4 mapping removes it in _http_get
        "quote": quote,
    }
    if aggregated:
        # Some v4 aggregated endpoints require 'symbol' (coin code) rather than 'coin'.
        coin = eff.symbol_to_coin.get(symbol)
        if not coin:
            # naive fallback: strip quote suffix
            coin = symbol[:-len(quote)] if symbol.endswith(quote) else symbol
        # Prefer 'symbol' for aggregated endpoints (e.g., open-interest/aggregated-history)
        params.update({"symbol": coin})
        # Some aggregated endpoints (e.g., liquidation aggregated) require 'exchange_list'
        if ds_key == "liquidation_5m":
            params.update({"exchange_list": exchange})
    else:
        # Prefer canonical 'Binance' casing for exchange; v4 mapping enforces this
        exch = "Binance" if str(exchange).lower() == "binance" else exchange
        params.update({"symbol": symbol, "exchange": exch})
    # Orderbook: v4 uses 5m interval; do not use timeEnum
    if ds_key in {"orderbook_futures_5m", "orderbook_spot_5m"}:
        params["interval"] = "5m"
        params.pop("timeEnum", None)
    return params, aggregated


def _time_slices(start_ms: int, end_ms: int, slice_ms: int) -> Iterable[Tuple[int, int]]:
    cur = start_ms
    while cur < end_ms:
        nxt = min(end_ms, cur + slice_ms)
        yield cur, nxt
        cur = nxt


def _fetch_time_sliced(
    *,
    base_url: str,
    headers: Mapping[str, str],
    path: str,
    params: Dict[str, Any],
    start_ms: int,
    end_ms: int,
    slice_ms: int,
) -> List[Mapping[str, Any]]:
    items: List[Mapping[str, Any]] = []
    for s, e in _time_slices(start_ms, end_ms, slice_ms):
        local = {**params, "startTime": s, "endTime": e}
        resp = _http_get(base_url, path, local, headers, backoff_initial=1.0, backoff_max=8.0)
        got = _extract_items(resp)
        LOG.debug("slice %s..%s got %d", _ms_to_iso(s), _ms_to_iso(e), len(got))
        items.extend(got)
        # gentle pacing to respect rate limits
        time.sleep(0.3)
    return items


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


def _max_saved_ts(path: Path) -> Optional[int]:
    """Scan a JSONL file and return the maximum ts value found.

    Returns None if the file does not exist or contains no ts values.
    """
    if not path.exists():
        return None
    max_ts: Optional[int] = None
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                ts = obj.get("ts")
                if isinstance(ts, (int, float)):
                    v = int(ts)
                    max_ts = v if max_ts is None or v > max_ts else max_ts
    except Exception:
        return max_ts
    return max_ts


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
    slice_days: int = 0,
    force_v3: Optional[List[str]] = None,
    orderbook_time_enum: Optional[str] = None,
) -> None:
    # Resolve API key if not provided (robust fallback)
    if not api_key and not dry_run:
        # Try file from effective config
        try:
            if getattr(eff, "api_key_file", None):
                p = Path(eff.api_key_file)  # type: ignore[arg-type]
                if p.exists():
                    api_key = p.read_text(encoding="utf-8").strip() or None
        except Exception:
            api_key = None
        # Try environment
        if not api_key:
            api_key = os.getenv(eff.api_key_env)

    # Compute global time window
    end_ms = _utc_now_ms()
    base_start_ms = end_ms - eff.days * 24 * 60 * 60 * 1000

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
            # Build output path under data/<SYM>/ and compute delta start
            out_dir = out_root / sym
            _ensure_dir(out_dir)
            out_file = out_dir / _output_filename(ds_key)
            last_ts = _max_saved_ts(out_file)
            # Backfill mode: if time-slicing is enabled, ignore delta and fetch from base_start
            if slice_days and slice_days > 0:
                start_ms_local = base_start_ms
            else:
                start_ms_local = base_start_ms
                if isinstance(last_ts, int):
                    start_ms_local = max(base_start_ms, last_ts + 1)

            # Align the request window to the dataset interval grid
            step_ms = _canonical_interval_ms(ds_key, interval)
            if step_ms:
                start_ms_local = _ceil_to_step(start_ms_local, step_ms)
                end_ms_aligned = _floor_to_step(end_ms, step_ms)
            else:
                end_ms_aligned = end_ms

            if start_ms_local >= end_ms_aligned:
                LOG.info("up-to-date %s %s (no new range)", ds_key, sym)
                continue
            try:
                LOG.info(
                    "aligned window %s %s: %s .. %s (interval=%s, step_ms=%s)",
                    ds_key,
                    sym,
                    _ms_to_iso(start_ms_local),
                    _ms_to_iso(end_ms_aligned),
                    interval,
                    step_ms,
                )
            except Exception:
                pass
            params, aggregated = _build_params(
                eff,
                ds_key,
                target_value,
                interval=interval,
                mode=mode,
                exchange=exchange,
                quote=quote,
                start_ms=start_ms_local,
                end_ms=end_ms_aligned,
                orderbook_time_enum=orderbook_time_enum,
            )

            def fetch_all(path: str) -> List[Mapping[str, Any]]:
                if dry_run:
                    LOG.info("DRY-RUN: GET %s params=%s", path, params)
                    return []
                # Use time-slicing for v4 when configured; otherwise use paging
                if slice_days and path.startswith("/api/") and "open-interest/ohlc-history" not in path and "price/ohlc-history" not in path:
                    slice_ms = slice_days * 24 * 60 * 60 * 1000
                    # Align the slice edges to the interval grid as well to avoid off-grid boundaries
                    step_ms_local = _canonical_interval_ms(ds_key, interval) or slice_ms
                    s0 = _ceil_to_step(params["startTime"], step_ms_local)
                    e0 = _floor_to_step(params["endTime"], step_ms_local)
                    if s0 >= e0:
                        return []
                    return _fetch_time_sliced(
                        base_url=base_url,
                        headers=headers,
                        path=path,
                        params={**params, "startTime": s0, "endTime": e0},
                        start_ms=s0,
                        end_ms=e0,
                        slice_ms=slice_ms,
                    )
                else:
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
            # Allow forcing v3 for specific datasets
            used_path = preferred
            if force_v3 and ds_key in set(force_v3):
                if ds_key == "futures_ohlcv_5m":
                    used_path = "/api/price/ohlc-history"
                elif ds_key == "oi_5m_ohlc":
                    used_path = "/api/futures/openInterest/ohlc-history"
                elif ds_key == "liquidation_5m":
                    used_path = "/api/futures/liquidation/history"
            try:
                items = fetch_all(used_path)
                if not items and fallback:
                    # Log additional context for debugging
                    _params_preview = {
                        k: params.get(k)
                        for k in ("symbol", "coin", "exchange", "interval")
                        if params.get(k) is not None
                    }
                    _params_preview.update(
                        {
                            "start": _ms_to_iso(params.get("startTime")),
                            "end": _ms_to_iso(params.get("endTime")),
                        }
                    )
                    LOG.info(
                        "fallback to %s for %s %s (preferred returned 0 items) params=%s",
                        fallback,
                        ds_key,
                        sym,
                        _params_preview,
                    )
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
                # No timeEnum retries for orderbook on v4; rely on interval=5m and time window
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
