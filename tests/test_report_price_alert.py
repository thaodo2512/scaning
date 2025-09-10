from __future__ import annotations

from pathlib import Path
import tempfile
import csv
import json
import unittest

from cryptostorm.report.price_alert import main as price_report_main


def write_jsonl(fp: Path, rows):
    fp.parent.mkdir(parents=True, exist_ok=True)
    with fp.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


class TestPriceAlertReport(unittest.TestCase):
    def test_build_price_alert_report_html(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cfg_path = tdp / "cfg.yaml"
            cfg = {
                "run": {"run_id": "test", "artifacts_root": str(tdp / "artifacts")},
                "universe": {"symbols": ["BTCUSDT"]},
            }
            import yaml
            cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

            base = 1700000000000
            data_root = tdp / "data"
            sym_dir = data_root / "BTCUSDT"
            write_jsonl(
                sym_dir / "futures_ohlcv_5m.jsonl",
                [
                    {"ts": base, "payload": {"open": 1, "high": 2, "low": 1, "close": 2}},
                    {"ts": base + 300000, "payload": {"open": 2, "high": 3, "low": 2, "close": 3}},
                ],
            )

            art = tdp / "artifacts" / "test"
            (art / "alerts").mkdir(parents=True, exist_ok=True)
            with (art / "alerts" / "BTCUSDT.csv").open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["ts", "symbol", "kind", "score", "threshold"])
                w.writeheader()
                w.writerow({"ts": base, "symbol": "BTCUSDT", "kind": "storm", "score": 0.5, "threshold": 0.4})

            out_dir = tdp / "reports"
            rc = price_report_main([str(cfg_path), "--data", str(data_root), "--artifacts", str(art), "--out", str(out_dir)])
            self.assertEqual(rc, 0)
            html = (out_dir / "BTCUSDT_price_alert.html").read_text(encoding="utf-8")
            self.assertIn("Plotly.newPlot", html)
            self.assertIn("priceData", html)
            self.assertIn("alertData", html)


if __name__ == "__main__":
    unittest.main()

