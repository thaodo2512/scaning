from __future__ import annotations

import argparse
import csv
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple


def _read_file(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    p = Path(path)
    try:
        if p.exists():
            s = p.read_text(encoding="utf-8").strip()
            return s or None
    except Exception:
        return None
    return None


def _utc_iso(ts_ms: int) -> str:
    try:
        import datetime as dt

        return dt.datetime.utcfromtimestamp(int(ts_ms) / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts_ms)


def _send_telegram(token: str, chat_id: str, text: str, *, parse_mode: Optional[str] = None, disable_notification: bool = False) -> None:
    import urllib.parse
    import urllib.request

    base = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {
        "chat_id": chat_id,
        "text": text,
        "disable_notification": str(bool(disable_notification)).lower(),
    }
    if parse_mode:
        data["parse_mode"] = parse_mode
    payload = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(base, data=payload)
    with urllib.request.urlopen(req, timeout=15) as resp:
        # Best-effort read to raise for HTTP errors; ignore body
        resp.read()


def _load_sent_registry(path: Path) -> Dict[str, bool]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return {}


def _save_sent_registry(path: Path, reg: Mapping[str, bool]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(reg, indent=2), encoding="utf-8")
    except Exception:
        pass


def _build_message(run_id: str, sym: str, kind: str, ts_ms: int, score: Optional[float], thr: Optional[float]) -> str:
    emoji = "⚠️" if kind == "pre_alert" else "🌩️" if kind == "storm" else "🔔"
    parts = [
        f"{emoji} {kind.replace('_', ' ').title()} {sym}",
        f"UTC: {_utc_iso(ts_ms)}",
    ]
    if isinstance(score, (int, float)) and isinstance(thr, (int, float)):
        parts.insert(1, f"Score: {score:.3f} vs thr {thr:.3f}")
    parts.append(f"run: {run_id}")
    return "\n".join(parts)


def _iter_alert_rows(alerts_dir: Path, symbols: Sequence[str], kinds: Sequence[str], since_ts: Optional[int]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for sym in symbols:
        fp = alerts_dir / f"{sym}.csv"
        if not fp.exists():
            continue
        try:
            with fp.open("r", encoding="utf-8") as f:
                rdr = csv.DictReader(f)
                for r in rdr:
                    try:
                        ts = int(r.get("ts") or 0)
                    except Exception:
                        ts = 0
                    if since_ts is not None and ts < since_ts:
                        continue
                    k = (r.get("kind") or "").strip() or "pre_alert"
                    if kinds and k not in kinds:
                        continue
                    out.append(r)
        except Exception:
            continue
    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Send CryptoStorm alerts to Telegram")
    parser.add_argument("config", type=str, help="Path to config YAML/JSON")
    parser.add_argument("--artifacts", type=str, help="Artifacts root; defaults to run.artifacts_root/run_id")
    parser.add_argument("--kinds", type=str, default="storm", help="Comma list of kinds to send (storm,pre_alert)")
    parser.add_argument("--since-ts", type=int, default=None, help="Only send alerts with ts >= since (ms)")
    parser.add_argument("--only-new", action="store_true", help="Send only alerts not seen before (persist registry)")
    parser.add_argument("--dry-run", action="store_true", help="Do not send, just print")

    args = parser.parse_args(argv)

    from ..config import load_config, validate_config

    cfg = load_config(args.config)
    eff, warns, errs = validate_config(cfg, require_env=False)
    if errs:
        for e in errs:
            print(f"error: {e}")
        return 2

    run_id = (cfg.get("run") or {}).get("run_id") or eff.run_id
    artifacts_root = Path(args.artifacts) if args.artifacts else Path((cfg.get("run") or {}).get("artifacts_root", "./artifacts")) / str(run_id)
    alerts_dir = artifacts_root / "alerts"

    kinds = [k.strip() for k in (args.kinds or "").split(",") if k.strip()]
    if not kinds:
        kinds = ["storm"]

    # Resolve token/chat from env or files
    token = os.getenv("TELEGRAM_BOT_TOKEN") or _read_file(os.getenv("TELEGRAM_BOT_TOKEN_FILE"))
    chat_id = os.getenv("TELEGRAM_CHAT_ID") or _read_file(os.getenv("TELEGRAM_CHAT_ID_FILE"))
    if not args.dry_run and (not token or not chat_id):
        print("error: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set (or *_FILE)")
        return 2

    rows = _iter_alert_rows(alerts_dir, eff.symbols, kinds, args.since_ts)
    if not rows:
        print("No alerts to send.")
        return 0

    # Dedup registry
    sent_reg_path = artifacts_root / "alerts" / "telegram_sent.json"
    sent = _load_sent_registry(sent_reg_path) if args.only_new else {}

    sent_now = 0
    for r in rows:
        try:
            sym = str(r.get("symbol") or "")
            ts = int(r.get("ts") or 0)
            kind = str(r.get("kind") or "pre_alert")
            key = f"{sym}:{kind}:{ts}"
            if args.only_new and sent.get(key):
                continue
            score = None
            thr = None
            try:
                if r.get("score") not in (None, ""):
                    score = float(r.get("score"))
            except Exception:
                pass
            try:
                if r.get("threshold") not in (None, ""):
                    thr = float(r.get("threshold"))
            except Exception:
                pass
            text = _build_message(run_id, sym, kind, ts, score, thr)
            if args.dry_run:
                print("DRY: ", text)
            else:
                _send_telegram(token=token, chat_id=chat_id, text=text)
                time.sleep(0.2)
            sent[key] = True
            sent_now += 1
        except Exception as e:
            print(f"warn: failed to send alert for row {r}: {e}")
            continue

    if args.only_new:
        _save_sent_registry(sent_reg_path, sent)
    print(f"Sent {sent_now} alerts to Telegram.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

