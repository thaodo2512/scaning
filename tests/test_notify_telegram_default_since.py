from __future__ import annotations

import io
import json
import csv
from pathlib import Path
import tempfile
import unittest
from unittest import mock


class TestTelegramDefaultSince(unittest.TestCase):
    def _write_cfg(self, tdp: Path) -> Path:
        cfg = {
            "run": {"run_id": "r1", "artifacts_root": str(tdp / "artifacts")},
            "universe": {"symbols": ["BTCUSDT"]},
            "acquisition": {
                "days": 1,
                "coinglass": {
                    "intervals": {"futures_ohlcv": "15m"},
                    "per_series_mode": {"futures_ohlcv": "exchange"},
                },
            },
        }
        import yaml  # type: ignore
        p = tdp / "cfg.yaml"
        p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        return p

    def test_sender_defaults_since_from_metrics(self):
        from cryptostorm.notify.telegram import main as tg_main

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cfg_path = self._write_cfg(tdp)
            art = tdp / "artifacts" / "r1"
            (art / "alerts").mkdir(parents=True, exist_ok=True)
            (art / "metrics").mkdir(parents=True, exist_ok=True)

            # Last realtime bar at 1700000000 (seconds) => default since = bar_ms - 15m
            last = {"bar_ts": 1700000000}
            (art / "metrics" / "realtime.jsonl").write_text(json.dumps(last) + "\n", encoding="utf-8")

            # Prepare alerts: one before the window, one inside the window
            step_ms = 15 * 60 * 1000
            bar_ms = last["bar_ts"] * 1000
            since_ms = bar_ms - step_ms
            older = since_ms - 60_000
            newer = since_ms + 60_000

            with (art / "alerts" / "BTCUSDT.csv").open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["ts", "symbol", "kind", "score", "threshold"])
                w.writeheader()
                w.writerow({"ts": older, "symbol": "BTCUSDT", "kind": "storm", "score": 0.6, "threshold": 0.5})
                w.writerow({"ts": newer, "symbol": "BTCUSDT", "kind": "storm", "score": 0.7, "threshold": 0.5})

            sent_payloads: list[dict] = []

            def fake_send(token: str, chat_id: str, text: str, **kwargs):
                sent_payloads.append({"text": text})

            # Provide env credentials so sender takes non-dry path
            with mock.patch("cryptostorm.notify.telegram._send_telegram", side_effect=fake_send):
                with mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "T", "TELEGRAM_CHAT_ID": "1"}, clear=False):
                    # No --since-ts provided; sender should default to last bar_ts with one-bar lookback
                    buf = io.StringIO()
                    with mock.patch("sys.stdout", new=buf):
                        rc = tg_main([str(cfg_path)])
                    self.assertEqual(rc, 0)

            # Only the newer alert should be sent
            self.assertEqual(len(sent_payloads), 1)
            self.assertIn("storm", sent_payloads[0]["text"].lower())


if __name__ == "__main__":
    unittest.main()

