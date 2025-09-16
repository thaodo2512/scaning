#!/usr/bin/env python3
# binance_perp_usdt_dataset.py
#
# Purpose: Prepare a reviewable dataset (CSV) of USDⓈ-M USDT PERPETUAL symbols on Binance Futures.
# It collects:
#   - Symbol meta from /fapi/v1/exchangeInfo
#   - 24h stats from /fapi/v1/ticker/24hr (bulk)
#   - Last N daily klines per symbol from /fapi/v1/klines
# And computes standardized metrics for evaluators (vol30d_quote, avg_daily_quote, liq_mom, rv30d_ann, etc.).
#
# Usage:
#   pip install aiohttp
#   python scripts/binance_perp_usdt_dataset.py --days 30 --concurrency 8 --outfile dataset.csv
#
# Notes:
#   - Keep concurrency modest to avoid 429 bans. 6-12 is usually fine.
#   - 'rv30d_ann' is annualized realized vol of daily log returns: stdev(logret) * sqrt(365).
#   - 'liq_mom' = vol24h_quote / avg_daily_quote.
from __future__ import annotations

import asyncio
import aiohttp
import argparse
import csv
import datetime as dt
import math
import os
import random
import statistics
import sys


BASE = "https://fapi.binance.com"


def iso(ms: int | float | None) -> str:
    try:
        if ms is None:
            return ""
        return dt.datetime.utcfromtimestamp(float(ms) / 1000).strftime("%Y-%m-%d")
    except Exception:
        return ""


async def get_json(session: aiohttp.ClientSession, path: str, params: dict | None = None, retries: int = 5):
    url = BASE + path
    backoff = 1.0
    for i in range(retries):
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status in (418, 429):
                    await asyncio.sleep(backoff + random.random())
                    backoff = min(backoff * 2.0, 8.0)
                    continue
                r.raise_for_status()
                return await r.json()
        except Exception:
            if i == retries - 1:
                raise
            await asyncio.sleep(backoff + random.random())
            backoff = min(backoff * 2.0, 8.0)


async def fetch_klines_1d(session: aiohttp.ClientSession, symbol: str, limit: int):
    # /fapi/v1/klines?symbol=XXXUSDT&interval=1d&limit=N
    return await get_json(session, "/fapi/v1/klines", {"symbol": symbol, "interval": "1d", "limit": limit})


def extract_filters(sym_obj: dict):
    tickSize = stepSize = minQty = minNotional = ""
    for f in sym_obj.get("filters", []) or []:
        t = f.get("filterType")
        if t == "PRICE_FILTER":
            tickSize = f.get("tickSize", "")
        elif t == "LOT_SIZE":
            stepSize = f.get("stepSize", "")
            minQty = f.get("minQty", "")
        elif t == "MIN_NOTIONAL":
            minNotional = f.get("notional", f.get("minNotional", ""))
    return tickSize, stepSize, minQty, minNotional


def realized_vol_annualized(closes: list[float]) -> tuple[float, float]:
    if len(closes) < 3:
        return 0.0, 0.0
    rets: list[float] = []
    for a, b in zip(closes, closes[1:]):
        if a > 0 and b > 0:
            rets.append(math.log(b / a))
    if len(rets) < 2:
        return 0.0, 0.0
    stdev = statistics.pstdev(rets)
    return stdev, stdev * math.sqrt(365.0)


async def main() -> int:
    ap = argparse.ArgumentParser(description="Prepare dataset of USDⓈ-M USDT PERPETUAL pairs for evaluation")
    ap.add_argument("--days", type=int, default=30, help="Number of daily candles per symbol")
    ap.add_argument("--concurrency", type=int, default=8, help="Max concurrent requests for klines")
    ap.add_argument("--outfile", type=str, default="dataset_usdtm_perps.csv", help="Output CSV path")
    args = ap.parse_args()

    conn = aiohttp.TCPConnector(limit=max(2, int(args.concurrency)))
    async with aiohttp.ClientSession(connector=conn, headers={"Accept": "application/json"}) as session:
        # Universe
        exch = await get_json(session, "/fapi/v1/exchangeInfo")
        symbols_meta = [
            s
            for s in (exch.get("symbols") or [])
            if s.get("contractType") == "PERPETUAL"
            and s.get("status") == "TRADING"
            and s.get("quoteAsset") == "USDT"
        ]
        symbols_meta.sort(key=lambda x: x.get("symbol", ""))
        symbols = [s["symbol"] for s in symbols_meta]

        # 24h stats bulk
        tick24_all = await get_json(session, "/fapi/v1/ticker/24hr")
        tick24_map = {t.get("symbol"): t for t in (tick24_all or []) if isinstance(t, dict) and "symbol" in t}

        # Klines with semaphore
        sem = asyncio.Semaphore(int(args.concurrency))

        async def fetch_one(sym: str):
            async with sem:
                try:
                    kl = await fetch_klines_1d(session, sym, int(args.days))
                    return sym, kl
                except Exception:
                    return sym, None

        tasks = [asyncio.create_task(fetch_one(sym)) for sym in symbols]
        kl_results = dict(await asyncio.gather(*tasks))

        # Build rows
        rows: list[dict[str, object]] = []
        for s in symbols_meta:
            sym = s["symbol"]
            base = s.get("baseAsset")
            quote = s.get("quoteAsset")
            onboard_ms = s.get("onboardDate")
            pricePrecision = s.get("pricePrecision")
            quantityPrecision = s.get("quantityPrecision")
            tickSize, stepSize, minQty, minNotional = extract_filters(s)

            kl = kl_results.get(sym) or []
            # kline: [0] openTime, [1] open, [2] high, [3] low, [4] close, [5] volume,
            # [6] closeTime, [7] quote volume, [8] trades, ...
            qsum = bsum = 0.0
            tcnt = 0
            closes: list[float] = []
            for c in kl or []:
                try:
                    bsum += float(c[5])
                    qsum += float(c[7])
                    tcnt += int(c[8])
                    closes.append(float(c[4]))
                except Exception:
                    continue
            rv_daily, rv_ann = realized_vol_annualized(closes)
            days_used = len(closes)

            t24 = tick24_map.get(sym, {})
            try:
                vol24q = float(t24.get("quoteVolume", 0.0) or 0.0)
            except Exception:
                vol24q = 0.0
            try:
                count24 = int(t24.get("count", 0) or 0)
            except Exception:
                count24 = 0
            avg_daily_q = (qsum / days_used) if days_used else 0.0
            liq_mom = (vol24q / avg_daily_q) if avg_daily_q > 0 else 0.0

            rows.append(
                {
                    "symbol": sym,
                    "baseAsset": base,
                    "quoteAsset": quote,
                    "onboardDate": iso(onboard_ms) if onboard_ms else "",
                    "pricePrecision": pricePrecision,
                    "quantityPrecision": quantityPrecision,
                    "tickSize": tickSize,
                    "stepSize": stepSize,
                    "minQty": minQty,
                    "minNotional": minNotional,
                    "kline_days_used": days_used,
                    "vol30d_base": bsum,
                    "vol30d_quote": qsum,
                    "avg_daily_quote": avg_daily_q,
                    "vol24h_quote": vol24q,
                    "liq_mom": liq_mom,
                    "trades30d": tcnt,
                    "tradeCount24h": count24,
                    "rv30d_daily": rv_daily,
                    "rv30d_ann": rv_ann,
                }
            )

        # Write CSV
        fields = [
            "symbol",
            "baseAsset",
            "quoteAsset",
            "onboardDate",
            "pricePrecision",
            "quantityPrecision",
            "tickSize",
            "stepSize",
            "minQty",
            "minNotional",
            "kline_days_used",
            "vol30d_base",
            "vol30d_quote",
            "avg_daily_quote",
            "vol24h_quote",
            "liq_mom",
            "trades30d",
            "tradeCount24h",
            "rv30d_daily",
            "rv30d_ann",
        ]
        out = args.outfile
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for r in rows:
                w.writerow(r)

        print(f"Wrote {os.path.abspath(out)}")
        print("Columns: " + ", ".join(fields))
    return 0


if __name__ == "__main__":  # pragma: no cover
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)

