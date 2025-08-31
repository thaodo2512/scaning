#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
screener_full_debug.py — Standalone InterestScore screener with Dashboard TUI + debug.log
- Exchanges: Binance (spot/perp), OKX (spot/perp), Bybit (spot/perp)
- Metrics: Notional/min, Trades/min, Spread (bps) + vol, Depth ±0.5/±1% (optional), Perp OIΔ15m
- InterestScore (per-group z-scores):
    0.45·Z(Notl/min) + 0.35·Z(Trades/min) + 0.10·Z(Depth±0.5%) + 0.10·Z(Depth±1%)
  + (Perp: +0.10·Z(OIΔ15m)) − 0.20·Z(Spread_bps) − 0.10·Z(Spread_volatility)
- Debug panel in TUI + writes all logs to debug.log
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import signal
import sys
import time
import smtplib
from email.message import EmailMessage
import ssl
import contextlib
from collections import defaultdict, deque, OrderedDict
from dataclasses import dataclass, field
from statistics import mean, pstdev
from typing import Any, Deque, Dict, List, Optional, Tuple

import aiohttp

try:
    import websockets  # pip install websockets
except Exception:
    websockets = None

try:
    import curses  # on Windows: pip install windows-curses
except Exception:
    curses = None

# Optional pretty tables for non-TUI mode
try:
    from rich import box
    from rich.console import Console
    from rich.table import Table
    console = Console()
except Exception:
    box = None
    Console = None
    Table = None
    class _Dummy:
        def print(self, *a, **k): print(*a)
        def log(self, *a, **k): print(*a)
        def rule(self, *a, **k): print("-"*80, *a, "-"*80)
    console = _Dummy()

# --- Debug logger: ring buffer + file ---
DEBUG_LOG = deque(maxlen=300)
DEBUG_FILE_ENABLE = True
DEBUG_UI_ENABLE = True
try:
    DEBUG_FILE = os.path.join(os.path.dirname(__file__), "debug.log")
except Exception:
    DEBUG_FILE = "debug.log"

def log_debug(msg: str) -> None:
    """Append to ring buffer & persist to debug.log (best-effort)."""
    try:
        ts = time.strftime("%H:%M:%S", time.localtime())
    except Exception:
        ts = "??:??:??"
    line = f"[{ts}] {msg}"
    DEBUG_LOG.append(line)
    try:
        if DEBUG_FILE_ENABLE:
            with open(DEBUG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass

# -------------
# Utils & Data
# -------------

STABLES = {"USDT", "USDC", "BUSD", "TUSD", "FDUSD", "DAI", "PYUSD", "EUR", "TRY", "BRL"}

def now_ms() -> int:
    return int(time.time() * 1000)

def parse_window(arg: str) -> int:
    m = re.fullmatch(r"(\d+)([smhd])", arg)
    if not m:
        raise SystemExit(f"Invalid window '{arg}'. Use like 5m, 15m, 1h.")
    n, u = int(m.group(1)), m.group(2)
    if u == "s": return max(1, n // 60)
    if u == "m": return max(1, n)
    if u == "h": return n * 60
    if u == "d": return n * 60 * 24
    raise SystemExit(f"Invalid unit in window '{arg}'.")

@dataclass
class MinuteBucket:
    t_minute_ms: int
    trades: int = 0
    notional: float = 0.0  # quote

@dataclass
class SpreadPoint:
    t_ms: int
    bps: float

@dataclass
class OIPoint:
    t_ms: int
    value: float

@dataclass
class OrderBookSide:
    levels: Dict[float, float] = field(default_factory=dict)
    def update_to(self, price: float, size: float) -> None:
        if size <= 0:
            self.levels.pop(price, None)
        else:
            self.levels[price] = size
    def quote_depth_within_pct(self, mid: float, pct: float, is_bid: bool, max_levels: int = 200) -> float:
        if math.isnan(mid) or mid <= 0:
            return 0.0
        lower = mid * (1 - pct)
        upper = mid * (1 + pct)
        total = 0.0
        prices = sorted(self.levels.keys(), reverse=is_bid)
        for p in prices[:max_levels]:
            if (is_bid and p < lower) or ((not is_bid) and p > upper):
                break
            total += p * self.levels[p]
        return total

@dataclass
class OrderBookL2:
    bids: OrderBookSide = field(default_factory=OrderBookSide)
    asks: OrderBookSide = field(default_factory=OrderBookSide)
    def mid(self) -> float:
        try:
            best_bid = max(self.bids.levels.keys()) if self.bids.levels else float("nan")
            best_ask = min(self.asks.levels.keys()) if self.asks.levels else float("nan")
            if math.isnan(best_bid) or math.isnan(best_ask):
                return float("nan")
            return (best_bid + best_ask) / 2.0
        except ValueError:
            return float("nan")

@dataclass
class SymbolState:
    symbol: str
    base: str
    quote: str
    group_key: str  # e.g., binance_spot
    minute_buckets: "OrderedDict[int, MinuteBucket]" = field(default_factory=OrderedDict)
    spread_points: Deque[SpreadPoint] = field(default_factory=lambda: deque(maxlen=1200))
    last_bid: float = float("nan")
    last_ask: float = float("nan")
    book: Optional[OrderBookL2] = None
    oi_history: Deque[OIPoint] = field(default_factory=lambda: deque(maxlen=200))

    def add_trade(self, ts_ms: int, price: float, qty_base: float) -> None:
        minute = (ts_ms // 60000) * 60000
        b = self.minute_buckets.get(minute)
        if b is None:
            b = MinuteBucket(t_minute_ms=minute)
            self.minute_buckets[minute] = b
        b.trades += 1
        q = price * qty_base
        if not math.isnan(q) and math.isfinite(q):
            b.notional += q

    def add_spread(self, ts_ms: int, bid: float, ask: float) -> None:
        self.last_bid, self.last_ask = bid, ask
        mid = (bid + ask) / 2.0 if bid and ask and bid > 0 and ask > 0 else float("nan")
        if mid and mid > 0:
            bps = (ask - bid) / mid * 1e4
            self.spread_points.append(SpreadPoint(ts_ms, bps))

    def add_oi(self, ts_ms: int, value: float) -> None:
        self.oi_history.append(OIPoint(ts_ms, value))

    def trim_to_window(self, window_mins: int) -> None:
        cutoff = now_ms() - window_mins * 60_000
        keys = list(self.minute_buckets.keys())
        for k in keys:
            if k < cutoff:
                self.minute_buckets.pop(k, None)
        while self.spread_points and self.spread_points[0].t_ms < cutoff:
            self.spread_points.popleft()
        while self.oi_history and self.oi_history[0].t_ms < cutoff - 15 * 60_000:
            self.oi_history.popleft()

    def notional_per_min(self, window_mins: int) -> float:
        if not self.minute_buckets:
            return 0.0
        total_notional = 0.0
        mins_counted = 0
        cutoff = now_ms() - window_mins * 60_000
        for k, b in self.minute_buckets.items():
            if k >= cutoff:
                total_notional += b.notional
                mins_counted += 1
        return total_notional / max(1, mins_counted)

    def trades_per_min(self, window_mins: int) -> float:
        if not self.minute_buckets:
            return 0.0
        total_trades = 0
        mins_counted = 0
        cutoff = now_ms() - window_mins * 60_000
        for k, b in self.minute_buckets.items():
            if k >= cutoff:
                total_trades += b.trades
                mins_counted += 1
        return total_trades / max(1, mins_counted)

    def latest_spread_bps(self) -> float:
        return self.spread_points[-1].bps if self.spread_points else float("nan")

    def spread_volatility(self) -> float:
        if len(self.spread_points) < 5:
            return float("nan")
        vals = [p.bps for p in self.spread_points]
        mu = mean(vals)
        return math.sqrt(sum((x - mu) ** 2 for x in vals) / len(vals))

    def depth_quote_within(self, pct: float) -> Optional[float]:
        if self.book is None:
            return None
        mid = self.book.mid()
        if math.isnan(mid) or mid <= 0:
            return None
        bid_q = self.book.bids.quote_depth_within_pct(mid, pct, is_bid=True)
        ask_q = self.book.asks.quote_depth_within_pct(mid, pct, is_bid=False)
        return min(bid_q, ask_q)

    def oi_delta_pct_15m(self) -> Optional[float]:
        if len(self.oi_history) < 2:
            return None
        latest = self.oi_history[-1]
        tgt = latest.t_ms - 15 * 60_000
        earlier = None
        for p in reversed(self.oi_history):
            if p.t_ms <= tgt:
                earlier = p
                break
        if earlier is None:
            earlier = self.oi_history[0]
        if earlier.value <= 0:
            return None
        return (latest.value - earlier.value) / earlier.value * 100.0

# --------- Z-scores ---------

class ZScorer:
    def __init__(self) -> None:
        self.samples: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))

    def add_sample(self, group: str, name: str, value: Optional[float]) -> None:
        if value is None or math.isnan(value) or not math.isfinite(value):
            return
        self.samples[group][name].append(float(value))

    def z(self, group: str, name: str, value: Optional[float]) -> float:
        if value is None or math.isnan(value) or not math.isfinite(value):
            return 0.0
        arr = self.samples.get(group, {}).get(name, [])
        if len(arr) < 5:
            return 0.0
        mu = mean(arr)
        sigma = pstdev(arr) or 1.0
        return (float(value) - mu) / sigma

# --------- Exchange base ---------

class ExchangeAdapter:
    name: str
    market: str  # 'spot' or 'perp'
    def __init__(self, session: aiohttp.ClientSession, args) -> None:
        self.session = session
        self.args = args
        self.symbols: List[Tuple[str, str, str]] = []  # (symbol, base, quote)
        self.states: Dict[str, SymbolState] = {}
        self.ws = None
        self.l2_enabled = bool(getattr(args, "with_l2", False))
        self._bg_tasks: List[asyncio.Task] = []

    async def discover_symbols(self) -> None:
        raise NotImplementedError

    def make_state(self, symbol: str, base: str, quote: str) -> SymbolState:
        group_key = f"{self.name}_{self.market}"
        st = SymbolState(symbol=symbol, base=base, quote=quote, group_key=group_key)
        if self.l2_enabled:
            st.book = OrderBookL2()
        self.states[symbol] = st
        return st

    async def run(self) -> None:
        raise NotImplementedError

    def filtered(self, base: str, quote: str) -> bool:
        quotes = getattr(self.args, "quotes", None)
        if quotes and quote not in quotes:
            return False
        if getattr(self.args, "exclude_stable_base", False) and base.upper() in STABLES:
            return False
        return True

    def add_bg_task(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._bg_tasks.append(t)
        return t

    async def cancel_bg_tasks(self) -> None:
        for t in self._bg_tasks:
            t.cancel()
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        self._bg_tasks.clear()

# --------- Binance ---------

class BinanceSpot(ExchangeAdapter):
    name = "binance"; market = "spot"
    async def discover_symbols(self) -> None:
        url = "https://api.binance.com/api/v3/exchangeInfo"
        async with self.session.get(url, timeout=20) as r:
            j = await r.json()
        out = []
        for s in j.get("symbols", []):
            if s.get("status") != "TRADING":
                continue
            base, quote = s["baseAsset"], s["quoteAsset"]
            if not self.filtered(base, quote):
                continue
            sym = s["symbol"].lower()
            out.append((sym, base, quote))
        self.symbols = out
        for sym, base, quote in self.symbols:
            self.make_state(sym, base, quote)
        log_debug(f"[binance spot] discovered {len(self.symbols)} symbols")

    async def run(self) -> None:
        if websockets is None:
            log_debug("websockets module not available")
            return
        if not self.symbols:
            await self.discover_symbols()
        params = []
        limit = getattr(self.args, "max_syms_per_group", 200)
        for sym, *_ in self.symbols[:limit]:
            params += [f"{sym}@aggTrade", f"{sym}@bookTicker"]
            if self.l2_enabled:
                params.append(f"{sym}@depth@100ms")
        url = "wss://stream.binance.com:9443/ws"
        while True:
            try:
                async with websockets.connect(url, ping_interval=15, ping_timeout=20) as ws:
                    self.ws = ws
                    for i in range(0, len(params), 200):
                        sub = {"method": "SUBSCRIBE", "params": params[i:i+200], "id": i//200 + 1}
                        await ws.send(json.dumps(sub))
                        await asyncio.sleep(0.2)
                    async for raw in ws:
                        d = json.loads(raw)
                        data = d.get("data", d)
                        await self._on_msg(data)
            except Exception as e:
                log_debug(f"[binance spot] ws error: {e}")
                await asyncio.sleep(2)

    async def _on_msg(self, d: Dict[str, Any]) -> None:
        ev = d.get("e")
        if ev == "aggTrade":
            sym = d["s"].lower()
            st = self.states.get(sym)
            if not st:
                return
            price = float(d["p"])
            qty = float(d["q"])
            ts = int(d["T"])
            st.add_trade(ts, price, qty)
        elif ev in ("24hrTicker", "bookTicker") or ("u" in d and "b" in d and "a" in d and d.get("s")):
            sym = d.get("s", "").lower()
            if not sym:
                return
            st = self.states.get(sym)
            if not st:
                return
            bid = float(d.get("b", d.get("b1", 0)) or 0)
            ask = float(d.get("a", d.get("a1", 0)) or 0)
            ts = int(d.get("E", now_ms()))
            if bid and ask:
                st.add_spread(ts, bid, ask)
        elif ev == "depthUpdate" and self.l2_enabled:
            sym = d["s"].lower()
            st = self.states.get(sym)
            if not st or st.book is None:
                return
            for p, q in d.get("b", []):
                st.book.bids.update_to(float(p), float(q))
            for p, q in d.get("a", []):
                st.book.asks.update_to(float(p), float(q))

class BinancePerp(BinanceSpot):
    name = "binance"; market = "perp"
    def __init__(self, session, args) -> None:
        super().__init__(session, args)
        self._oi_started = False

    async def discover_symbols(self) -> None:
        url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
        async with self.session.get(url, timeout=20) as r:
            j = await r.json()
        out = []
        for s in j.get("symbols", []):
            if s.get("status") != "TRADING":
                continue
            base, quote = s["baseAsset"], s["quoteAsset"]
            if not self.filtered(base, quote):
                continue
            sym = s["symbol"].lower()
            out.append((sym, base, quote))
        self.symbols = out
        for sym, base, quote in self.symbols:
            self.make_state(sym, base, quote)
        log_debug(f"[binance perp] discovered {len(self.symbols)} symbols")

    async def run(self) -> None:
        if websockets is None:
            log_debug("websockets module not available")
            return
        if not self.symbols:
            await self.discover_symbols()
        if not self._oi_started:
            self._oi_started = True
            self.add_bg_task(self._poll_oi_loop())
        params = []
        limit = getattr(self.args, "max_syms_per_group", 200)
        for sym, *_ in self.symbols[:limit]:
            params += [f"{sym}@aggTrade", f"{sym}@bookTicker"]
            if self.l2_enabled:
                params.append(f"{sym}@depth@100ms")
        url = "wss://fstream.binance.com/ws"
        while True:
            try:
                async with websockets.connect(url, ping_interval=15, ping_timeout=20) as ws:
                    self.ws = ws
                    for i in range(0, len(params), 200):
                        sub = {"method": "SUBSCRIBE", "params": params[i:i+200], "id": i//200 + 1}
                        await ws.send(json.dumps(sub))
                        await asyncio.sleep(0.2)
                    async for raw in ws:
                        d = json.loads(raw)
                        data = d.get("data", d)
                        await self._on_msg(data)
            except Exception as e:
                log_debug(f"[binance perp] ws error: {e}")
                await asyncio.sleep(2)

    async def _poll_oi_loop(self) -> None:
        url = "https://fapi.binance.com/fapi/v1/openInterest"
        while True:
            try:
                if self.session.closed:
                    break
                ts = now_ms()
                for sym, *_ in self.symbols[:200]:
                    params = {"symbol": sym.upper()}
                    async with self.session.get(url, params=params, timeout=10) as r:
                        j = await r.json()
                    oi_val = float(j.get("openInterest", 0.0))
                    st = self.states.get(sym)
                    if st:
                        st.add_oi(ts, oi_val)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log_debug(f"[binance perp] OI poll error: {e}")
            await asyncio.sleep(60)

# --------- Bybit ---------

class BybitBase(ExchangeAdapter):
    ws_public_url: str
    category: str  # 'spot' or 'linear'

    async def discover_symbols(self) -> None:
        url = "https://api.bybit.com/v5/market/instruments-info"
        params = {"category": self.category}
        async with self.session.get(url, params=params, timeout=20) as r:
            j = await r.json()
        out = []
        for it in j.get("result", {}).get("list", []):
            sym = it.get("symbol")
            if not sym:
                continue
            base = it.get("baseCoin") or sym.replace("USDT", "")
            quote = it.get("quoteCoin") or "USDT"
            if not self.filtered(base, quote):
                continue
            out.append((sym, base, quote))
        # Only USDT pairs
        self.symbols = [(s, b, q) for (s, b, q) in out if s.endswith("USDT")]
        for sym, base, quote in self.symbols:
            self.make_state(sym, base, quote)
        log_debug(f"[bybit {self.category}] discovered {len(self.symbols)} symbols")

    async def run(self) -> None:
        if websockets is None:
            log_debug("websockets module not available")
            return
        if not self.symbols:
            await self.discover_symbols()
        subs = []
        limit = getattr(self.args, "max_syms_per_group", 200)
        for sym, *_ in self.symbols[:limit]:
            subs.append({"op": "subscribe", "args": [f"publicTrade.{sym}"]})
            subs.append({"op": "subscribe", "args": [f"tickers.{sym}"]})
            if self.l2_enabled:
                subs.append({"op": "subscribe", "args": [f"orderbook.50.{sym}"]})
        while True:
            try:
                async with websockets.connect(self.ws_public_url, ping_interval=15, ping_timeout=20) as ws:
                    self.ws = ws
                    for i, sub in enumerate(subs):
                        await ws.send(json.dumps(sub))
                        if i % 30 == 29:
                            await asyncio.sleep(0.3)
                    async for raw in ws:
                        d = json.loads(raw)
                        await self._on_msg(d)
            except Exception as e:
                log_debug(f"[bybit {self.category}] ws error: {e}")
                await asyncio.sleep(2)

    async def _on_msg(self, d: Dict[str, Any]) -> None:
        topic = d.get("topic")
        if not topic:
            log_debug("bybit: missing topic")
            return
        msg_ts = int(d.get("ts", now_ms()))
        if topic.startswith("publicTrade."):
            sym = topic.split(".")[1]
            st = self.states.get(sym)
            if not st:
                log_debug(f"bybit: state missing for {sym}")
                return
            for t in d.get("data", []):
                try:
                    price = float(t.get("p") or t.get("price") or 0)
                    qty = float(t.get("v") or t.get("size") or 0)
                    t_ts = int(t.get("T") or msg_ts)
                    if price > 0 and qty > 0:
                        st.add_trade(t_ts, price, qty)
                except Exception as e:
                    log_debug(f"bybit trade parse err: {e}")
                    continue
        elif topic.startswith("tickers."):
            sym = topic.split(".")[1]
            st = self.states.get(sym)
            if not st:
                log_debug(f"bybit: state missing for {sym}")
                return
            data = d.get("data", {})
            bid = data.get("bid1Price") or data.get("bestBidPrice") or data.get("bidPrice") or 0
            ask = data.get("ask1Price") or data.get("bestAskPrice") or data.get("askPrice") or 0
            try:
                bid = float(bid or 0)
                ask = float(ask or 0)
                if bid and ask:
                    st.add_spread(int(data.get("ts") or msg_ts), bid, ask)
            except Exception as e:
                log_debug(f"bybit ticker parse err: {e}")
        elif self.l2_enabled and topic.startswith("orderbook.50."):
            sym = topic.split(".")[2]
            st = self.states.get(sym)
            if not st or st.book is None:
                if not st:
                    log_debug(f"bybit L2: state missing for {sym}")
                return
            data = d.get("data", {})
            try:
                if d.get("type") == "snapshot":
                    st.book = OrderBookL2()
                for p, q in data.get("b", []):
                    st.book.bids.update_to(float(p), float(q))
                for p, q in data.get("a", []):
                    st.book.asks.update_to(float(p), float(q))
            except Exception as e:
                log_debug(f"bybit L2 parse err: {e}")

class BybitSpot(BybitBase):
    name = "bybit"; market = "spot"; ws_public_url = "wss://stream.bybit.com/v5/public/spot"; category = "spot"

class BybitPerp(BybitBase):
    name = "bybit"; market = "perp"; ws_public_url = "wss://stream.bybit.com/v5/public/linear"; category = "linear"
    def __init__(self, session, args) -> None:
        super().__init__(session, args)
        self._oi_started = False

    async def discover_symbols(self) -> None:
        await super().discover_symbols()
        if not self._oi_started:
            self._oi_started = True
            self.add_bg_task(self._poll_oi_loop())

    async def _poll_oi_loop(self) -> None:
        url = "https://api.bybit.com/v5/market/open-interest"
        while True:
            try:
                if self.session.closed:
                    break
                ts = now_ms()
                for sym, *_ in self.symbols[:200]:
                    params = {"category": "linear", "symbol": sym, "intervalTime": "5min", "limit": 1}
                    async with self.session.get(url, params=params, timeout=10) as r:
                        j = await r.json()
                    lst = j.get("result", {}).get("list", [])
                    if lst:
                        cur_oi = float(lst[-1].get("openInterest", 0))
                        st = self.states.get(sym)
                        if st:
                            st.add_oi(ts, cur_oi)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log_debug(f"[bybit perp] OI poll error: {e}")
            await asyncio.sleep(60)

# --------- OKX ---------

class OKXSpot(ExchangeAdapter):
    name = "okx"; market = "spot"
    async def discover_symbols(self) -> None:
        url = "https://www.okx.com/api/v5/public/instruments"
        params = {"instType": "SPOT"}
        async with self.session.get(url, params=params, timeout=20) as r:
            j = await r.json()
        out = []
        for it in j.get("data", []):
            instId = it.get("instId")
            base = it.get("baseCcy")
            quote = it.get("quoteCcy")
            if not instId or not base or not quote:
                continue
            if quote != "USDT":
                continue
            if not self.filtered(base, quote):
                continue
            out.append((instId, base, quote))
        self.symbols = out
        for sym, base, quote in self.symbols:
            self.make_state(sym, base, quote)
        log_debug(f"[okx spot] discovered {len(self.symbols)} symbols")

    async def run(self) -> None:
        if websockets is None:
            log_debug("websockets module not available")
            return
        if not self.symbols:
            await self.discover_symbols()
        url = "wss://ws.okx.com:8443/ws/v5/public"
        args_list = []
        limit = getattr(self.args, "max_syms_per_group", 200)
        for sym, *_ in self.symbols[:limit]:
            args_list.append({"channel": "trades", "instId": sym})
            args_list.append({"channel": "tickers", "instId": sym})
            if self.l2_enabled:
                args_list.append({"channel": "books5", "instId": sym})
        while True:
            try:
                async with websockets.connect(url, ping_interval=15, ping_timeout=20) as ws:
                    self.ws = ws
                    for i in range(0, len(args_list), 50):
                        sub = {"op": "subscribe", "args": args_list[i:i+50]}
                        await ws.send(json.dumps(sub))
                        await asyncio.sleep(0.3)
                    async for raw in ws:
                        d = json.loads(raw)
                        await self._on_msg(d)
            except Exception as e:
                log_debug(f"[okx spot] ws error: {e}")
                await asyncio.sleep(2)

    async def _on_msg(self, d: Dict[str, Any]) -> None:
        if "arg" not in d:
            return
        arg = d["arg"]
        channel = arg.get("channel")
        instId = arg.get("instId")
        st = self.states.get(instId)
        if not st:
            return
        if channel == "trades":
            for t in d.get("data", []):
                try:
                    price = float(t.get("px", 0))
                    qty = float(t.get("sz", 0))
                    ts = int(t.get("ts", now_ms()))
                    if price > 0 and qty > 0:
                        st.add_trade(ts, price, qty)
                except Exception:
                    pass
        elif channel == "tickers":
            for t in d.get("data", []):
                try:
                    bid = float(t.get("bidPx", 0))
                    ask = float(t.get("askPx", 0))
                    ts = int(t.get("ts", now_ms()))
                    if bid and ask:
                        st.add_spread(ts, bid, ask)
                except Exception:
                    pass
        elif self.l2_enabled and channel == "books5":
            for t in d.get("data", []):
                if st.book is None:
                    continue
                try:
                    st.book = OrderBookL2()
                    for p, q, *_ in t.get("bids", []):
                        st.book.bids.update_to(float(p), float(q))
                    for p, q, *_ in t.get("asks", []):
                        st.book.asks.update_to(float(p), float(q))
                except Exception:
                    pass

class OKXPerp(OKXSpot):
    name = "okx"; market = "perp"
    async def discover_symbols(self) -> None:
        url = "https://www.okx.com/api/v5/public/instruments"
        params = {"instType": "SWAP"}
        async with self.session.get(url, params=params, timeout=20) as r:
            j = await r.json()
        out = []
        for it in j.get("data", []):
            instId = it.get("instId")
            if not instId or "USDT" not in instId:
                continue
            base_underlying = instId.split("-")[0]
            quote = "USDT"
            if not self.filtered(base_underlying, quote):
                continue
            out.append((instId, base_underlying, quote))
        self.symbols = out
        for sym, base, quote in self.symbols:
            self.make_state(sym, base, quote)
        log_debug(f"[okx perp] discovered {len(self.symbols)} symbols")

# --------- Score & Filters ---------

@dataclass
class ScoreRow:
    symbol: str
    exch: str
    market: str
    base: str
    quote: str
    notl_per_min: float
    trades_per_min: float
    spread_bps: float
    spread_vol: float
    depth_0_5: Optional[float]
    depth_1_0: Optional[float]
    oi_delta_pct_15m: Optional[float]
    score: float

def fmt_bps(x: float) -> str:
    return f"{x:.2f}" if math.isfinite(x) else "-"

def fmt_quote(x: Optional[float]) -> str:
    return f"{x:,.0f}" if x is not None and math.isfinite(x) else "-"

def compute_scores(adapters: List[ExchangeAdapter], window_mins: int) -> List[ScoreRow]:
    z = ZScorer()
    rows: List[ScoreRow] = []
    raw: List[Tuple[SymbolState, Dict[str, Optional[float]]]] = []
    for ad in adapters:
        for st in ad.states.values():
            st.trim_to_window(window_mins)
            m = {
                "notl_min": st.notional_per_min(window_mins),
                "trd_min": st.trades_per_min(window_mins),
                "spr_bps": st.latest_spread_bps(),
                "spr_vol": st.spread_volatility(),
                "d_0_5": st.depth_quote_within(0.005) if st.book else None,
                "d_1_0": st.depth_quote_within(0.01) if st.book else None,
                "oi15": st.oi_delta_pct_15m() if ad.market == "perp" else None,
            }
            raw.append((st, m))
            g = st.group_key
            z.add_sample(g, "notl_min", m["notl_min"])
            z.add_sample(g, "trd_min", m["trd_min"])
            z.add_sample(g, "spr_bps", m["spr_bps"])
            z.add_sample(g, "spr_vol", m["spr_vol"])
            z.add_sample(g, "d_0_5", m["d_0_5"])
            z.add_sample(g, "d_1_0", m["d_1_0"])
            if ad.market == "perp":
                z.add_sample(g, "oi15", m["oi15"])
    for st, m in raw:
        g = st.group_key
        z_notl = z.z(g, "notl_min", m["notl_min"])
        z_trds = z.z(g, "trd_min", m["trd_min"])
        z_d05  = z.z(g, "d_0_5", m["d_0_5"])
        z_d10  = z.z(g, "d_1_0", m["d_1_0"])
        z_oi   = z.z(g, "oi15", m["oi15"]) if m["oi15"] is not None else 0.0
        z_sp   = z.z(g, "spr_bps", m["spr_bps"])
        z_spv  = z.z(g, "spr_vol", m["spr_vol"])
        score  = (0.45*z_notl + 0.35*z_trds + 0.10*z_d05 + 0.10*z_d10 + 0.10*z_oi - 0.20*z_sp - 0.10*z_spv)
        rows.append(ScoreRow(
            symbol=st.symbol.upper(), exch=g.split("_")[0], market=g.split("_")[1],
            base=st.base, quote=st.quote,
            notl_per_min=m["notl_min"] or 0.0,
            trades_per_min=m["trd_min"] or 0.0,
            spread_bps=m["spr_bps"] if m["spr_bps"] is not None else float("nan"),
            spread_vol=m["spr_vol"] if m["spr_vol"] is not None else float("nan"),
            depth_0_5=m["d_0_5"],
            depth_1_0=m["d_1_0"],
            oi_delta_pct_15m=m["oi15"],
            score=score,
        ))
    return rows

def mark_passing_filters(rows: List[ScoreRow], order_size_quote: float, market: str) -> None:
    need05 = order_size_quote * 50
    need10 = order_size_quote * 200
    for r in rows:
        passes = True
        if r.notl_per_min < 20 * order_size_quote:
            passes = False
        if r.base in STABLES:
            passes = False
        if r.base in {"BTC", "ETH"}:
            if r.trades_per_min < 50:
                passes = False
            if math.isfinite(r.spread_bps) and r.spread_bps > 3.0:
                passes = False
        else:
            if r.trades_per_min < 100:
                passes = False
            if math.isfinite(r.spread_bps) and r.spread_bps > 8.0:
                passes = False
        if r.depth_0_5 is not None and r.depth_0_5 < need05:
            passes = False
        if r.depth_1_0 is not None and r.depth_1_0 < need10:
            passes = False
        if market == "perp" and r.oi_delta_pct_15m is not None and r.oi_delta_pct_15m < 0.5:
            passes = False
        setattr(r, "passes", passes)

def apply_fast_filters(rows: List[ScoreRow], order_size_quote: float, market: str) -> List[ScoreRow]:
    out = []
    for r in rows:
        mark_passing_filters([r], order_size_quote, market)
        if getattr(r, "passes", False):
            out.append(r)
    return out

def print_tables(rows: List[ScoreRow], top_n: int) -> None:
    if Table is None:
        groups: Dict[Tuple[str, str], List[ScoreRow]] = defaultdict(list)
        for r in rows:
            groups[(r.exch, r.market)].append(r)
        for (exch, market), lst in sorted(groups.items()):
            lst.sort(key=lambda x: x.score, reverse=True)
            print(f"\n== Top {top_n} — {exch.upper()} {market.upper()} ==")
            for i, r in enumerate(lst[:top_n], 1):
                print(f"{i:>2}. {r.symbol:>12} notl/min={r.notl_per_min:,.0f} trades/min={r.trades_per_min:,.0f} spread={fmt_bps(r.spread_bps)} score={r.score:+.2f}")
        return

    groups: Dict[Tuple[str, str], List[ScoreRow]] = defaultdict(list)
    for r in rows:
        groups[(r.exch, r.market)].append(r)
    for (exch, market), lst in sorted(groups.items()):
        lst.sort(key=lambda x: x.score, reverse=True)
        tbl = Table(title=f"Top {top_n} — {exch.upper()} {market.upper()} (InterestScore)", box=box.SIMPLE_HEAVY)
        tbl.add_column("#", justify="right")
        tbl.add_column("Symbol")
        tbl.add_column("Notl/min", justify="right")
        tbl.add_column("Trades/min", justify="right")
        tbl.add_column("Spread(bps)", justify="right")
        tbl.add_column("Spread σ", justify="right")
        tbl.add_column("Depth±0.5%", justify="right")
        tbl.add_column("Depth±1%", justify="right")
        if market == "perp":
            tbl.add_column("OI Δ15m%", justify="right")
        tbl.add_column("Score", justify="right")
        for i, r in enumerate(lst[:top_n], 1):
            row = [
                str(i), r.symbol,
                f"{r.notl_per_min:,.0f}", f"{r.trades_per_min:,.0f}",
                fmt_bps(r.spread_bps), fmt_bps(r.spread_vol),
                fmt_quote(r.depth_0_5), fmt_quote(r.depth_1_0),
            ]
            if market == "perp":
                row.append(f"{r.oi_delta_pct_15m:.2f}" if r.oi_delta_pct_15m is not None else "-")
            row.append(f"{r.score:+.2f}")
            tbl.add_row(*row)
        console.print(tbl)

# --------- TUI helpers ---------

SORT_KEYS = [
    ("score",      lambda r: r.score,          False),
    ("notional",   lambda r: r.notl_per_min,   False),
    ("trades",     lambda r: r.trades_per_min, False),
    ("spread",     lambda r: (r.spread_bps if math.isfinite(r.spread_bps) else 1e9), True),
    ("depth0.5",   lambda r: (r.depth_0_5 or -1), False),
    ("depth1.0",   lambda r: (r.depth_1_0 or -1), False),
    ("oiΔ15m",     lambda r: (r.oi_delta_pct_15m if r.oi_delta_pct_15m is not None else -1), False),
]

def _safe_addnstr(stdscr, y: int, x: int, text: str, width: int, attr: int = 0) -> None:
    maxw = max(1, width - 1)
    line = text.ljust(maxw)[:maxw]
    if y >= 0 and x >= 0:
        try:
            stdscr.addnstr(y, x, line, maxw, attr)
        except Exception:
            pass

def _init_colors():
    colors = {"header": 0, "sub": 0, "footer": 0, "pos": 0, "neg": 0, "norm": 0, "warn": 0}
    if curses and curses.has_colors():
        curses.start_color()
        curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(2, curses.COLOR_CYAN, curses.COLOR_BLACK)
        curses.init_pair(3, curses.COLOR_BLACK, curses.COLOR_WHITE)
        curses.init_pair(4, curses.COLOR_GREEN, curses.COLOR_BLACK)
        curses.init_pair(5, curses.COLOR_RED, curses.COLOR_BLACK)
        curses.init_pair(6, curses.COLOR_YELLOW, curses.COLOR_BLACK)
        colors.update({
            "header": curses.color_pair(1) | curses.A_BOLD,
            "sub": curses.color_pair(2),
            "footer": curses.color_pair(3),
            "pos": curses.color_pair(4),
            "neg": curses.color_pair(5),
            "norm": curses.A_NORMAL,
            "warn": curses.color_pair(6),
        })
    return colors

def _visible_columns(width: int, market: str) -> List[str]:
    cols = ["Symbol", "Exch", "Notl/min", "Trades/min", "Spread", "Score"]
    if width >= 72:
        cols.insert(5, "σ")
    if width >= 92:
        cols.insert(5, "Depth±0.5%")
    if width >= 110:
        cols.insert(6, "Depth±1%")
    if market == "perp" and width >= 126:
        cols.insert(-1, "OIΔ15m%")
    return cols

def _cell(value: str, width: int) -> str:
    if len(value) > width:
        return value[:max(0, width-1)] + "…"
    return value.ljust(width)

def _row_color(colors, r: ScoreRow) -> int:
    if r.score >= 1.0:
        return colors.get("pos", 0)
    if r.score <= -1.0:
        return colors.get("neg", 0)
    return colors.get("norm", 0)

# --------- TUI: Dashboard ---------

async def run_dashboard_ui(adapters: List[ExchangeAdapter], args) -> None:
    if curses is None:
        console.print("[red]curses not available; run with --no-tui or install windows-curses[/red]")
        return
    window_mins = parse_window(args.window)
    groups = sorted({(ad.name, ad.market) for ad in adapters})

    def _loop(stdscr):
        curses.curs_set(0)
        stdscr.nodelay(True)
        colors = _init_colors()
        filter_text = ""
        sort_idx = 0
        sort_reverse = not SORT_KEYS[0][2]
        dash_scroll = 0

        def _handle_input() -> bool:
            nonlocal filter_text, sort_idx, sort_reverse, dash_scroll
            try:
                ch = stdscr.getch()
            except Exception:
                ch = -1
            if ch == -1:
                return True
            if ch in (ord('q'), ord('Q')):
                return False
            elif ch == ord('s'):
                sort_idx = (sort_idx + 1) % len(SORT_KEYS)
            elif ch == ord('S'):
                sort_reverse = not sort_reverse
            elif ch == ord('r'):
                filter_text = ""
            elif ch == ord('/'):
                curses.echo()
                curses.curs_set(1)
                h, w = stdscr.getmaxyx()
                _safe_addnstr(stdscr, h-1, 0, "Filter: ", w, 0)
                stdscr.clrtoeol()
                try:
                    filter_text = stdscr.getstr(h-1, 8, 40).decode(errors='ignore')
                except Exception:
                    filter_text = ""
                curses.noecho()
                curses.curs_set(0)
                dash_scroll = 0
            elif ch in (curses.KEY_UP, ord('k')):
                dash_scroll = max(0, dash_scroll - 1)
            elif ch in (curses.KEY_DOWN, ord('j')):
                dash_scroll += 1
            elif ch == curses.KEY_PPAGE:
                dash_scroll = max(0, dash_scroll - (curses.LINES // 2))
            elif ch == curses.KEY_NPAGE:
                dash_scroll += (curses.LINES // 2)
            elif ch in (ord('g'),):
                dash_scroll = 0
            elif ch in (ord('G'),):
                dash_scroll = 10**9
            return True

        def _adapter_stats() -> List[Tuple[str, int, int, int]]:
            stats = []
            try:
                cutoff = now_ms() - 120_000
                for ad in adapters:
                    name = f"{ad.name} {ad.market}"
                    total = len(getattr(ad, "states", {}))
                    active = 0
                    spread_ok = 0
                    for st in getattr(ad, "states", {}).values():
                        if getattr(st, "minute_buckets", None):
                            if st.minute_buckets:
                                latest_min = next(reversed(st.minute_buckets))
                                if latest_min >= cutoff:
                                    active += 1
                        if getattr(st, "spread_points", None):
                            if st.spread_points and st.spread_points[-1].t_ms >= cutoff:
                                spread_ok += 1
                    stats.append((name, total, active, spread_ok))
            except Exception as e:
                log_debug(f"stats error: {e}")
                return []
            return stats

        def _render_debug_panel(stdscr):
            h, w = stdscr.getmaxyx()
            lines = list(DEBUG_LOG)[-5:]
            stats = _adapter_stats()
            panel_height = min(7 + len(stats), max(6, h // 3))
            y0 = max(2, h - panel_height - 1)
            header = " Debug — last errors & adapter health "
            _safe_addnstr(stdscr, y0, 0, header, w, colors.get('footer', 0))
            y = y0 + 1
            _safe_addnstr(stdscr, y, 0, "Group".ljust(22) + "symbols  active(2m)  spread(2m)", w, curses.A_BOLD)
            y += 1
            for (name, total, active, spread_ok) in stats:
                row = f"{name:<22}{total:>6}  {active:>10}  {spread_ok:>11}"
                _safe_addnstr(stdscr, y, 0, row, w, 0)
                y += 1
            _safe_addnstr(stdscr, y, 0, "-" * min(w-1, 80), w, 0)
            y += 1
            for ln in lines:
                _safe_addnstr(stdscr, y, 0, ln, w, 0)
                y += 1
            groups_present = {(ad.name, ad.market) for ad in adapters}
            if ("okx", "perp") not in groups_present:
                _safe_addnstr(stdscr, y, 0, "WARN: okx_perp not loaded — add to --groups", w, colors.get('warn', 0))

        def _draw():
            stdscr.clear()
            height, width = stdscr.getmaxyx()
            title = f" InterestScore Dashboard — window={args.window}  order_size={args.order_size:.0f}  L2={'on' if args.with_l2 else 'off'} "
            status = f" sort={SORT_KEYS[sort_idx][0]} {'↓' if sort_reverse else '↑'}   filter=/{filter_text or '—'} "
            _safe_addnstr(stdscr, 0, 0, title, width, colors.get('header', 0))
            _safe_addnstr(stdscr, 1, 0, status, width, colors.get('sub', 0))
            _safe_addnstr(stdscr, height-1, 0, "[s]ort [S]↑/↓  [/]filter  [r]eset  [↑/↓/PgUp/PgDn]scroll  [q]quit", width, colors.get('footer', 0))

            try:
                all_rows = compute_scores(adapters, window_mins)
            except Exception as e:
                log_debug(f"compute_scores error: {e}")
                all_rows = []

            y = 2 - dash_scroll
            key_fn = SORT_KEYS[sort_idx][1]
            for gname in groups:
                hdr = f"[{gname[0].upper()} {gname[1].upper()}]"
                _safe_addnstr(stdscr, y, 0, hdr, width, curses.A_BOLD)
                y += 1

                rows_for_group = [r for r in all_rows if (r.exch, r.market) == gname]
                # Apply filter-mode: 'hard' filters out, 'soft/off' marks only
                fm = getattr(args, 'filter_mode', 'soft')
                if fm == 'hard':
                    render_rows = apply_fast_filters(rows_for_group, args.order_size, gname[1])
                else:
                    for r in rows_for_group:
                        mark_passing_filters([r], args.order_size, gname[1])
                    render_rows = rows_for_group

                f = (filter_text or "").lower()
                if f:
                    render_rows = [r for r in render_rows if f in r.symbol.lower() or f in r.base.lower()]

                render_rows.sort(key=key_fn, reverse=sort_reverse)
                display_rows = render_rows[:args.top]

                cols = _visible_columns(width, gname[1])
                base_layout = [("Symbol", 10), ("Exch", 6), ("Notl/min", 10), ("Trades/min", 10), ("Spread", 8), ("σ", 6), ("Depth±0.5%", 12), ("Depth±1%", 12), ("OIΔ15m%", 9), ("Score", 8)]
                layout = [t for t in base_layout if t[0] in cols]
                fixed = sum(w for _, w in layout) + len(layout) - 1
                layout[0] = (layout[0][0], layout[0][1] + max(0, width - fixed))

                x = 0
                for name, w in layout:
                    _safe_addnstr(stdscr, y, x, _cell(name, w), w, curses.A_BOLD)
                    x += w + 1
                y += 1

                row_map = {
                    "Symbol": lambda r: (r.symbol + ("" if not hasattr(r, "passes") else (" ✓" if getattr(r,"passes", False) else " ✗"))), "Exch": lambda r: r.exch.upper(),
                    "Notl/min": lambda r: f"{r.notl_per_min:,.0f}", "Trades/min": lambda r: f"{r.trades_per_min:.0f}",
                    "Spread": lambda r: (f"{r.spread_bps:.2f}" if math.isfinite(r.spread_bps) else "-"),
                    "σ": lambda r: (f"{r.spread_vol:.2f}" if math.isfinite(r.spread_vol) else "-"),
                    "Depth±0.5%": lambda r: ("-" if r.depth_0_5 is None or not math.isfinite(r.depth_0_5) else f"{r.depth_0_5:,.0f}"),
                    "Depth±1%": lambda r: ("-" if r.depth_1_0 is None or not math.isfinite(r.depth_1_0) else f"{r.depth_1_0:,.0f}"),
                    "OIΔ15m%": lambda r: ("-" if r.oi_delta_pct_15m is None else f"{r.oi_delta_pct_15m:.2f}"),
                    "Score": lambda r: f"{r.score:+.2f}",
                }
                for r in display_rows:
                    x = 0
                    attr = _row_color(colors, r)
                    for (name, w) in layout:
                        _safe_addnstr(stdscr, y, x, _cell(row_map[name](r), w), w, attr)
                        x += w + 1
                    y += 1

                y += 1  # space between groups

            if DEBUG_UI_ENABLE:
                _render_debug_panel(stdscr)
            stdscr.refresh()

        last = 0.0
        while True:
            if not _handle_input():
                break
            now = time.time()
            if (now - last) >= args.refresh:
                _draw()
                last = now
            time.sleep(0.02)

    await asyncio.to_thread(curses.wrapper, _loop)

# --------- Non-TUI print loop ---------

async def async_print_loop(adapters: List[ExchangeAdapter], args) -> None:
    window_mins = parse_window(args.window)

    def _top_per_group(rows: List[ScoreRow], top_n: int, filter_mode: str, order_size: float) -> List[ScoreRow]:
        groups = defaultdict(list)
        for r in rows:
            groups[(r.exch, r.market)].append(r)
        out = []
        for (exch, market), lst in groups.items():
            if filter_mode == 'hard':
                lst2 = apply_fast_filters(lst, order_size, market)
            else:
                # soft/off: mark but do not drop
                for r in lst:
                    mark_passing_filters([r], order_size, market)
                lst2 = lst
            lst2.sort(key=lambda x: x.score, reverse=True)
            out.extend(lst2[:top_n])
        return out

    try:
        while True:
            t0 = time.time()
            rows = compute_scores(adapters, window_mins)
            rows_top = _top_per_group(rows, args.top, getattr(args,'filter_mode','soft'), args.order_size)
            print_tables(rows_top, args.top)
            elapsed = time.time() - t0
            await asyncio.sleep(max(0.0, args.print_every - elapsed))
    except asyncio.CancelledError:
        return


# --------- Email Snapshot Helpers ---------

def _group_top(rows: List[ScoreRow], top_n: int, filter_mode: str, order_size: float):
    groups = defaultdict(list)
    for r in rows:
        groups[(r.exch, r.market)].append(r)
    out = {}
    for (exch, market), lst in groups.items():
        if filter_mode == "hard":
            lst2 = apply_fast_filters(lst, order_size, market)
        else:
            for r in lst:
                mark_passing_filters([r], order_size, market)
            lst2 = lst
        lst2.sort(key=lambda x: x.score, reverse=True)
        out[(exch, market)] = lst2[:top_n]
    return out

def _rows_to_csv(grouped: Dict[Tuple[str,str], List[ScoreRow]]) -> str:
    import io, csv
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["group","symbol","base","quote","notl_per_min","trades_per_min","spread_bps","spread_sigma","depth_0_5","depth_1_0","oi_delta_15m","score","passes"])
    for (exch, market), lst in grouped.items():
        gname = f"{exch}_{market}"
        for r in lst:
            w.writerow([gname, r.symbol, r.base, r.quote,
                        f"{r.notl_per_min:.0f}", f"{r.trades_per_min:.0f}",
                        ("" if not math.isfinite(r.spread_bps) else f"{r.spread_bps:.2f}"),
                        ("" if not math.isfinite(r.spread_vol) else f"{r.spread_vol:.2f}"),
                        ("" if r.depth_0_5 is None else f"{r.depth_0_5:.0f}"),
                        ("" if r.depth_1_0 is None else f"{r.depth_1_0:.0f}"),
                        ("" if r.oi_delta_pct_15m is None else f"{r.oi_delta_pct_15m:.2f}"),
                        f"{r.score:.2f}", getattr(r, "passes", "")])
    return buf.getvalue()

def _rows_to_html(grouped: Dict[Tuple[str,str], List[ScoreRow]]) -> str:
    def esc(x): 
        return (str(x).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;"))
    parts = []
    parts.append("<html><body>")
    parts.append(f"<h2>Interest Screener Snapshot</h2>")
    for (exch, market), lst in grouped.items():
        parts.append(f"<h3>{exch.upper()} {market.upper()}</h3>")
        parts.append("<table border='1' cellpadding='4' cellspacing='0' style='border-collapse:collapse;font-family:monospace;font-size:12px;'>")
        head = ["#", "Symbol", "Notl/min", "Trades/min", "Spread(bps)", "σ", "Depth±0.5%", "Depth±1%", "OI Δ15m%", "Score", "Pass?"]
        parts.append("<tr>" + "".join(f"<th>{esc(h)}</th>" for h in head) + "</tr>")
        for i, r in enumerate(lst, 1):
            row = [
                i, r.symbol, f"{r.notl_per_min:,.0f}", f"{r.trades_per_min:.0f}",
                ("" if not math.isfinite(r.spread_bps) else f"{r.spread_bps:.2f}"),
                ("" if not math.isfinite(r.spread_vol) else f"{r.spread_vol:.2f}"),
                ("" if r.depth_0_5 is None else f"{r.depth_0_5:,.0f}"),
                ("" if r.depth_1_0 is None else f"{r.depth_1_0:,.0f}"),
                ("" if r.oi_delta_pct_15m is None else f"{r.oi_delta_pct_15m:.2f}"),
                f"{r.score:.2f}", ("✓" if getattr(r, "passes", False) else "✗")
            ]
            parts.append("<tr>" + "".join(f"<td>{esc(x)}</td>" for x in row) + "</tr>")
        parts.append("</table>")
    parts.append("</body></html>")
    return "\n".join(parts)

def _send_email_snapshot(args, html_body: str, csv_data: str) -> None:
    to_list = [x.strip() for x in (args.email_to or "").split(",") if x.strip()]
    if not to_list:
        return
    sender = args.email_from or args.smtp_user or ""
    if not sender:
        log_debug("email: missing sender --email-from/--smtp-user; skip send")
        return
    user = args.smtp_user or sender
    pwd = args.smtp_pass or os.environ.get("GMAIL_APP_PASSWORD", "")
    host, port = args.smtp_host, int(args.smtp_port)

    msg = EmailMessage()
    subject = args.email_subject
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(to_list)
    msg.set_content("HTML snapshot attached. If you see this, your client does not support HTML.")
    msg.add_alternative(html_body, subtype="html")

    if csv_data:
        msg.add_attachment(csv_data.encode("utf-8"), maintype="text", subtype="csv", filename="snapshot.csv")

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.ehlo()
            server.starttls(context=ctx)
            server.ehlo()
            server.login(user, pwd)
            server.send_message(msg)
        log_debug(f"email: sent snapshot to {len(to_list)} recipient(s)")
    except Exception as e:
        log_debug(f"email send error: {e}")

async def _snapshot_mailer(adapters: List[ExchangeAdapter], args):
    # Only run if email_to provided
    if not args.email_to:
        return
    mins = parse_window(args.snapshot_interval)
    if mins < 1:
        mins = 1
    while True:
        try:
            rows = compute_scores(adapters, parse_window(args.window))
            grouped = _group_top(rows, args.snapshot_top, args.snapshot_filter_mode, args.order_size)
            html_body = _rows_to_html(grouped)
            csv_data = _rows_to_csv(grouped)
            # Offload blocking SMTP to thread
            await asyncio.to_thread(_send_email_snapshot, args, html_body, csv_data)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log_debug(f"snapshot loop error: {e}")
        await asyncio.sleep(mins * 60)
# --------- Runner ---------

async def main_async(args) -> None:
    parse_window(args.window)  # validate
    async with aiohttp.ClientSession() as session:
        adapters: List[ExchangeAdapter] = []
        gset = set(x.strip() for x in (args.groups if isinstance(args.groups, str) else ",".join(args.groups)).split(",") if x.strip())

        def add(cls):
            adapters.append(cls(session, args))

        if "binance_spot" in gset: add(BinanceSpot)
        if "binance_perp" in gset: add(BinancePerp)
        if "okx_spot" in gset: add(OKXSpot)
        if "okx_perp" in gset: add(OKXPerp)
        if "bybit_spot" in gset: add(BybitSpot)
        if "bybit_perp" in gset: add(BybitPerp)

        # Discover first
        for ad in adapters:
            try:
                await ad.discover_symbols()
            except Exception as e:
                log_debug(f"discover error {ad.name} {ad.market}: {e}")

        # WS loops
        tasks = [asyncio.create_task(ad.run()) for ad in adapters]
        # Email snapshot loop
        snap_task = asyncio.create_task(_snapshot_mailer(adapters, args))
        try:
            if args.tui:
                await run_dashboard_ui(adapters, args)
            else:
                await async_print_loop(adapters, args)
        finally:
            snap_task.cancel()
            with contextlib.suppress(Exception):
                await snap_task
            for ad in adapters:
                await ad.cancel_bg_tasks()
            for t in tasks:
                t.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

def parse_args(argv=None):
    import argparse
    p = argparse.ArgumentParser(description="Standalone InterestScore screener with dashboard + debug.log")
    p.add_argument("--window", default="15m", help="rolling window (e.g., 5m, 15m, 1h)")
    p.add_argument("--top", type=int, default=10, help="Top N per group")
    p.add_argument("--quotes", nargs="*", default=["USDT"], help="include quotes (default: USDT)")
    p.add_argument("--exclude-stable-base", action="store_true", dest="exclude_stable_base", default=True)
    p.add_argument("--groups", default="binance_spot,binance_perp,okx_spot,okx_perp,bybit_spot,bybit_perp", help="comma list of groups")
    p.add_argument("--with-l2", action="store_true", help="enable L2 orderbook depth (±0.5%/±1%)")
    p.add_argument("--order-size", type=float, default=1000.0, help="your order size in QUOTE (e.g., 1000 USDT)")
    p.add_argument("--print-every", type=float, default=30.0, help="print interval (non-TUI mode)")
    p.add_argument("--refresh", type=float, default=0.5, help="TUI refresh seconds")
    p.add_argument("--no-tui", action="store_true", help="disable TUI; print mode")
    p.add_argument("--max-syms-per-group", type=int, default=150, help="cap subscriptions per group")
    p.add_argument("--self-test", action="store_true", help="run self tests and exit")
    p.add_argument("--email-to", default="", help="comma emails to send snapshot to (enables mailer)")
    p.add_argument("--email-from", default="", help="sender email (e.g., your Gmail address)")
    p.add_argument("--smtp-host", default="smtp.gmail.com", help="SMTP host (default: gmail)")
    p.add_argument("--smtp-port", type=int, default=587, help="SMTP port (default: 587)")
    p.add_argument("--smtp-user", default="", help="SMTP username (default: --email-from)")
    p.add_argument("--smtp-pass", default="", help="SMTP password / app password (or set env GMAIL_APP_PASSWORD)")
    p.add_argument("--snapshot-interval", default="30m", help="send snapshot every N (e.g., 15m, 1h)")
    p.add_argument("--snapshot-top", type=int, default=10, help="Top N per group in email")
    p.add_argument("--snapshot-filter-mode", choices=["soft","hard","off"], default="soft", help="filters for email snapshot")
    p.add_argument("--email-subject", default="Interest Screener Snapshot", help="email subject prefix")
    p.add_argument("--filter-mode", choices=["soft","hard","off"], default="soft",
                   help="soft=mark only (default), hard=filter non-passing, off=ignore filters")
    p.add_argument("--no-debug", action="store_true", help="disable all debug logging (UI + file)")
    p.add_argument("--no-debug-ui", action="store_true", help="hide debug panel in TUI")
    p.add_argument("--no-debug-file", action="store_true", help="do not write debug.log")
    args = p.parse_args(argv)
    args.tui = not args.no_tui
    # Apply debug flags
    global DEBUG_UI_ENABLE, DEBUG_FILE_ENABLE
    if getattr(args, 'no_debug', False):
        DEBUG_UI_ENABLE = False
        DEBUG_FILE_ENABLE = False
    if getattr(args, 'no_debug_ui', False):
        DEBUG_UI_ENABLE = False
    if getattr(args, 'no_debug_file', False):
        DEBUG_FILE_ENABLE = False

    return args

def _run_self_tests() -> None:
    console.rule("[bold]Self-tests: Interest Screener core logic")
    assert parse_window("5m") == 5
    assert parse_window("15m") == 15
    assert parse_window("1h") == 60

    class DummyAdapter:
        def __init__(self, name: str, market: str) -> None:
            self.name = name; self.market = market; self.states: Dict[str, SymbolState] = {}

    def mk_state(symbol: str, base: str, quote: str, group_key: str) -> SymbolState:
        st = SymbolState(symbol=symbol, base=base, quote=quote, group_key=group_key)
        t0 = now_ms() - 4 * 60_000
        for i in range(5):
            ts = t0 + i * 60_000 + 1000
            for _ in range(120):
                st.add_trade(ts, 100.0 + i, 0.5)
            st.add_spread(ts, 100.0, 100.01)
        return st

    ad_spot = DummyAdapter("binance", "spot")
    st_btc = mk_state("BTCUSDT", "BTC", "USDT", "binance_spot")
    st_alt = mk_state("ALTUSDT", "ALT", "USDT", "binance_spot")
    ad_spot.states[st_btc.symbol.lower()] = st_btc
    ad_spot.states[st_alt.symbol.lower()] = st_alt

    ad_perp = DummyAdapter("binance", "perp")
    st_btc_p = mk_state("BTCUSDT", "BTC", "USDT", "binance_perp")
    nowt = now_ms()
    st_btc_p.add_oi(nowt - 16*60_000, 1000.0); st_btc_p.add_oi(nowt, 1010.0)
    ad_perp.states[st_btc_p.symbol.lower()] = st_btc_p

    rows = compute_scores([ad_spot, ad_perp], window_mins=5)
    assert any(r.symbol == "BTCUSDT" and r.market == "spot" for r in rows)
    assert any(r.symbol == "BTCUSDT" and r.market == "perp" for r in rows)
    filtered_spot = apply_fast_filters([r for r in rows if r.market == "spot"], order_size_quote=10.0, market="spot")
    assert len(filtered_spot) >= 1, "Spot rows should pass with small order size"
    filtered_perp = apply_fast_filters([r for r in rows if r.market == "perp"], order_size_quote=10.0, market="perp")
    assert len(filtered_perp) >= 1, "Perp rows should pass with small order size"
    console.print("[green]All self-tests passed.[/green]")

def main(argv=None) -> None:
    args = parse_args(argv)
    if getattr(args, "self_test", False):
        _run_self_tests()
        return
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\nInterrupted")

if __name__ == "__main__":
    main()
