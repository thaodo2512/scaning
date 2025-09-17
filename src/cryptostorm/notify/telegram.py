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


def _send_telegram(token: str, chat_id: str, text: str, *, parse_mode: Optional[str] = None, disable_notification: bool = False) -> Optional[int]:
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
        body = resp.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
            # Telegram returns { ok: true, result: { message_id: ..., ... } }
            if isinstance(data, dict) and isinstance(data.get("result"), dict):
                mid = data["result"].get("message_id")
                if isinstance(mid, int):
                    return mid
        except Exception:
            # Ignore parse errors; treat as sent without ID
            pass
    return None


def _parse_chat_ids_from_str(s: Optional[str]) -> List[str]:
    if not s:
        return []
    # Accept comma or newline separated
    parts = [p.strip() for p in str(s).replace("\n", ",").split(",")]
    return [p for p in parts if p]


def _parse_chat_ids_from_file(path: Optional[str]) -> List[str]:
    txt = _read_file(path)
    return _parse_chat_ids_from_str(txt)


def _append_send_log(path: Path, entry: Mapping[str, object]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except Exception:
        # best-effort; never raise from logging
        pass


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


def _build_message(run_id: str, sym: str, kind: str, ts_ms: int, score: Optional[float], thr: Optional[float], *, include_json: bool = False) -> str:
    emoji = "⚠️" if kind == "pre_alert" else "🌩️" if kind == "storm" else "🔔"
    parts = [
        f"{emoji} {kind.replace('_', ' ').title()} {sym}",
        f"UTC: {_utc_iso(ts_ms)}",
    ]
    if isinstance(score, (int, float)) and isinstance(thr, (int, float)):
        parts.insert(1, f"Score: {score:.3f} vs thr {thr:.3f}")
    parts.append(f"run: {run_id}")
    if include_json:
        import json as _json
        ctx = {
            "symbol": sym,
            "kind": kind,
            "ts": int(ts_ms),
            "ts_iso": _utc_iso(ts_ms),
            "score": (float(score) if isinstance(score, (int, float)) else None),
            "threshold": (float(thr) if isinstance(thr, (int, float)) else None),
            "run_id": run_id,
        }
        # Append a compact one-line JSON for easy copy/paste to AI tools
        parts.append(_json.dumps(ctx, separators=(",", ":")))
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
    parser.add_argument("--include-json", action="store_true", help="Append a compact one-line JSON context for AI copy/paste")
    parser.add_argument("--limit", type=int, default=None, help="Max alerts to send this run (newest with storm priority)")
    parser.add_argument("--cooldown-min", type=int, default=None, help="Per-symbol cooldown minutes (skip alerts sent recently)")

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
    # Convenience fallback: if no --artifacts was provided and the computed
    # run_id path does not exist, but artifacts/top_realtime exists, use it.
    if not args.artifacts and not artifacts_root.exists():
        fb = Path("./artifacts/top_realtime")
        try:
            if fb.exists():
                artifacts_root = fb
        except Exception:
            pass
    alerts_dir = artifacts_root / "alerts"

    # Load telegram section from config for defaults/overrides
    tg_cfg = ((cfg.get("notifications") or {}).get("telegram") or {}) if isinstance(cfg.get("notifications"), dict) else {}

    # Effective flags: CLI wins when explicitly provided; otherwise use config
    kinds_arg = args.kinds
    kinds_cfg = str(tg_cfg.get("kinds") or "").strip()
    # If CLI provided default 'storm' and config overrides, prefer config
    kinds_eff_str = kinds_arg if (kinds_arg and (kinds_arg != "storm" or not kinds_cfg)) else (kinds_cfg or kinds_arg)
    kinds = [k.strip() for k in (kinds_eff_str or "").split(",") if k.strip()]
    if not kinds:
        kinds = ["storm"]

    # Resolve token/chat from env or files or config
    token = os.getenv("TELEGRAM_BOT_TOKEN") or _read_file(os.getenv("TELEGRAM_BOT_TOKEN_FILE"))
    chat_env = os.getenv("TELEGRAM_CHAT_ID") or _read_file(os.getenv("TELEGRAM_CHAT_ID_FILE"))
    if not token:
        token = (tg_cfg.get("bot_token") if isinstance(tg_cfg.get("bot_token"), str) else None) or _read_file(tg_cfg.get("bot_token_file"))
    # Build recipients list from env, files, or config
    recipients: List[str] = []
    # 1) Env value or file (comma/newline separated supported)
    recipients.extend(_parse_chat_ids_from_str(chat_env))
    if not recipients:
        recipients.extend(_parse_chat_ids_from_file(os.getenv("TELEGRAM_CHAT_ID_FILE")))
    # 2) Config list chat_ids or file
    chat_ids_cfg = tg_cfg.get("chat_ids") if isinstance(tg_cfg, dict) else None
    if isinstance(chat_ids_cfg, list):
        for v in chat_ids_cfg:
            try:
                if isinstance(v, (str, int)):
                    recipients.append(str(v).strip())
            except Exception:
                continue
    if not recipients:
        recipients.extend(_parse_chat_ids_from_file(tg_cfg.get("chat_ids_file")))
    # 3) Fallback single chat_id or chat_id_file
    if not recipients:
        chat_id = (tg_cfg.get("chat_id") if isinstance(tg_cfg.get("chat_id"), str) else None) or _read_file(tg_cfg.get("chat_id_file"))
        recipients.extend(_parse_chat_ids_from_str(chat_id))

    # Effective dry_run and since_ts / only_new
    eff_dry = bool(args.dry_run or bool(tg_cfg.get("dry_run")))
    eff_since_ts = args.since_ts if args.since_ts is not None else (int(tg_cfg.get("since_ts")) if isinstance(tg_cfg.get("since_ts"), (int, float)) else None)

    # Sensible default: if since_ts not provided, read last realtime bar_ts from metrics and use that (ms)
    if eff_since_ts is None:
        try:
            mfp = alerts_dir.parent / "metrics" / "realtime.jsonl"
            if mfp.exists():
                last = None
                with mfp.open("r", encoding="utf-8") as f:
                    lines = f.read().splitlines()
                    if lines:
                        last = json.loads(lines[-1])
                if isinstance(last, dict) and last.get("bar_ts"):
                    step_ms = 15 * 60 * 1000
                    try:
                        # Infer step from config if possible
                        iv = ((cfg.get("acquisition") or {}).get("coinglass") or {}).get("intervals") or {}
                        fut_iv = str(iv.get("futures_ohlcv") or "15m").lower()
                        if fut_iv.endswith("m") and fut_iv[:-1].isdigit():
                            step_ms = int(fut_iv[:-1]) * 60 * 1000
                    except Exception:
                        pass
                    # Default one-bar lookback to cover slow cycles (dedup prevents repeats)
                    lookback_bars = 1
                    eff_since_ts = int(last["bar_ts"]) * 1000 - lookback_bars * step_ms
        except Exception:
            eff_since_ts = None
    eff_only_new = bool(args.only_new or bool(tg_cfg.get("only_new")))
    eff_include_json = bool(args.include_json or bool(tg_cfg.get("include_json")))
    eff_limit = int(args.limit) if args.limit is not None else (int(tg_cfg.get("limit")) if isinstance(tg_cfg.get("limit"), (int, float)) else None)
    eff_cooldown_min = int(args.cooldown_min) if args.cooldown_min is not None else (int(tg_cfg.get("cooldown_min")) if isinstance(tg_cfg.get("cooldown_min"), (int, float)) else None)

    # Deduplicate and sanitize recipients
    recipients = [r for r in {r for r in recipients if r}]

    if not eff_dry and (not token or not recipients):
        print("error: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID(S) must be set (or *_FILE / config)")
        return 2

    rows = _iter_alert_rows(alerts_dir, eff.symbols, kinds, eff_since_ts)
    if not rows:
        print("No alerts to send.")
        return 0

    # Dedup registry (also used for cooldown metadata)
    sent_reg_path = artifacts_root / "alerts" / "telegram_sent.json"
    sent = _load_sent_registry(sent_reg_path) if (eff_only_new or eff_cooldown_min) else {}
    meta = {}
    try:
        meta = dict(sent.get("_meta", {})) if isinstance(sent, dict) else {}
    except Exception:
        meta = {}
    last_by_symbol = {}
    try:
        last_by_symbol = dict(meta.get("last_symbol_ts", {})) if isinstance(meta, dict) else {}
    except Exception:
        last_by_symbol = {}

    # First pass: build candidate list with only-new and cooldown filtering
    cooldown_ms = int(eff_cooldown_min) * 60 * 1000 if isinstance(eff_cooldown_min, int) and eff_cooldown_min > 0 else None
    candidates: List[dict] = []
    for r in rows:
        try:
            sym = str(r.get("symbol") or "")
            ts = int(r.get("ts") or 0)
            kind = str(r.get("kind") or "pre_alert")
            key = f"{sym}:{kind}:{ts}"
            if eff_only_new and isinstance(sent, dict) and sent.get(key):
                continue
            if cooldown_ms is not None:
                last_ts = last_by_symbol.get(sym)
                if isinstance(last_ts, (int, float)) and (ts - int(last_ts)) < cooldown_ms:
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
            candidates.append({"sym": sym, "ts": ts, "kind": kind, "score": score, "thr": thr, "key": key})
        except Exception as e:
            print(f"warn: failed to prepare alert row {r}: {e}")
            continue

    # Apply limit: prioritize storm, then newest
    def _prio(k: str) -> int:
        if k == "storm":
            return 0
        if k == "pre_alert":
            return 1
        return 2

    candidates.sort(key=lambda x: (_prio(x["kind"]), -int(x["ts"])))
    if isinstance(eff_limit, int) and eff_limit > 0 and len(candidates) > eff_limit:
        candidates = candidates[: eff_limit]

    # Send
    sent_now = 0
    for it in candidates:
        sym = it["sym"]
        ts = int(it["ts"])
        kind = it["kind"]
        score = it["score"]
        thr = it["thr"]
        key = it["key"]
        try:
            text = _build_message(run_id, sym, kind, ts, score, thr, include_json=eff_include_json)
            if eff_dry:
                print("DRY: ", text)
            else:
                # Send to all recipients
                last_mid: Optional[int] = None
                for rcp in recipients:
                    try:
                        mid = _send_telegram(token=token, chat_id=rcp, text=text)
                        last_mid = mid or last_mid
                        # Append a send log entry (what was actually sent)
                        try:
                            now_ms = int(time.time() * 1000)
                            entry = {
                                "sent_at_ms": now_ms,
                                "sent_at_iso": _utc_iso(now_ms),
                                "run_id": run_id,
                                "symbol": sym,
                                "kind": kind,
                                "bar_ts": ts,
                                "chat_id": rcp,
                                "score": (float(score) if isinstance(score, (int, float)) else None),
                                "threshold": (float(thr) if isinstance(thr, (int, float)) else None),
                                "message_id": (int(mid) if isinstance(mid, int) else None),
                                "text_len": len(text),
                            }
                            _append_send_log(artifacts_root / "alerts" / "telegram_send.jsonl", entry)
                        except Exception:
                            pass
                        time.sleep(0.2)
                    except Exception as e:
                        print(f"warn: failed to send alert to {rcp} for {sym} {kind} {ts}: {e}")
                        continue
            if isinstance(sent, dict):
                sent[key] = True
            last_by_symbol[sym] = ts
            sent_now += 1
        except Exception as e:
            print(f"warn: failed to send alert for {sym} {kind} {ts}: {e}")
            continue

    # Persist registry/meta if used
    if isinstance(sent, dict) and (eff_only_new or eff_cooldown_min):
        try:
            meta["last_symbol_ts"] = last_by_symbol
            sent["_meta"] = meta  # type: ignore[index]
        except Exception:
            pass
        _save_sent_registry(sent_reg_path, sent)
    print(f"Sent {sent_now} alerts to Telegram.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
