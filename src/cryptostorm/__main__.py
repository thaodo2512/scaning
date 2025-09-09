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

    p_aud = sub.add_parser("audit", help="Audit data coverage for the 30d window")
    p_aud.add_argument("config", type=str)
    p_aud.add_argument("--data", type=str, default="data")
    p_aud.add_argument("--min-ratio", type=float, default=0.95)

    p_feat = sub.add_parser("feature", help="Build 5m feature tables from raw data")
    p_feat.add_argument("config", type=str)
    p_feat.add_argument("--data", type=str, default="data")
    p_feat.add_argument("--out", type=str, default="features")

    p_bt = sub.add_parser("backtest", help="Run walk-forward IsolationForest backtest")
    p_bt.add_argument("config", type=str)
    p_bt.add_argument("--features", type=str, default="features")
    p_bt.add_argument("--artifacts-root", type=str)

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
        )
        return 0

    if args.cmd == "audit":
        return audit_main([args.config, "--data", args.data, "--min-ratio", str(args.min_ratio)])

    if args.cmd == "feature":
        return feature_main([args.config, "--data", args.data, "--out", args.out])

    if args.cmd == "backtest":
        argv = [args.config, "--features", args.features]
        if args.artifacts_root:
            argv += ["--artifacts-root", args.artifacts_root]
        return backtest_main(argv)

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
