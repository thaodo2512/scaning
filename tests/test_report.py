from pathlib import Path
import tempfile
import csv
import json
import unittest

from cryptostorm.report.engine import main as report_main


def write_jsonl(fp: Path, rows):
    fp.parent.mkdir(parents=True, exist_ok=True)
    with fp.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


class TestReport(unittest.TestCase):
    def test_build_report_html(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cfg_path = tdp / "cfg.yaml"
            cfg = {
                "run": {"run_id": "test", "artifacts_root": str(tdp / "artifacts")},
                "universe": {"symbols": ["BTCUSDT"]},
                "acquisition": {
                    "days": 1,
                    "coinglass": {
                        "intervals": {
                            "futures_ohlcv": "5m",
                            "funding_8h": "8h",
                            "oi_ohlc": "5m",
                            "taker_volume": "5m",
                            "liquidation": "5m",
                            "orderbook_sample": "last_of_5m",
                        },
                        "per_series_mode": {
                            "futures_ohlcv": "exchange",
                            "funding_8h": "exchange",
                            "oi_5m": "aggregated",
                            "taker_futures": "exchange",
                            "liquidation": "aggregated",
                            "orderbook": "exchange",
                        },
                    },
                },
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

            # Scores + alerts
            art = tdp / "artifacts" / "test"
            (art / "scores").mkdir(parents=True, exist_ok=True)
            with (art / "scores" / "BTCUSDT.csv").open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["ts", "symbol", "score", "threshold"])
                w.writerow([base, "BTCUSDT", 0.5, 0.4])
                w.writerow([base+300000, "BTCUSDT", 0.3, 0.4])
            (art / "alerts").mkdir(parents=True, exist_ok=True)
            with (art / "alerts" / "BTCUSDT.csv").open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["ts", "symbol", "kind", "score", "threshold"])
                w.writeheader()
                w.writerow({"ts": base, "symbol": "BTCUSDT", "kind": "pre_alert", "score": 0.5, "threshold": 0.4})

            out_dir = tdp / "reports"
            rc = report_main([str(cfg_path), "--data", str(data_root), "--features", str(tdp / "features"), "--artifacts", str(art), "--out", str(out_dir)])
            self.assertEqual(rc, 0)
            html = (out_dir / "BTCUSDT.html").read_text(encoding="utf-8")
            self.assertIn("LightweightCharts", html)
            self.assertIn("BTCUSDT — Price + Alerts", html)
            self.assertTrue(("candlestickSeries" in html) or ("addCandlestickSeries" in html))


if __name__ == "__main__":
    unittest.main()
