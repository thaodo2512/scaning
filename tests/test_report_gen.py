from __future__ import annotations

from pathlib import Path
import tempfile
import csv
import json
import re
import unittest

from cryptostorm.report.engine import main as report_main


def _write_jsonl(fp: Path, rows):
    fp.parent.mkdir(parents=True, exist_ok=True)
    with fp.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _extract_const_array(html: str, const_name: str):
    # Extract a JSON array from a const declaration (e.g., const name = [...];)
    needle = f"const {const_name}"
    i = html.find(needle)
    if i == -1:
        return None
    i = html.find("=", i)
    if i == -1:
        return None
    j = html.find(";", i)
    if j == -1:
        return None
    payload = html[i + 1 : j].strip()
    return json.loads(payload)


class TestReportGeneration(unittest.TestCase):
    def _base_cfg(self, tdp: Path) -> Path:
        cfg = {
            "run": {"run_id": "testrun", "artifacts_root": str(tdp / "artifacts")},
            "universe": {"symbols": ["BTCUSDT"]},
            "acquisition": {"days": 1, "coinglass": {"intervals": {"futures_ohlcv": "5m", "funding_8h": "8h", "oi_ohlc": "5m"}, "per_series_mode": {"futures_ohlcv": "exchange", "funding_8h": "exchange", "oi_5m": "aggregated"}}},
        }
        import yaml

        p = tdp / "cfg.yaml"
        p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        return p

    def test_sanitizes_nan_and_renders_minimum_series(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cfg_path = self._base_cfg(tdp)
            base = 1700000000000

            data_root = tdp / "data" / "BTCUSDT"
            # Futures OHLCV: include a bad third row with NaN strings -> should be filtered
            _write_jsonl(
                data_root / "futures_ohlcv_5m.jsonl",
                [
                    {"ts": base, "payload": {"open": 1, "high": 2, "low": 1, "close": 2}},
                    {"ts": base + 300000, "payload": {"open": 2, "high": 3, "low": 2, "close": 3}},
                    {"ts": base + 600000, "payload": {"open": "NaN", "high": 4, "low": 3, "close": 4}},
                ],
            )
            # OI with one valid row
            _write_jsonl(data_root / "oi_5m_ohlc.jsonl", [{"ts": base, "payload": {"close": 100}}])
            # Liq with one valid row
            _write_jsonl(data_root / "liquidation_5m.jsonl", [{"ts": base, "payload": {"notional": 1000}}])

            # Scores: first is NaN, second finite
            art = tdp / "artifacts" / "testrun"
            (art / "scores").mkdir(parents=True, exist_ok=True)
            with (art / "scores" / "BTCUSDT.csv").open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["ts", "symbol", "score", "threshold"])
                w.writerow([base // 1000, "BTCUSDT", "nan", "nan"])  # invalid
                w.writerow([base // 1000 + 300, "BTCUSDT", 0.5, 0.4])    # valid

            out_dir = tdp / "reports"
            rc = report_main([str(cfg_path), "--data", str(tdp / "data"), "--features", str(tdp / "features"), "--artifacts", str(art), "--out", str(out_dir)])
            self.assertEqual(rc, 0)
            html = (out_dir / "BTCUSDT.html").read_text(encoding="utf-8")

            price_raw = _extract_const_array(html, "priceRaw")
            score_raw = _extract_const_array(html, "scoreRaw")
            thr_raw = _extract_const_array(html, "thrRaw")
            oi_raw = _extract_const_array(html, "oiRaw")
            liq_raw = _extract_const_array(html, "liqRaw")

            # Price should have filtered out the NaN row -> only 2 candles
            self.assertIsInstance(price_raw, list)
            self.assertEqual(len(price_raw), 2)
            # Scores should contain only the finite entry
            self.assertEqual(len(score_raw), 1)
            self.assertEqual(len(thr_raw), 1)
            # OI and Liq should have one point each
            self.assertEqual(len(oi_raw), 1)
            self.assertEqual(len(liq_raw), 1)

    def test_empty_states_when_missing_sections(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cfg_path = self._base_cfg(tdp)
            base = 1700000000000

            data_root = tdp / "data" / "BTCUSDT"
            # Only price data; no OI/Liq files, and no scores
            _write_jsonl(
                data_root / "futures_ohlcv_5m.jsonl",
                [{"ts": base, "payload": {"open": 1, "high": 1.5, "low": 0.9, "close": 1.2}}],
            )

            out_dir = tdp / "reports"
            rc = report_main([str(cfg_path), "--data", str(tdp / "data"), "--features", str(tdp / "features"), "--artifacts", str(tdp / "artifacts" / "testrun"), "--out", str(out_dir)])
            self.assertEqual(rc, 0)
            html = (out_dir / "BTCUSDT.html").read_text(encoding="utf-8")
            # Score panel empty message present
            self.assertIn("No model scores — run backtest for this run_id", html)
            # OI/Liq empty message present
            self.assertIn("No OI/Liquidation data available", html)


if __name__ == "__main__":
    unittest.main()
