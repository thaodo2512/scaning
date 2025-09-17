from __future__ import annotations

import csv
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


class TestTelegramDefaultArtifactsFallback(unittest.TestCase):
    def test_fallback_to_top_realtime_when_run_dir_missing(self):
        from cryptostorm.notify.telegram import main as tg_main

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cwd0 = Path.cwd()
            os.chdir(tdp)
            try:
                # Config points run_id to a directory that won't exist
                cfg = {
                    "run": {"run_id": "missing_run", "artifacts_root": "./artifacts"},
                    "universe": {"symbols": ["ZZZUSDT"]},
                    "acquisition": {"days": 1, "coinglass": {"intervals": {"futures_ohlcv": "15m"}, "per_series_mode": {"futures_ohlcv": "exchange"}}},
                    "notifications": {"telegram": {"kinds": "storm"}},
                }
                import yaml  # type: ignore

                cfgp = tdp / "cfg.yaml"
                cfgp.write_text(yaml.safe_dump(cfg), encoding="utf-8")

                # Prepare alerts under artifacts/top_realtime (fallback target)
                adir = tdp / "artifacts" / "top_realtime" / "alerts"
                adir.mkdir(parents=True, exist_ok=True)
                with (adir / "ZZZUSDT.csv").open("w", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=["ts", "symbol", "kind", "score", "threshold"])
                    w.writeheader()
                    w.writerow({"ts": 1700000000000, "symbol": "ZZZUSDT", "kind": "storm", "score": 0.6, "threshold": 0.5})

                # Dry run should pick up fallback and print one message
                buf = io.StringIO()
                with mock.patch("sys.stdout", new=buf):
                    rc = tg_main([str(cfgp), "--dry-run"])  # no --artifacts provided
                out = buf.getvalue()
                self.assertEqual(rc, 0)
                self.assertIn("DRY:", out)
                self.assertIn("ZZZUSDT", out)
            finally:
                os.chdir(cwd0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

