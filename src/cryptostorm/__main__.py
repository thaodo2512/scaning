from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Optional

from .config import load_config, validate_config, main as config_main
from .retrieve.coinglass import run_retrieve


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

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

