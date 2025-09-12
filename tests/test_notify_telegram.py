from __future__ import annotations

import io
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


class TestTelegramNotify(unittest.TestCase):
    def _write_cfg(self, tdp: Path, symbols=("BTCUSDT",), run_id: str = "test-run") -> Path:
        cfg = {
            "run": {"run_id": run_id, "artifacts_root": str(tdp / "artifacts")},
            "universe": {"symbols": list(symbols)},
            # minimal acquisition section to satisfy validator
            "acquisition": {"days": 1, "coinglass": {"intervals": {"futures_ohlcv": "5m"}, "per_series_mode": {"futures_ohlcv": "exchange"}}},
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

    def test_dry_run_prints_messages(self):
        from cryptostorm.notify.telegram import main as tg_main

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cfg_path = self._write_cfg(tdp, symbols=("BTCUSDT",), run_id="dry")
            # Prepare one storm alert with score/threshold
            rows = [
                {"ts": 1700000000000, "symbol": "BTCUSDT", "kind": "storm", "score": 0.55, "threshold": 0.5},
            ]
            self._write_alerts(tdp / "artifacts", "dry", "BTCUSDT", rows)

            buf = io.StringIO()
            with mock.patch("sys.stdout", new=buf):
                rc = tg_main([str(cfg_path), "--artifacts", str(tdp / "artifacts" / "dry"), "--dry-run", "--kinds", "storm"])
            out = buf.getvalue()
            self.assertEqual(rc, 0)
            # Expect DRY output and the kind text; include run id
            self.assertIn("DRY:", out)
            # look for kind in a case-insensitive manner
            self.assertIn("storm", out.lower())
            self.assertIn("BTCUSDT", out)
            self.assertIn("run: dry", out)

    def test_only_new_registry_prevents_resend(self):
        from cryptostorm.notify.telegram import main as tg_main

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cfg_path = self._write_cfg(tdp, symbols=("BTCUSDT",), run_id="reg")
            rows = [
                {"ts": 1700000000000, "symbol": "BTCUSDT", "kind": "storm", "score": 0.6, "threshold": 0.5},
                {"ts": 1700000005000, "symbol": "BTCUSDT", "kind": "pre_alert", "score": 0.52, "threshold": 0.5},
            ]
            self._write_alerts(tdp / "artifacts", "reg", "BTCUSDT", rows)

            # Mock actual network call to Telegram
            sent_calls: list[dict] = []

            def fake_send(token: str, chat_id: str, text: str, parse_mode=None, disable_notification=False):
                sent_calls.append({"token": token, "chat_id": chat_id, "text": text})

            with mock.patch("cryptostorm.notify.telegram._send_telegram", side_effect=fake_send):
                # Provide credentials via env so the code takes the non-dry path
                with mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "TKN", "TELEGRAM_CHAT_ID": "123"}, clear=False):
                    # First run should send both
                    buf1 = io.StringIO()
                    with mock.patch("sys.stdout", new=buf1):
                        rc1 = tg_main([str(cfg_path), "--artifacts", str(tdp / "artifacts" / "reg"), "--only-new", "--kinds", "storm,pre_alert"])
                    out1 = buf1.getvalue()
                    self.assertEqual(rc1, 0)
                    self.assertIn("Sent 2 alerts", out1)
                    self.assertEqual(len(sent_calls), 2)

                    # Second run should detect registry and send 0
                    buf2 = io.StringIO()
                    with mock.patch("sys.stdout", new=buf2):
                        rc2 = tg_main([str(cfg_path), "--artifacts", str(tdp / "artifacts" / "reg"), "--only-new", "--kinds", "storm,pre_alert"])
                    out2 = buf2.getvalue()
                    self.assertEqual(rc2, 0)
                    self.assertIn("Sent 0 alerts", out2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
