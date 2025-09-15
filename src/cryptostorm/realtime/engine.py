from __future__ import annotations

import argparse
import logging
import os
import random
import time
from pathlib import Path
from typing import Optional
import json

from ..config import load_config, validate_config, EffectiveConfig
from ..retrieve.coinglass import run_retrieve
from ..feature.engine import update_features_last
from ..backtest.engine import run_backtest
# (duplicates removed)


def _utc_now_ms() -> int:
    return int(time.time() * 1000)


def _align_to_5m_close(ms: int) -> int:
    step = 5 * 60 * 1000
    return int(ms) - (int(ms) % step)


def _sleep_until(target_ms: int, jitter_s: float) -> None:
    now = _utc_now_ms()
    delay_ms = max(0, target_ms - now)
    if jitter_s and jitter_s > 0:
        delay_ms += int(random.uniform(0, float(jitter_s)) * 1000)
    if delay_ms > 0:
        time.sleep(delay_ms / 1000.0)


def _resolve_artifacts_root(cfg: dict, eff: EffectiveConfig, override: Optional[str]) -> Path:
    if override:
        return Path(override)
    run_id = (cfg.get("run") or {}).get("run_id") or eff.run_id
    root = Path((cfg.get("run") or {}).get("artifacts_root", "./artifacts")) / str(run_id)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _retrieve_once(cfg: dict, eff: EffectiveConfig, *, data_root: Path) -> None:
    acq = cfg.get("acquisition", {})
    cg = (acq or {}).get("coinglass", {})
    base_url = cg.get("base_url", "https://open-api-v4.coinglass.com")
    v3_base_url = cg.get("v3_base_url", "https://open-api.coinglass.com")
    exchange = cg.get("exchange", "binance")
    quote = cg.get("quote", "USDT")
    paging = cg.get("paging", {})
    page_limit = int(paging.get("page_limit", 500))
    backoff_initial = float(paging.get("backoff_initial_s", 1))
    backoff_max = float(paging.get("backoff_max_s", 64))
    force_v3 = list(cg.get("force_v3", []) or [])
    slice_days = int(cg.get("slice_days", 0) or 0)
    orderbook_time_enum = cg.get("orderbook_time_enum") or (
        "LAST_OF_5M" if (cg.get("intervals", {}) or {}).get("orderbook_sample") == "last_of_5m" else None
    )

    run_retrieve(
        eff,
        base_url=base_url,
        v3_base_url=v3_base_url,
        exchange=exchange,
        quote=quote,
        page_limit=page_limit,
        backoff_initial=backoff_initial,
        backoff_max=backoff_max,
        out_root=data_root,
        dry_run=False,
        api_key=None,
        slice_days=slice_days,
        force_v3=force_v3,
        orderbook_time_enum=orderbook_time_enum,
    )


def run_once(cfg: dict, eff: EffectiveConfig, *, data_root: Path, features_root: Path, artifacts_root: Path, send_telegram: bool, telegram_kinds: str) -> None:
    LOG = logging.getLogger("cryptostorm.realtime")
    LOG.info("Starting cycle: retrieve → update-last → backtest → alerts")
    _retrieve_once(cfg, eff, data_root=data_root)
    update_features_last(eff, data_root=data_root, out_root=features_root)
    run_backtest(cfg, eff, features_root=features_root, out_root=artifacts_root)
    if send_telegram:
        from ..notify.telegram import main as telegram_main
        try:
            telegram_main([cfg.get("_path", ""), "--artifacts", str(artifacts_root), "--kinds", telegram_kinds, "--only-new"])  # type: ignore[arg-type]
        except Exception:
            LOG.info("Telegram sending skipped or failed; ensure credentials and config path are set")


def _write_report_index(reports_root: Path, symbols: list[str]) -> None:
    try:
        rows = []
        import time as _t
        now = _t.strftime("%Y-%m-%d %H:%M:%SZ", _t.gmtime())
        for s in symbols:
            items = []
            for name in (f"{s}.html", f"{s}_plotly.html", f"{s}_price_alert.html"):
                fp = reports_root / name
                if fp.exists():
                    items.append((name, fp.stat().st_mtime))
            rows.append((s, items))
        rows.sort(key=lambda x: x[0])
        html = [
            "<!doctype html>",
            "<html><head><meta charset=\"utf-8\" />",
            "<title>CryptoStorm Reports</title>",
            "<style>body{font-family:-apple-system,system-ui,Segoe UI,Roboto,sans-serif;background:#111;color:#ddd;margin:0} .wrap{padding:10px} a{color:#9bd;text-decoration:none} table{border-collapse:collapse;width:100%} th,td{padding:6px 8px;border-bottom:1px solid #333;text-align:left} th{color:#bbb} .ts{color:#aaa;font-size:12px}</style>",
            "</head><body><div class=wrap>",
            f"<h2>CryptoStorm Reports <span class=ts>(generated {now})</span></h2>",
            "<table><thead><tr><th>Symbol</th><th>Available</th></tr></thead><tbody>",
        ]
        for sym, items in rows:
            links = []
            for name, _mt in items:
                label = name.replace(sym, "").lstrip("_") or "lightweight"
                links.append(f"<a href=\"{name}\">{label}</a>")
            html.append(f"<tr><td>{sym}</td><td>{' | '.join(links) if links else '-'} </td></tr>")
        html.extend(["</tbody></table>", "</div></body></html>"])
        reports_root.mkdir(parents=True, exist_ok=True)
        (reports_root / "index.html").write_text("\n".join(html), encoding="utf-8")
    except Exception:
        pass


def _build_report_one(
    engine: str,
    cfg: dict,
    eff: EffectiveConfig,
    *,
    data_root: Path,
    features_root: Path,
    artifacts_root: Path,
    reports_root: Path,
    symbol: str,
) -> str:
    # Build a report for a single symbol by cloning EffectiveConfig
    from dataclasses import replace

    eff1 = replace(eff, symbols=[symbol], symbol_to_coin={k: v for k, v in eff.symbol_to_coin.items() if k == symbol})
    try:
        if engine == "plotly":
            from ..report.plotly_full import build_reports as _b

            _b(cfg, eff1, data_root=data_root, artifacts_root=artifacts_root, out_root=reports_root)
        elif engine == "price":
            from ..report.price_alert import build_reports as _b

            _b(cfg, eff1, data_root=data_root, artifacts_root=artifacts_root, out_root=reports_root)
        else:  # lightweight
            from ..report.engine import build_reports as _b

            _b(cfg, eff1, data_root=data_root, features_root=features_root, artifacts_root=artifacts_root, out_root=reports_root)
    except Exception:
        # Best-effort; failure will just skip this symbol
        pass
    return symbol


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="CryptoStorm realtime runner (Phase 1)")
    parser.add_argument("run", nargs="?", default="run", help="Subcommand placeholder (use 'run')")
    parser.add_argument("config", type=str, help="Path to YAML/JSON config")
    parser.add_argument("--data", type=str, default="data")
    parser.add_argument("--features", type=str, default="features")
    parser.add_argument("--artifacts", type=str, help="Artifacts root override")
    parser.add_argument("--poll-offset-s", type=float, default=15.0)
    parser.add_argument("--jitter-s", type=float, default=2.0)
    parser.add_argument("--once", action="store_true", help="Run a single cycle immediately")
    parser.add_argument("--log-level", type=str, default="INFO")
    parser.add_argument("--send-telegram", action="store_true")
    parser.add_argument("--telegram-kinds", type=str, default="storm,pre_alert")
    parser.add_argument("--online-scoring", action="store_true", help="Use online scoring (no retrain) if artifacts present")
    parser.add_argument("--build-reports", action="store_true", help="Build reports after each cycle and update index.html")
    parser.add_argument("--reports", type=str, default="reports", help="Reports output directory")
    parser.add_argument("--report-engine", type=str, choices=["plotly", "lightweight", "price"], default="price")
    parser.add_argument("--bar-interval", type=str, choices=["5m", "15m"], default="15m")
    parser.add_argument("--workers", type=int, default=0, help="Per-symbol parallel workers for features/backtest (0=auto)")
    parser.add_argument("--coinglass-rps", type=float, default=4.1667, help="Global Coinglass request rate (req/s), capped to ~250/min")
    args = parser.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cfg = load_config(args.config)
    eff, warns, errs = validate_config(cfg, require_env=True)
    if errs:
        for e in errs:
            logging.getLogger("cryptostorm").error(e)
        return 2
    # attach original path for downstream convenience (telegram)
    cfg["_path"] = args.config  # type: ignore[index]

    data_root = Path(args.data)
    features_root = Path(args.features)
    artifacts_root = _resolve_artifacts_root(cfg, eff, args.artifacts)

    # Helper: check training lock to avoid races with trainer
    def _training_locked() -> bool:
        try:
            lp = artifacts_root / ".locks" / "retraining.lock"
            return lp.exists()
        except Exception:
            return False

    # Resolve workers (0=auto)
    try:
        import os as _os
        workers = int(args.workers)
        if workers <= 0:
            workers = int(_os.cpu_count() or 1)
    except Exception:
        workers = 1

    # Set global Coinglass rate limiter (~250 req/min by default)
    try:
        rps = float(args.coinglass_rps)
        if rps > 4.1667:
            rps = 4.1667
        if rps > 0:
            _set_global_rps_limiter(_RateLimiter(rps))
    except Exception:
        pass

    if args.once:
        # Respect training lock: skip this cycle to avoid conflicts
        if _training_locked():
            logging.getLogger("cryptostorm.realtime").info("training lock present; skipping realtime cycle")
            return 0
        _cycle_start = time.monotonic()
        step_ms = 5 * 60 * 1000 if args.bar_interval == "5m" else 15 * 60 * 1000
        bar_ts = _utc_now_ms() - (_utc_now_ms() % step_ms)
        # Timings
        t0 = time.monotonic()
        _retrieve_once(cfg, eff, data_root=data_root)
        t1 = time.monotonic()
        # Feature append (parallel if workers>1)
        if workers > 1:
            from ..feature.engine import _parallel_features as _pf  # type: ignore

            _pf(
                eff,
                data_root=data_root,
                out_root=features_root,
                now_ms=None,
                interval=("15m" if args.bar_interval == "15m" else "5m"),
                update_last=True,
                workers=workers,
                log_level=args.log_level,
            )
        else:
            if args.bar_interval == "15m":
                from ..feature.engine import update_features_last_15m as _upd
                _upd(eff, data_root=data_root, out_root=features_root)
            else:
                update_features_last(eff, data_root=data_root, out_root=features_root)
        t2 = time.monotonic()
        if args.online_scoring:
            from ..backtest.engine import score_online

            score_online(
                cfg,
                eff,
                features_root=features_root,
                out_root=artifacts_root,
                features_interval=("15m" if args.bar_interval == "15m" else "5m"),
                workers=workers,
            )
        else:
            run_backtest(
                cfg,
                eff,
                features_root=features_root,
                out_root=artifacts_root,
                features_interval=("15m" if args.bar_interval == "15m" else "5m"),
                workers=workers,
            )
        t3 = time.monotonic()
        # Optional alerts
        if args.send_telegram:
            from ..notify.telegram import main as telegram_main

            try:
                # Dynamic lookback: if cycle took longer than one bar, include prior bars (cap at 3)
                step_ms = 5 * 60 * 1000 if args.bar_interval == "5m" else 15 * 60 * 1000
                lag_s = max(0.0, time.monotonic() - _cycle_start)
                bars_back = int((lag_s * 1000 + step_ms - 1) // step_ms)
                if bars_back < 0:
                    bars_back = 0
                if bars_back > 3:
                    bars_back = 3
                since_ts = int(bar_ts) - bars_back * step_ms
                telegram_main([
                    cfg.get("_path", ""),
                    "--artifacts", str(artifacts_root),
                    "--kinds", args.telegram_kinds,
                    "--only-new",
                    "--since-ts", str(int(since_ts)),
                ])  # type: ignore[arg-type]
            except Exception:
                pass
        # Optional reports
        if args.build_reports:
            try:
                reports_root = Path(args.reports)
                if workers > 1:
                    import concurrent.futures as _cf

                    done = 0
                    total = len(eff.symbols)
                    with _cf.ProcessPoolExecutor(max_workers=workers) as ex:
                        futs = [
                            ex.submit(
                                _build_report_one,
                                args.report_engine,
                                cfg,
                                eff,
                                data_root=Path(args.data),
                                features_root=features_root,
                                artifacts_root=artifacts_root,
                                reports_root=reports_root,
                                symbol=s,
                            )
                            for s in eff.symbols
                        ]
                        for _f in _cf.as_completed(futs):
                            done += 1
                    _write_report_index(reports_root, eff.symbols)
                else:
                    if args.report_engine == "plotly":
                        from ..report.plotly_full import build_reports as build_plotly

                        build_plotly(cfg, eff, data_root=Path(args.data), artifacts_root=artifacts_root, out_root=reports_root)
                    elif args.report_engine == "price":
                        from ..report.price_alert import build_reports as build_price

                        build_price(cfg, eff, data_root=Path(args.data), artifacts_root=artifacts_root, out_root=reports_root)
                    else:
                        from ..report.engine import build_reports as build_lw

                        build_lw(cfg, eff, data_root=Path(args.data), features_root=features_root, artifacts_root=artifacts_root, out_root=reports_root)
                    _write_report_index(reports_root, eff.symbols)
            except Exception:
                pass
        # SLO metrics
        slo = {
            "ts": int(time.time()),
            "bar_ts": int(bar_ts / 1000),
            "scheduler_lag_s": max(0.0, (time.monotonic() - _cycle_start)),
            "retrieve_ms": int((t1 - t0) * 1000),
            "feature_ms": int((t2 - t1) * 1000),
            "score_ms": int((t3 - t2) * 1000),
            "mode": ("online" if args.online_scoring else "train"),
        }
        try:
            mdir = artifacts_root / "metrics"
            mdir.mkdir(parents=True, exist_ok=True)
            with (mdir / "realtime.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(slo) + "\n")
        except Exception:
            pass
        return 0

    LOG = logging.getLogger("cryptostorm.realtime")
    LOG.info("Realtime loop started: offset=%.1fs jitter≤%.1fs", args.poll_offset_s, args.jitter_s)
    while True:
        # If trainer is running, pause this cycle
        if _training_locked():
            logging.getLogger("cryptostorm.realtime").info("training lock present; sleeping until next bar")
            # Sleep to next bar offset directly
            step_ms = 5 * 60 * 1000 if args.bar_interval == "5m" else 15 * 60 * 1000
            next_target = (_utc_now_ms() - (_utc_now_ms() % step_ms)) + step_ms + int(args.poll_offset_s * 1000)
            _sleep_until(next_target, args.jitter_s)
            continue
        now = _utc_now_ms()
        step_ms = 5 * 60 * 1000 if args.bar_interval == "5m" else 15 * 60 * 1000
        bar_ts = now - (now % step_ms)
        target = bar_ts + int(args.poll_offset_s * 1000)
        if now < target:
            _sleep_until(target, args.jitter_s)
        try:
            _cycle_start = time.monotonic()
            # Step timings
            t0 = time.monotonic()
            _retrieve_once(cfg, eff, data_root=data_root)
            t1 = time.monotonic()
            # Feature append (parallel if workers>1)
            if workers > 1:
                from ..feature.engine import _parallel_features as _pf  # type: ignore

                _pf(
                    eff,
                    data_root=data_root,
                    out_root=features_root,
                    now_ms=None,
                    interval=("15m" if args.bar_interval == "15m" else "5m"),
                    update_last=True,
                    workers=workers,
                    log_level=args.log_level,
                )
            else:
                if args.bar_interval == "15m":
                    from ..feature.engine import update_features_last_15m as _upd
                    _upd(eff, data_root=data_root, out_root=features_root)
                else:
                    update_features_last(eff, data_root=data_root, out_root=features_root)
            t2 = time.monotonic()
            if args.online_scoring:
                from ..backtest.engine import score_online

                score_online(cfg, eff, features_root=features_root, out_root=artifacts_root, workers=workers)
            else:
                run_backtest(cfg, eff, features_root=features_root, out_root=artifacts_root, workers=workers)
            t3 = time.monotonic()
            if args.send_telegram:
                from ..notify.telegram import main as telegram_main

                try:
                    # Dynamic lookback for long cycles (cap at 3 bars)
                    step_ms = 5 * 60 * 1000 if args.bar_interval == "5m" else 15 * 60 * 1000
                    lag_s = max(0.0, time.monotonic() - _cycle_start)
                    bars_back = int((lag_s * 1000 + step_ms - 1) // step_ms)
                    if bars_back < 0:
                        bars_back = 0
                    if bars_back > 3:
                        bars_back = 3
                    since_ts = int(bar_ts) - bars_back * step_ms
                    telegram_main([
                        cfg.get("_path", ""),
                        "--artifacts", str(artifacts_root),
                        "--kinds", args.telegram_kinds,
                        "--only-new",
                        "--since-ts", str(int(since_ts)),
                    ])  # type: ignore[arg-type]
                except Exception:
                    pass
            # Optional reports
            if args.build_reports:
                try:
                    reports_root = Path(args.reports)
                    if workers > 1:
                        import concurrent.futures as _cf

                        with _cf.ProcessPoolExecutor(max_workers=workers) as ex:
                            futs = [
                                ex.submit(
                                    _build_report_one,
                                    args.report_engine,
                                    cfg,
                                    eff,
                                    data_root=Path(args.data),
                                    features_root=features_root,
                                    artifacts_root=artifacts_root,
                                    reports_root=reports_root,
                                    symbol=s,
                                )
                                for s in eff.symbols
                            ]
                            for _f in _cf.as_completed(futs):
                                pass
                        _write_report_index(reports_root, eff.symbols)
                    else:
                        if args.report_engine == "plotly":
                            from ..report.plotly_full import build_reports as build_plotly

                            build_plotly(cfg, eff, data_root=Path(args.data), artifacts_root=artifacts_root, out_root=reports_root)
                        elif args.report_engine == "price":
                            from ..report.price_alert import build_reports as build_price

                            build_price(cfg, eff, data_root=Path(args.data), artifacts_root=artifacts_root, out_root=reports_root)
                        else:
                            from ..report.engine import build_reports as build_lw

                            build_lw(cfg, eff, data_root=Path(args.data), features_root=features_root, artifacts_root=artifacts_root, out_root=reports_root)
                        _write_report_index(reports_root, eff.symbols)
                except Exception:
                    pass

            slo = {
                "ts": int(time.time()),
                "bar_ts": int(bar_ts / 1000),
                "scheduler_lag_s": max(0.0, (time.monotonic() - _cycle_start)),
                "retrieve_ms": int((t1 - t0) * 1000),
                "feature_ms": int((t2 - t1) * 1000),
                "score_ms": int((t3 - t2) * 1000),
                "mode": ("online" if args.online_scoring else "train"),
            }
            try:
                mdir = artifacts_root / "metrics"
                mdir.mkdir(parents=True, exist_ok=True)
                with (mdir / "realtime.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps(slo) + "\n")
            except Exception:
                pass
        except Exception as e:  # noqa: BLE001
            LOG.warning("Realtime cycle failed: %s", e)
        # Sleep until next bar's offset
        next_target = (_utc_now_ms() - (_utc_now_ms() % step_ms)) + step_ms + int(args.poll_offset_s * 1000)
        _sleep_until(next_target, args.jitter_s)

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


# -------------------------
# Watch mode for retrieve only (Phase 2)
# -------------------------

def _clone_eff_for_symbols(eff: EffectiveConfig, symbols: list[str]) -> EffectiveConfig:
    # Shallow clone EffectiveConfig with limited symbols and symbol_to_coin subset
    from dataclasses import replace

    sub_map = {k: v for k, v in eff.symbol_to_coin.items() if k in symbols}
    return replace(eff, symbols=list(symbols), symbol_to_coin=sub_map)


class _RateLimiter:
    def __init__(self, rps: float) -> None:
        import threading

        self.rps = max(0.1, float(rps))
        self.lock = threading.Lock()
        self.tokens = self.rps
        self.last = time.monotonic()

    def acquire(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                elapsed = now - self.last
                self.last = now
                self.tokens = min(self.rps, self.tokens + elapsed * self.rps)
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
            time.sleep(0.01)


def _set_global_rps_limiter(limiter: Optional[_RateLimiter]) -> None:
    # Pass the limiter into retrieve module so all HTTP calls respect it
    try:
        from ..retrieve import coinglass as _cg

        _cg.set_rps_limiter(limiter)
    except Exception:
        pass


def watch_retrieve(
    cfg: dict,
    eff: EffectiveConfig,
    *,
    data_root: Path,
    rps: Optional[float],
    workers: int,
    poll_offset_s: float,
    jitter_s: float,
    once: bool,
    log_level: str,
) -> int:
    import threading
    import queue

    logging.basicConfig(level=getattr(logging, log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    LOG = logging.getLogger("cryptostorm.watch")

    limiter = _RateLimiter(rps) if isinstance(rps, (int, float)) and rps and rps > 0 else None
    _set_global_rps_limiter(limiter)

    def _cycle() -> None:
        q: "queue.Queue[str]" = queue.Queue()
        for s in eff.symbols:
            q.put(s)

        def _worker() -> None:
            while True:
                try:
                    sym = q.get_nowait()
                except Exception:
                    break
                try:
                    eff2 = _clone_eff_for_symbols(eff, [sym])
                    _retrieve_once(cfg, eff2, data_root=data_root)
                except Exception as e:  # noqa: BLE001
                    LOG.warning("worker retrieve failed for %s: %s", sym, e)
                finally:
                    try:
                        q.task_done()
                    except Exception:
                        pass

        threads: list[threading.Thread] = []
        for _ in range(max(1, workers)):
            t = threading.Thread(target=_worker, daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()

    def _sleep_until(target_ms: int) -> None:
        now = _utc_now_ms()
        delay = max(0, target_ms - now)
        if jitter_s and jitter_s > 0:
            delay += int(random.uniform(0, float(jitter_s)) * 1000)
        if delay > 0:
            time.sleep(delay / 1000.0)

    if once:
        _cycle()
        return 0

    LOG.info("retrieve --watch started: workers=%d rps=%s offset=%.1fs jitter≤%.1fs", workers, (rps or "-"), poll_offset_s, jitter_s)
    while True:
        now = _utc_now_ms()
        bar_ts = _align_to_5m_close(now)
        target = bar_ts + int(poll_offset_s * 1000)
        if now < target:
            _sleep_until(target)
        try:
            _cycle()
        except Exception as e:  # noqa: BLE001
            LOG.warning("watch cycle failed: %s", e)
        # Sleep to next bar offset
        next_target = _align_to_5m_close(_utc_now_ms()) + 5 * 60 * 1000 + int(poll_offset_s * 1000)
        _sleep_until(next_target)

    return 0
