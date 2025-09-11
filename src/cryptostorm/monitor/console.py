from __future__ import annotations

import argparse
import csv
import curses
import json
import math
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
import logging

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


def _read_last_alert(artifacts_root: Path, sym: str) -> Optional[Tuple[int, str]]:
    fp = artifacts_root / "alerts" / f"{sym}.csv"
    if not fp.exists():
        return None
    try:
        with fp.open("r", encoding="utf-8") as f:
            rdr = list(csv.DictReader(f))
            if not rdr:
                return None
            last = rdr[-1]
            t = int(last.get("ts") or 0)
            kind = str(last.get("kind") or "")
            return (t, kind)
    except Exception:
        return None


def _read_last_score(artifacts_root: Path, sym: str) -> Optional[Tuple[int, float, float]]:
    fp = artifacts_root / "scores" / f"{sym}.csv"
    if not fp.exists():
        return None
    try:
        with fp.open("r", encoding="utf-8") as f:
            rdr = list(csv.DictReader(f))
            if not rdr:
                return None
            last = rdr[-1]
            ts = int(last.get("ts") or 0)
            score = float(last.get("score") or "nan")
            thr = float(last.get("threshold") or "nan")
            return (ts, score, thr)
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


def _draw(
    stdscr,
    cfg: Mapping[str, Any],
    eff: EffectiveConfig,
    data_root: Path,
    features_root: Path,
    artifacts_root: Path,
    datasets: List[str],
    max_symbols: int,
    refresh_s: float,
    view: str,
):
    curses.curs_set(0)
    stdscr.nodelay(True)
    LOG = logging.getLogger("cryptostorm.monitor")
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
        if view == "alerts":
            hdr = "Symbol  Score(ts/age)  value  thr  LatestAlert(kind/age)"
        else:
            hdr = "Symbol  Feat(ts/age)  " + "  ".join([f"{ds.split('_')[0]}(ts/age)" for ds in datasets]) + "  LastAlert(age)"
        stdscr.addstr(3, 0, hdr[: curses.COLS - 1], curses.A_UNDERLINE)

        # Rows per symbol
        row = 4
        stale_count = 0
        for i, sym in enumerate(eff.symbols[: max_symbols]):
            data_sym = data_root / sym
            if view == "alerts":
                # Scores + latest alert view
                last_score = _read_last_score(artifacts_root, sym)
                if last_score:
                    s_ts, s_val, s_thr = last_score
                else:
                    s_ts, s_val, s_thr = None, float("nan"), float("nan")
                last_alert = _read_last_alert(artifacts_root, sym)
                if last_alert:
                    a_ts, a_kind = last_alert
                else:
                    a_ts, a_kind = None, "-"
                parts = [
                    sym.ljust(7),
                    f"{_ms_to_iso(s_ts)} / {_age_str(now_ms, s_ts)}",
                    (f"{s_val:.3f}" if isinstance(s_val, float) else "nan"),
                    (f"{s_thr:.3f}" if isinstance(s_thr, float) else "nan"),
                    f"{a_kind} ({_age_str(now_ms, a_ts)})",
                ]
                line = "  ".join(parts)
                # Color by score vs threshold when available
                attr = curses.color_pair(2)
                try:
                    if (not (isinstance(s_val, float) and isinstance(s_thr, float))) or not (s_val >= s_thr):
                        attr = curses.color_pair(1)
                except Exception:
                    attr = curses.color_pair(1)
            else:
                # Data freshness view
                feat_ts = _last_feature_ts(features_root / sym / "features_5m.csv")
                parts = [sym.ljust(7), f"{_ms_to_iso(feat_ts)} / {_age_str(now_ms, feat_ts)}"]
                for ds in datasets:
                    ts = _load_sidecar_last_ts(data_sym, ds)
                    parts.append(f"{_ms_to_iso(ts)} / {_age_str(now_ms, ts)}")
                last_alert = _read_last_alert(artifacts_root, sym)
                lat_ts = last_alert[0] if last_alert else None
                parts.append(f"{_ms_to_iso(lat_ts)} ({_age_str(now_ms, lat_ts)})")
                line = "  ".join(parts)
                # Color by overall freshness: red if any critical ds older than 15m
                ages_ok = True
                for ds in datasets:
                    ts = _load_sidecar_last_ts(data_sym, ds)
                    if not isinstance(ts, int) or (now_ms - ts) > (15 * 60 * 1000):
                        ages_ok = False
                        break
                attr = curses.color_pair(2) if ages_ok else curses.color_pair(1)
                if not ages_ok:
                    stale_count += 1
            stdscr.addstr(row, 0, line[: curses.COLS - 1], attr)
            row += 1
            if row >= curses.LINES - 2:
                break

        try:
            LOG.debug(
                "cycle now=%s bar=%s status=%s view=%s shown=%d stale=%d",
                _ms_to_iso(now_ms),
                _ms_to_iso(bar_ts),
                status,
                view,
                min(max_symbols, len(eff.symbols)),
                stale_count,
            )
        except Exception:
            pass

        # Footer
        stdscr.addstr(curses.LINES - 1, 0, f"q=quit  refresh={refresh_s}s  datasets={','.join(datasets)}  symbols_shown={min(max_symbols, len(eff.symbols))}/{len(eff.symbols)}")
        stdscr.refresh()

        # Key handling
        try:
            c = stdscr.getch()
            if c == ord('q'):
                try:
                    LOG.debug("quit requested via keypress")
                except Exception:
                    pass
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
    parser.add_argument("--view", type=str, choices=["data", "alerts"], default="data")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging to debug.log")
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

    # Configure debug logging if requested
    if bool(args.debug):
        try:
            logging.basicConfig(
                level=logging.DEBUG,
                format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                handlers=[
                    logging.FileHandler("debug.log", mode="a", encoding="utf-8"),
                ],
            )
            logging.getLogger("cryptostorm.monitor").debug("monitor started (view=%s)", args.view)
        except Exception:
            pass

    def _run(stdscr):
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_RED, -1)
        curses.init_pair(2, curses.COLOR_GREEN, -1)
        _draw(
            stdscr,
            cfg,
            eff,
            Path(args.data),
            Path(args.features),
            artifacts_root,
            datasets,
            max_symbols=int(args.symbols),
            refresh_s=float(args.refresh_s),
            view=str(args.view),
        )

    curses.wrapper(_run)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
