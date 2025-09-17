from __future__ import annotations

import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


class TestTelegramNotifyFilters(unittest.TestCase):
    def _write_cfg(self, tdp: Path, symbols=("AAAUSDT",), run_id: str = "r") -> Path:
        cfg = {
            "run": {"run_id": run_id, "artifacts_root": str(tdp / "artifacts")},
            "universe": {"symbols": list(symbols)},
            # minimal acquisition section to satisfy validator
            "acquisition": {"days": 1, "coinglass": {"intervals": {"futures_ohlcv": "15m"}, "per_series_mode": {"futures_ohlcv": "exchange"}}},
        }
        import yaml  # type: ignore

        p = tdp / "cfg.yaml"
        p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        return p

    def _write_alerts(self, art_root: Path, run_id: str, symbol: str, rows: list[dict]) -> Path:
        adir = art_root / run_id / "alerts"
        adir.mkdir(parents=True, exist_ok=True)
        fp = adir / f"{symbol}.csv"
        with fp.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["ts", "symbol", "kind", "score", "threshold"])
            w.writeheader()
            for r in rows:
                w.writerow(r)
        return fp

    def test_limit_prioritizes_storm_then_newest(self):
        from cryptostorm.notify.telegram import main as tg_main

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            sym = "AAAUSDT"
            cfg_path = self._write_cfg(tdp, symbols=(sym,), run_id="lim")
            # Three alerts: two storms (newer/older) and one pre_alert
            base = 1700000000000
            rows = [
                {"ts": base + 0, "symbol": sym, "kind": "pre_alert", "score": 0.52, "threshold": 0.50},
                {"ts": base + 60000, "symbol": sym, "kind": "storm", "score": 0.60, "threshold": 0.50},
                {"ts": base + 120000, "symbol": sym, "kind": "storm", "score": 0.62, "threshold": 0.50},
            ]
            self._write_alerts(tdp / "artifacts", "lim", sym, rows)

            sent_texts: list[str] = []

            def fake_send(token: str, chat_id: str, text: str, **kwargs):
                sent_texts.append(text)
                return 123

            with mock.patch("cryptostorm.notify.telegram._send_telegram", side_effect=fake_send):
                with mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "T", "TELEGRAM_CHAT_ID": "1"}, clear=False):
                    # limit=2 should pick storms first (newest storm then older storm)
                    buf = io.StringIO()
                    with mock.patch("sys.stdout", new=buf):
                        rc = tg_main([str(cfg_path), "--artifacts", str(tdp / "artifacts" / "lim"), "--kinds", "storm,pre_alert", "--limit", "2"])  # noqa: E501
                    self.assertEqual(rc, 0)
            # We should have exactly two sends, both storms, newest first
            self.assertEqual(len(sent_texts), 2)
            self.assertIn("storm", sent_texts[0].lower())
            self.assertIn("storm", sent_texts[1].lower())

    def test_cooldown_skips_when_recent_sent_in_registry(self):
        from cryptostorm.notify.telegram import main as tg_main

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            sym = "BBBUSDT"
            cfg_path = self._write_cfg(tdp, symbols=(sym,), run_id="cd")
            base = 1700000000000
            # One storm at t=+5m; registry says last sent at +4m; cooldown 10m => skip send (0 calls)
            rows = [
                {"ts": base + 5 * 60 * 1000, "symbol": sym, "kind": "storm", "score": 0.61, "threshold": 0.50},
            ]
            art_root = tdp / "artifacts"
            self._write_alerts(art_root, "cd", sym, rows)
            # Create cooldown registry with recent last_symbol_ts
            reg = {"_meta": {"last_symbol_ts": {sym: base + 4 * 60 * 1000}}}
            reg_fp = art_root / "cd" / "alerts" / "telegram_sent.json"
            reg_fp.parent.mkdir(parents=True, exist_ok=True)
            reg_fp.write_text(json.dumps(reg), encoding="utf-8")

            calls: list[str] = []

            def fake_send(token: str, chat_id: str, text: str, **kwargs):
                calls.append(text)
                return 1

            with mock.patch("cryptostorm.notify.telegram._send_telegram", side_effect=fake_send):
                with mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "T", "TELEGRAM_CHAT_ID": "1"}, clear=False):
                    buf = io.StringIO()
                    with mock.patch("sys.stdout", new=buf):
                        rc = tg_main([str(cfg_path), "--artifacts", str(tdp / "artifacts" / "cd"), "--kinds", "storm", "--cooldown-min", "10"])  # noqa: E501
                    self.assertEqual(rc, 0)
            # Cooldown should prevent sending => 0 calls
            self.assertEqual(len(calls), 0)

    def test_multi_recipients_send_twice(self):
        from cryptostorm.notify.telegram import main as tg_main

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            sym = "CCCUSDT"
            cfg_path = self._write_cfg(tdp, symbols=(sym,), run_id="mr")
            rows = [
                {"ts": 1700000000000, "symbol": sym, "kind": "pre_alert", "score": 0.52, "threshold": 0.50},
            ]
            self._write_alerts(tdp / "artifacts", "mr", sym, rows)

            payloads: list[tuple[str, str]] = []

            def fake_send(token: str, chat_id: str, text: str, **kwargs):
                payloads.append((chat_id, text))
                return 99

            with mock.patch("cryptostorm.notify.telegram._send_telegram", side_effect=fake_send):
                # Two recipients via env (comma and whitespace supported)
                with mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "T", "TELEGRAM_CHAT_ID": "1,2"}, clear=False):
                    buf = io.StringIO()
                    with mock.patch("sys.stdout", new=buf):
                        rc = tg_main([str(cfg_path), "--artifacts", str(tdp / "artifacts" / "mr"), "--kinds", "pre_alert"])
                    self.assertEqual(rc, 0)
            # One alert sent to two recipients => two calls
            self.assertEqual(len(payloads), 2)
            self.assertEqual({c for (c, _t) in payloads}, {"1", "2"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
