from __future__ import annotations

import argparse
import logging
import os
import random
import time
from pathlib import Path
from typing import Optional

from ..config import load_config, validate_config, EffectiveConfig
from ..retrieve.coinglass import run_retrieve
from ..feature.engine import update_features_last
from ..backtest.engine import run_backtest


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
        run_once(cfg, eff, data_root=data_root, features_root=features_root, artifacts_root=artifacts_root, send_telegram=args.send_telegram, telegram_kinds=args.telegram_kinds)
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
            run_once(cfg, eff, data_root=data_root, features_root=features_root, artifacts_root=artifacts_root, send_telegram=args.send_telegram, telegram_kinds=args.telegram_kinds)
        except Exception as e:  # noqa: BLE001
            LOG.warning("Realtime cycle failed: %s", e)
        # Sleep until next bar's offset
        next_target = _align_to_5m_close(_utc_now_ms()) + 5 * 60 * 1000 + int(args.poll_offset_s * 1000)
        _sleep_until(next_target, args.jitter_s)

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
