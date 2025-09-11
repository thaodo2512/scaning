from __future__ import annotations

import argparse
import csv
import curses
import json
import math
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..config import load_config, validate_config, EffectiveConfig
from ..retrieve.coinglass import _output_filename


def _utc_now_ms() -> int:
    return int(time.time() * 1000)


def _ms_to_iso(ms: Optional[int]) -> str:
    if ms is None:
        return "-"
    try:
        import datetime as dt

        return dt.datetime.utcfromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M:%SZ")
    except Exception:
        return str(ms)


def _align_to_5m_close(ms: int) -> int:
    step = 5 * 60 * 1000
    return ms - (ms % step)


def _last_feature_ts(feat_fp: Path) -> Optional[int]:
    if not feat_fp.exists():
        return None
    try:
        # Read last few lines, parse last non-empty
        with feat_fp.open("r", encoding="utf-8") as f:
            lines = f.read().splitlines()
            for line in reversed(lines[1:]):  # skip header
                if not line.strip():
                    continue
                try:
                    ts_str = line.split(",", 2)[0]
                    return int(ts_str)
                except Exception:
                    continue
    except Exception:
        return None
    return None


def _load_sidecar_last_ts(data_sym_dir: Path, ds_key: str) -> Optional[int]:
    sp = data_sym_dir / ".state" / f"{ds_key}.json"
    try:
        if sp.exists():
            obj = json.loads(sp.read_text(encoding="utf-8") or "{}")
            v = obj.get("last_ts")
            if isinstance(v, (int, float)):
                return int(v)
    except Exception:
        return None
    # fallback: scan JSONL file if present
    fp = data_sym_dir / _output_filename(ds_key)
    if not fp.exists():
        return None
    try:
        last = None
        for line in fp.read_text(encoding="utf-8").splitlines():
            try:
                obj = json.loads(line)
                ts = obj.get("ts")
                if isinstance(ts, (int, float)):
                    v = int(ts)
                    last = v if (last is None or v > last) else last
            except Exception:
                continue
        return last
    except Exception:
        return None


def _read_last_slo(artifacts_root: Path) -> Optional[Dict[str, Any]]:
    fp = artifacts_root / "metrics" / "realtime.jsonl"
    try:
        if not fp.exists():
            return None
        lines = fp.read_text(encoding="utf-8").splitlines()
        if not lines:
            return None
        return json.loads(lines[-1])
    except Exception:
        return None


def _read_last_alert_ts(artifacts_root: Path, sym: str) -> Optional[int]:
    fp = artifacts_root / "alerts" / f"{sym}.csv"
    if not fp.exists():
        return None
    try:
        with fp.open("r", encoding="utf-8") as f:
            rdr = list(csv.DictReader(f))
            if not rdr:
                return None
            return int(rdr[-1].get("ts") or 0)
    except Exception:
        return None


def _age_str(now_ms: int, ts: Optional[int]) -> str:
    if not isinstance(ts, int):
        return "-"
    d = max(0, now_ms - ts)
    mins = d // (60 * 1000)
    if mins < 60:
        return f"{mins}m"
    hrs = mins // 60
    return f"{hrs}h"


def _draw(stdscr, cfg: Mapping[str, Any], eff: EffectiveConfig, data_root: Path, features_root: Path, artifacts_root: Path, datasets: List[str], max_symbols: int, refresh_s: float):
    curses.curs_set(0)
    stdscr.nodelay(True)
    while True:
        stdscr.erase()
        now_ms = _utc_now_ms()
        bar_ts = _align_to_5m_close(now_ms)
        slo = _read_last_slo(artifacts_root)
        status = "OK" if slo and abs(int(time.time()) - int(slo.get("ts", 0))) <= 120 else "STALE"
        # Header
        title = f"CryptoStorm Monitor — run_id={eff.run_id} now={_ms_to_iso(now_ms)} bar={_ms_to_iso(bar_ts)} status={status}"
        stdscr.addstr(0, 0, title[: curses.COLS - 1], curses.A_BOLD)
        if slo:
            slo_line = f"last: lag={slo.get('scheduler_lag_s','-')}s retrieve={slo.get('retrieve_ms','-')}ms feature={slo.get('feature_ms','-')}ms score={slo.get('score_ms','-')}ms mode={slo.get('mode','-')}"
            stdscr.addstr(1, 0, slo_line[: curses.COLS - 1])
        # Table header
        hdr = "Symbol  Feat(ts/age)  " + "  ".join([f"{ds.split('_')[0]}(ts/age)" for ds in datasets]) + "  LastAlert(age)"
        stdscr.addstr(3, 0, hdr[: curses.COLS - 1], curses.A_UNDERLINE)

        # Rows per symbol
        row = 4
        for i, sym in enumerate(eff.symbols[: max_symbols]):
            data_sym = data_root / sym
            feat_ts = _last_feature_ts(features_root / sym / "features_5m.csv")
            parts = [sym.ljust(7), f"{_ms_to_iso(feat_ts)} / {_age_str(now_ms, feat_ts)}"]
            for ds in datasets:
                ts = _load_sidecar_last_ts(data_sym, ds)
                parts.append(f"{_ms_to_iso(ts)} / {_age_str(now_ms, ts)}")
            lat = _read_last_alert_ts(artifacts_root, sym)
            parts.append(f"{_ms_to_iso(lat)} ({_age_str(now_ms, lat)})")
            line = "  ".join(parts)
            # Color by overall freshness: red if any critical ds older than 15m
            ages_ok = True
            for ds in datasets:
                ts = _load_sidecar_last_ts(data_sym, ds)
                if not isinstance(ts, int) or (now_ms - ts) > (15 * 60 * 1000):
                    ages_ok = False
                    break
            attr = curses.color_pair(2) if ages_ok else curses.color_pair(1)
            stdscr.addstr(row, 0, line[: curses.COLS - 1], attr)
            row += 1
            if row >= curses.LINES - 2:
                break

        # Footer
        stdscr.addstr(curses.LINES - 1, 0, f"q=quit  refresh={refresh_s}s  datasets={','.join(datasets)}  symbols_shown={min(max_symbols, len(eff.symbols))}/{len(eff.symbols)}")
        stdscr.refresh()

        # Key handling
        try:
            c = stdscr.getch()
            if c == ord('q'):
                break
        except Exception:
            pass
        time.sleep(refresh_s)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="CryptoStorm console monitor")
    parser.add_argument("config", type=str)
    parser.add_argument("--data", type=str, default="data")
    parser.add_argument("--features", type=str, default="features")
    parser.add_argument("--artifacts", type=str)
    parser.add_argument("--datasets", type=str, default="futures_ohlcv_5m,oi_5m_ohlc,orderbook_futures_5m")
    parser.add_argument("--symbols", type=int, default=20)
    parser.add_argument("--refresh-s", type=float, default=2.0)
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    eff, warns, errs = validate_config(cfg, require_env=False)
    if errs:
        for e in errs:
            print(f"error: {e}")
        return 2
    run_id = (cfg.get("run") or {}).get("run_id") or eff.run_id
    artifacts_root = Path(args.artifacts) if args.artifacts else Path((cfg.get("run") or {}).get("artifacts_root", "./artifacts")) / str(run_id)
    datasets = [d.strip() for d in args.datasets.split(",") if d.strip()]

    def _run(stdscr):
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_RED, -1)
        curses.init_pair(2, curses.COLOR_GREEN, -1)
        _draw(stdscr, cfg, eff, Path(args.data), Path(args.features), artifacts_root, datasets, max_symbols=int(args.symbols), refresh_s=float(args.refresh_s))

    curses.wrapper(_run)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

