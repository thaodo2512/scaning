from __future__ import annotations

import tempfile
from pathlib import Path
import unittest
import json
import csv

from cryptostorm.report.price_alert import main as price_report_main


class TestReportEvalPanel(unittest.TestCase):
    def test_eval_panel_rendered(self):
        with tempfile.TemporaryDirectory() as td:
            tdpath = Path(td)
            # Minimal config with labels
            cfg = {
                "run": {"run_id": "r1", "artifacts_root": str(tdpath / "artifacts")},
                "universe": {"symbols": ["BTCUSDT"]},
                "labels": {"pct_move": 0.05, "horizons_min": [30, 60]},
            }
            import yaml  # type: ignore
            cfg_path = tdpath / "cfg.yaml"
            cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

            # Data: two 15m closes
            data_dir = tdpath / "data" / "BTCUSDT"
            data_dir.mkdir(parents=True, exist_ok=True)
            fut = data_dir / "futures_ohlcv_15m.jsonl"
            fut.write_text(
                "\n".join([
                    json.dumps({"ts": 1700000000000, "payload": {"close": 1}}),
                    json.dumps({"ts": 1700000900000, "payload": {"close": 1.1}}),
                ]) + "\n",
                encoding="utf-8",
            )

            # Alerts with one storm
            art = tdpath / "artifacts" / "r1"
            (art / "alerts").mkdir(parents=True, exist_ok=True)
            with (art / "alerts" / "BTCUSDT.csv").open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["ts", "symbol", "kind", "score", "threshold"])
                w.writeheader()
                w.writerow({"ts": 1700000000000, "symbol": "BTCUSDT", "kind": "storm", "score": 0.6, "threshold": 0.5})

            # Metrics file with per-symbol stats
            (art / "metrics").mkdir(parents=True, exist_ok=True)
            metrics = {
                "symbols": {"BTCUSDT": {"storm_count": 1, "true_positive": 1, "precision": 1.0, "avg_lead_min": 30}},
                "summary": {"storm_count": 1, "true_positive": 1, "precision": 1.0},
            }
            (art / "metrics" / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")

            out = tdpath / "reports"
            rc = price_report_main([str(cfg_path), "--data", str(tdpath / "data"), "--artifacts", str(art), "--out", str(out)])
            self.assertEqual(rc, 0)
            html = (out / "BTCUSDT_price_alert.html").read_text(encoding="utf-8")
            # Check for eval pills
            self.assertIn("pct_move=0.05", html)
            self.assertIn("horizons=30,60m", html)
            self.assertIn("storms=1", html)
            self.assertIn("precision=1.000", html)


if __name__ == "__main__":
    unittest.main()

