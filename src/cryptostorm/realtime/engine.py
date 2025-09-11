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
from pathlib import Path
from typing import Optional
import json


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
        slice_days=0,
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


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="CryptoStorm realtime runner (Phase 1)")
    parser.add_argument("run", nargs="?", default="run", help="Subcommand placeholder (use 'run')")
    parser.add_argument("config", type=str, help="Path to YAML/JSON config")
    parser.add_argument("--data", type=str, default="data")
    parser.add_argument("--features", type=str, default="features")
    parser.add_argument("--artifacts", type=str, help="Artifacts root override")
    parser.add_argument("--poll-offset-s", type=float, default=10.0)
    parser.add_argument("--jitter-s", type=float, default=2.0)
    parser.add_argument("--once", action="store_true", help="Run a single cycle immediately")
    parser.add_argument("--log-level", type=str, default="INFO")
    parser.add_argument("--send-telegram", action="store_true")
    parser.add_argument("--telegram-kinds", type=str, default="storm")
    parser.add_argument("--online-scoring", action="store_true", help="Use online scoring (no retrain) if artifacts present")
    parser.add_argument("--build-reports", action="store_true", help="Build reports after each cycle and update index.html")
    parser.add_argument("--reports", type=str, default="reports", help="Reports output directory")
    parser.add_argument("--report-engine", type=str, choices=["plotly", "lightweight", "price"], default="price")
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

    if args.once:
        _cycle_start = time.monotonic()
        bar_ts = _align_to_5m_close(_utc_now_ms())
        # Timings
        t0 = time.monotonic()
        _retrieve_once(cfg, eff, data_root=data_root)
        t1 = time.monotonic()
        update_features_last(eff, data_root=data_root, out_root=features_root)
        t2 = time.monotonic()
        if args.online_scoring:
            from ..backtest.engine import score_online

            score_online(cfg, eff, features_root=features_root, out_root=artifacts_root)
        else:
            run_backtest(cfg, eff, features_root=features_root, out_root=artifacts_root)
        t3 = time.monotonic()
        # Optional alerts
        if args.send_telegram:
            from ..notify.telegram import main as telegram_main

            try:
                telegram_main([cfg.get("_path", ""), "--artifacts", str(artifacts_root), "--kinds", args.telegram_kinds, "--only-new"])  # type: ignore[arg-type]
            except Exception:
                pass
        # Optional reports
        if args.build_reports:
            try:
                reports_root = Path(args.reports)
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
        now = _utc_now_ms()
        bar_ts = _align_to_5m_close(now)
        target = bar_ts + int(args.poll_offset_s * 1000)
        if now < target:
            _sleep_until(target, args.jitter_s)
        try:
            _cycle_start = time.monotonic()
            # Step timings
            t0 = time.monotonic()
            _retrieve_once(cfg, eff, data_root=data_root)
            t1 = time.monotonic()
            update_features_last(eff, data_root=data_root, out_root=features_root)
            t2 = time.monotonic()
            if args.online_scoring:
                from ..backtest.engine import score_online

                score_online(cfg, eff, features_root=features_root, out_root=artifacts_root)
            else:
                run_backtest(cfg, eff, features_root=features_root, out_root=artifacts_root)
            t3 = time.monotonic()
            if args.send_telegram:
                from ..notify.telegram import main as telegram_main

                try:
                    telegram_main([cfg.get("_path", ""), "--artifacts", str(artifacts_root), "--kinds", args.telegram_kinds, "--only-new"])  # type: ignore[arg-type]
                except Exception:
                    pass
            # Optional reports
            if args.build_reports:
                try:
                    reports_root = Path(args.reports)
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
        next_target = _align_to_5m_close(_utc_now_ms()) + 5 * 60 * 1000 + int(args.poll_offset_s * 1000)
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
