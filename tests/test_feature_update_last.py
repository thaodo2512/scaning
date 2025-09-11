from __future__ import annotations

import csv
import json
from pathlib import Path
import tempfile
import unittest

from cryptostorm.config import validate_config
from cryptostorm.feature.engine import update_features_last


def _write_jsonl(fp: Path, rows):
    fp.parent.mkdir(parents=True, exist_ok=True)
    with fp.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


class TestFeatureUpdateLast(unittest.TestCase):
    def _cfg(self, days=1):
        return {
            "run": {"run_id": "20250101-000000", "artifacts_root": "./artifacts"},
            "universe": {"symbols": ["BTCUSDT"]},
            "acquisition": {
                "days": days,
                "coinglass": {
                    "base_url": "https://open-api-v4.coinglass.com",
                    "api_key_env": "COINGLASS_API_KEY",
                    "exchange": "binance",
                    "quote": "USDT",
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
            "conventions": {"symbol_to_coin": {"BTCUSDT": "BTC"}, "orderbook": {"max_snapshot_age_s": 60}},
        }

    def test_update_features_last_appends_single_row_and_dedupes(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cfg = self._cfg(days=1)
            eff, warns, errs = validate_config(cfg, require_env=False)
            self.assertEqual(errs, [])

            data_root = tdp / "data"
            features_root = tdp / "features"
            sym_dir = data_root / "BTCUSDT"
            base_raw = 1700000000000
            step = 5 * 60 * 1000
            base = base_raw - (base_raw % step)
            # Inputs
            _write_jsonl(
                sym_dir / "futures_ohlcv_5m.jsonl",
                [{"ts": base, "payload": {"open": 1, "high": 2, "low": 1, "close": 2, "volume": 10}}],
            )
            _write_jsonl(sym_dir / "funding_8h_ohlc.jsonl", [{"ts": base, "payload": {"value": 0.01}}])
            _write_jsonl(sym_dir / "oi_5m_ohlc.jsonl", [{"ts": base, "payload": {"value": 100}}])
            _write_jsonl(
                sym_dir / "orderbook_futures_5m.jsonl",
                [{"ts": base, "payload": {"bids": [[100.0, 1.0]], "asks": [[100.2, 1.0]]}}],
            )

            # Append once
            update_features_last(eff, data_root=data_root, out_root=features_root, now_ms=base)
            out_fp = features_root / "BTCUSDT" / "features_5m.csv"
            self.assertTrue(out_fp.exists())
            with out_fp.open() as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(len(rows), 1)
            # Verify a few fields
            self.assertEqual(int(rows[0]["ts"]), base - (base % (5 * 60 * 1000)))
            self.assertAlmostEqual(float(rows[0]["funding_now"]), 0.01)
            self.assertGreater(float(rows[0]["spread_bps"]), 0.0)
            self.assertEqual(rows[0]["data_ok"], "True")

            # Append again with same ts; should dedupe (still 1 row)
            update_features_last(eff, data_root=data_root, out_root=features_root, now_ms=base)
            with out_fp.open() as fh2:
                rows2 = list(csv.DictReader(fh2))
            self.assertEqual(len(rows2), 1)


if __name__ == "__main__":
    unittest.main()
