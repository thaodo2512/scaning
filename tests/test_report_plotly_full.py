from pathlib import Path
import tempfile
import csv
import json
import unittest

from cryptostorm.report.plotly_full import main as plotly_full_main


def write_jsonl(fp: Path, rows):
    fp.parent.mkdir(parents=True, exist_ok=True)
    with fp.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


class TestPlotlyFullReport(unittest.TestCase):
    def test_build_plotly_full_report_html(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cfg_path = tdp / "cfg.yaml"
            cfg = {
                "run": {"run_id": "test", "artifacts_root": str(tdp / "artifacts")},
                "universe": {"symbols": ["BTCUSDT"]},
                "acquisition": {"days": 1, "coinglass": {"intervals": {"futures_ohlcv": "5m", "funding_8h": "8h", "oi_ohlc": "5m", "liquidation": "5m"}, "per_series_mode": {"futures_ohlcv": "exchange", "funding_8h": "exchange", "oi_5m": "aggregated", "liquidation": "aggregated"}}},
            }
            import yaml
            cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

            base = 1700000000000
            data_root = tdp / "data"
            sym_dir = data_root / "BTCUSDT"
            write_jsonl(sym_dir / "futures_ohlcv_5m.jsonl", [
                {"ts": base, "payload": {"open": 1, "high": 2, "low": 1, "close": 2}},
                {"ts": base+300000, "payload": {"open": 2, "high": 3, "low": 2, "close": 3}},
            ])
            write_jsonl(sym_dir / "oi_5m_ohlc.jsonl", [
                {"ts": base, "payload": {"close": 100}},
                {"ts": base+300000, "payload": {"close": 101}},
            ])
            write_jsonl(sym_dir / "liquidation_5m.jsonl", [
                {"ts": base, "payload": {"notional": 1000}},
                {"ts": base+300000, "payload": {"notional": 2000}},
            ])

            art = tdp / "artifacts" / "test"
            (art / "scores").mkdir(parents=True, exist_ok=True)
            with (art / "scores" / "BTCUSDT.csv").open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["ts", "symbol", "score", "threshold"])
                w.writerow([base // 1000, "BTCUSDT", 0.5, 0.4])
                w.writerow([base // 1000 + 300, "BTCUSDT", 0.3, 0.4])
            (art / "alerts").mkdir(parents=True, exist_ok=True)
            with (art / "alerts" / "BTCUSDT.csv").open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["ts", "symbol", "kind", "score", "threshold"])
                w.writeheader()
                w.writerow({"ts": base // 1000, "symbol": "BTCUSDT", "kind": "pre_alert", "score": 0.5, "threshold": 0.4})

            out_dir = tdp / "reports"
            rc = plotly_full_main([str(cfg_path), "--data", str(data_root), "--artifacts", str(art), "--out", str(out_dir)])
            self.assertEqual(rc, 0)
            html = (out_dir / "BTCUSDT_plotly.html").read_text(encoding="utf-8")
            self.assertIn("Plotly.newPlot", html)
            self.assertIn("candlestick", html)
            self.assertIn("IsolationForest Score", html)


if __name__ == "__main__":
    unittest.main()

