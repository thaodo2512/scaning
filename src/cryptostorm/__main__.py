from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Optional

from .config import load_config, validate_config, main as config_main
from .retrieve.coinglass import run_retrieve
from .audit import main as audit_main
from .feature.engine import main as feature_main
from .backtest.engine import main as backtest_main
from .report.engine import main as report_main
from .report.price_alert import main as price_alert_main
from .report.plotly_full import main as plotly_full_main
from .notify.telegram import main as telegram_notify_main
from .realtime.engine import main as realtime_main
from .api.server import main as api_main
from .storage.export import main as storage_main
from .monitor.console import main as monitor_main


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="cryptostorm", description="CryptoStorm CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_val = sub.add_parser("validate", help="Validate a config and print summary")
    p_val.add_argument("config", type=str)
    p_val.add_argument("--no-require-env", action="store_true")

    p_ret = sub.add_parser("retrieve", help="Run retrieval stage (Coinglass)")
    p_ret.add_argument("config", type=str)
    p_ret.add_argument("--out", type=str, default="data")
    p_ret.add_argument("--dry-run", action="store_true")
    p_ret.add_argument("--log-level", type=str, default="INFO")
    p_ret.add_argument("--slice-days", type=int, default=None)
    # Watch mode (realtime retrieve only)
    p_ret.add_argument("--watch", action="store_true")
    p_ret.add_argument("--poll-offset-s", type=float, default=10.0)
    p_ret.add_argument("--jitter-s", type=float, default=2.0)
    p_ret.add_argument("--rps", type=float, default=None)
    p_ret.add_argument("--workers", type=int, default=1)
    p_ret.add_argument("--once", action="store_true")

    p_aud = sub.add_parser("audit", help="Audit data coverage for the 30d window")
    p_aud.add_argument("config", type=str)
    p_aud.add_argument("--data", type=str, default="data")
    p_aud.add_argument("--min-ratio", type=float, default=0.95)

    p_feat = sub.add_parser("feature", help="Build 5m feature tables from raw data")
    p_feat.add_argument("config", type=str)
    p_feat.add_argument("--data", type=str, default="data")
    p_feat.add_argument("--out", type=str, default="features")
    p_feat.add_argument("--log-level", type=str, default="INFO")

    p_bt = sub.add_parser("backtest", help="Run walk-forward IsolationForest backtest")
    p_bt.add_argument("config", type=str)
    p_bt.add_argument("--features", type=str, default="features")
    p_bt.add_argument("--artifacts-root", type=str)

    p_rep = sub.add_parser("report", help="Generate per-symbol HTML reports")
    p_rep.add_argument("config", type=str)
    p_rep.add_argument("--data", type=str, default="data")
    p_rep.add_argument("--features", type=str, default="features")
    p_rep.add_argument("--artifacts", type=str)
    p_rep.add_argument("--out", type=str, default="reports")

    p_rep2 = sub.add_parser("report-price", help="Generate price+alerts Plotly reports (no lightweight-charts)")
    p_rep2.add_argument("config", type=str)
    p_rep2.add_argument("--data", type=str, default="data")
    p_rep2.add_argument("--artifacts", type=str)
    p_rep2.add_argument("--out", type=str, default="reports")

    p_rep3 = sub.add_parser("report-plotly", help="Generate full Plotly reports (candles, score, OI+liq)")
    p_rep3.add_argument("config", type=str)
    p_rep3.add_argument("--data", type=str, default="data")
    p_rep3.add_argument("--artifacts", type=str)
    p_rep3.add_argument("--out", type=str, default="reports")

    p_tel = sub.add_parser("alert-telegram", help="Send alerts to Telegram from artifacts/<RUN_ID>/alerts")
    p_tel.add_argument("config", type=str)
    p_tel.add_argument("--artifacts", type=str)
    p_tel.add_argument("--kinds", type=str, default="storm")
    p_tel.add_argument("--since-ts", type=int, default=None)
    p_tel.add_argument("--only-new", action="store_true")
    p_tel.add_argument("--dry-run", action="store_true")

    p_rt = sub.add_parser("realtime", help="Realtime loop: retrieve → features(update-last) → backtest → (optional) alerts")
    p_rt.add_argument("config", type=str)
    p_rt.add_argument("--data", type=str, default="data")
    p_rt.add_argument("--features", type=str, default="features")
    p_rt.add_argument("--artifacts", type=str)
    p_rt.add_argument("--poll-offset-s", type=float, default=10.0)
    p_rt.add_argument("--jitter-s", type=float, default=2.0)
    p_rt.add_argument("--once", action="store_true")
    p_rt.add_argument("--log-level", type=str, default="INFO")
    p_rt.add_argument("--send-telegram", action="store_true")
    p_rt.add_argument("--telegram-kinds", type=str, default="storm")
    p_rt.add_argument("--online-scoring", action="store_true")
    p_rt.add_argument("--build-reports", action="store_true")
    p_rt.add_argument("--reports", type=str, default="reports")
    p_rt.add_argument("--report-engine", type=str, choices=["plotly", "lightweight", "price"], default="price")

    p_api = sub.add_parser("api", help="Run FastAPI server for realtime scores/alerts")
    p_api.add_argument("config", type=str)
    p_api.add_argument("--data", type=str, default="data")
    p_api.add_argument("--features", type=str, default="features")
    p_api.add_argument("--artifacts", type=str)
    p_api.add_argument("--reports", type=str, default="reports")
    p_api.add_argument("--host", type=str, default="127.0.0.1")
    p_api.add_argument("--port", type=int, default=8000)
    p_api.add_argument("--token", type=str, default=None)

    p_store = sub.add_parser("storage", help="Storage/export utilities (Parquet, DDL)")
    p_store.add_argument("sub", choices=["export-parquet", "emit-ddl"], help="Subcommand")
    p_store.add_argument("config", type=str)
    p_store.add_argument("--data", type=str, default="data")
    p_store.add_argument("--features", type=str, default="features")
    p_store.add_argument("--out", type=str)
    p_store.add_argument("--kind", type=str, default="clickhouse")

    p_mon = sub.add_parser("monitor", help="Console dashboard for realtime system")
    p_mon.add_argument("config", type=str)
    p_mon.add_argument("--data", type=str, default="data")
    p_mon.add_argument("--features", type=str, default="features")
    p_mon.add_argument("--artifacts", type=str)
    p_mon.add_argument("--datasets", type=str, default="futures_ohlcv_5m,oi_5m_ohlc,orderbook_futures_5m")
    p_mon.add_argument("--symbols", type=int, default=20)
    p_mon.add_argument("--refresh-s", type=float, default=2.0)
    p_mon.add_argument("--view", type=str, choices=["data", "alerts"], default="data")
    p_mon.add_argument("--debug", action="store_true")

    args = parser.parse_args(argv)

    if args.cmd == "validate":
        # Delegate to config module CLI for consistency
        import sys

        return config_main(["validate", args.config] + (["--no-require-env"] if args.no_require_env else []))

    if args.cmd == "retrieve":
        logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        cfg = load_config(args.config)
        eff, warns, errs = validate_config(cfg, require_env=not args.dry_run)
        for w in warns:
            logging.getLogger("cryptostorm").warning(w)
        if errs:
            for e in errs:
                logging.getLogger("cryptostorm").error(e)
            return 2
        # Watch mode: parallel, rate-limited retrieve only
        if args.watch:
            from .realtime.engine import watch_retrieve
            return watch_retrieve(
                cfg,
                eff,
                data_root=Path(args.out),
                rps=(float(args.rps) if args.rps is not None else None),
                workers=max(1, int(args.workers)),
                poll_offset_s=float(args.poll_offset_s),
                jitter_s=float(args.jitter_s),
                once=bool(args.once),
                log_level=args.log_level,
            )
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
        out_root = Path(args.out)
        run_retrieve(
            eff,
            base_url=base_url,
            v3_base_url=v3_base_url,
            exchange=exchange,
            quote=quote,
            page_limit=page_limit,
            backoff_initial=backoff_initial,
            backoff_max=backoff_max,
            out_root=out_root,
            dry_run=args.dry_run,
            api_key=None,
            slice_days=(args.slice_days if args.slice_days is not None else int(cg.get("slice_days", 0) or 0)),
            force_v3=list(cg.get("force_v3", []) or []),
            orderbook_time_enum=cg.get("orderbook_time_enum") or ("LAST_OF_5M" if (cg.get("intervals", {}) or {}).get("orderbook_sample") == "last_of_5m" else None),
        )
        return 0

    if args.cmd == "audit":
        return audit_main([args.config, "--data", args.data, "--min-ratio", str(args.min_ratio)])

    if args.cmd == "feature":
        return feature_main([args.config, "--data", args.data, "--out", args.out, "--log-level", args.log_level])

    if args.cmd == "backtest":
        argv = [args.config, "--features", args.features]
        if args.artifacts_root:
            argv += ["--artifacts-root", args.artifacts_root]
        return backtest_main(argv)

    if args.cmd == "report":
        argv = [args.config, "--data", args.data, "--features", args.features, "--out", args.out]
        if args.artifacts:
            argv += ["--artifacts", args.artifacts]
        return report_main(argv)
    if args.cmd == "report-price":
        argv = [args.config, "--data", args.data, "--out", args.out]
        if args.artifacts:
            argv += ["--artifacts", args.artifacts]
        return price_alert_main(argv)
    if args.cmd == "report-plotly":
        argv = [args.config, "--data", args.data, "--out", args.out]
        if args.artifacts:
            argv += ["--artifacts", args.artifacts]
        return plotly_full_main(argv)
    if args.cmd == "alert-telegram":
        argv = [args.config]
        if args.artifacts:
            argv += ["--artifacts", args.artifacts]
        if args.kinds:
            argv += ["--kinds", args.kinds]
        if args.since_ts is not None:
            argv += ["--since-ts", str(args.since_ts)]
        if args.only_new:
            argv += ["--only-new"]
        if args.dry_run:
            argv += ["--dry-run"]
        return telegram_notify_main(argv)

    if args.cmd == "realtime":
        argv = [
            "run",
            args.config,
            "--data",
            args.data,
            "--features",
            args.features,
            "--poll-offset-s",
            str(args.poll_offset_s),
            "--jitter-s",
            str(args.jitter_s),
            "--log-level",
            args.log_level,
        ]
        if args.artifacts:
            argv += ["--artifacts", args.artifacts]
        if args.once:
            argv += ["--once"]
        if args.send_telegram:
            argv += ["--send-telegram", "--telegram-kinds", args.telegram_kinds]
        if args.online_scoring:
            argv += ["--online-scoring"]
        if args.build_reports:
            argv += ["--build-reports", "--reports", args.reports, "--report-engine", args.report_engine]
        return realtime_main(argv)

    if args.cmd == "api":
        argv = [
            args.config,
            "--data", args.data,
            "--features", args.features,
            "--reports", args.reports,
            "--host", args.host,
            "--port", str(args.port),
        ]
        if args.artifacts:
            argv += ["--artifacts", args.artifacts]
        if args.token:
            argv += ["--token", args.token]
        return api_main(argv)

    if args.cmd == "storage":
        if args.sub == "export-parquet":
            argv = [
                "export-parquet",
                args.config,
                "--data", args.data,
                "--features", args.features,
            ] + (["--out", args.out] if args.out else [])
        else:
            argv = [
                "emit-ddl",
                args.config,
                "--kind", args.kind,
            ] + (["--out", args.out] if args.out else [])
        return storage_main(argv)

    if args.cmd == "monitor":
        argv = [
            args.config,
            "--data", args.data,
            "--features", args.features,
            "--datasets", args.datasets,
            "--symbols", str(args.symbols),
            "--refresh-s", str(args.refresh_s),
            "--view", args.view,
        ]
        if args.artifacts:
            argv += ["--artifacts", args.artifacts]
        if args.debug:
            argv += ["--debug"]
        return monitor_main(argv)

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
