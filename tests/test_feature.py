import csv
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from cryptostorm.config import validate_config
from cryptostorm.feature.engine import build_features


def write_jsonl(path: Path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


class TestFeatureEngineering(unittest.TestCase):
    def cfg(self, days=1):
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
                    "paging": {"page_limit": 50, "backoff_initial_s": 0.1, "backoff_max_s": 0.2},
                    "intervals": {
                        "futures_ohlcv": "5m",
                        "funding_8h": "8h",
                        "oi_ohlc": "5m",
                        "orderbook_sample": "last_of_5m",
                        "taker_volume": "5m",
                        "liquidation": "5m",
                    },
                    "per_series_mode": {
                        "futures_ohlcv": "exchange",
                        "funding_8h": "exchange",
                        "oi_5m": "aggregated",
                        "orderbook": "exchange",
                        "taker_futures": "exchange",
                        "liquidation": "aggregated",
                    },
                },
                "enable": {
                    "taker_futures_5m": False,
                    "liquidation_5m": False,
                    "spot_ohlcv_5m": False,
                    "taker_spot_5m": False,
                    "orderbook_spot_5m": False,
                    "funding_pred_5m": False,
                },
            },
            "conventions": {"symbol_to_coin": {"BTCUSDT": "BTC"}, "orderbook": {"max_snapshot_age_s": 60}},
        }

    def test_funding_ffill_only_and_ob_staleness(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cfg = self.cfg(days=1)
            eff, warns, errs = validate_config(cfg, require_env=False)
            self.assertEqual(errs, [])

            data_root = tdp / "data"
            sym_dir = data_root / "BTCUSDT"
            # Create OHLCV for two consecutive 5m bars
            step = 5 * 60 * 1000
            base_raw = 1700000000000
            base = base_raw - (base_raw % step)
            write_jsonl(
                sym_dir / "futures_ohlcv_5m.jsonl",
                [
                    {"ts": base, "payload": {"open": 1, "high": 2, "low": 1, "close": 2, "volume": 10}},
                    {"ts": base + 5 * 60 * 1000, "payload": {"open": 2, "high": 3, "low": 2, "close": 3, "volume": 20}},
                ],
            )
            # Funding only at first bar; should forward-fill to second
            write_jsonl(sym_dir / "funding_8h_ohlc.jsonl", [{"ts": base, "payload": {"value": 0.01}}])
            # OI only at first bar; should NOT ffill to second
            write_jsonl(sym_dir / "oi_5m_ohlc.jsonl", [{"ts": base, "payload": {"value": 100}}])
            # Orderbook snapshot too old for second bar -> data_ok False for second
            write_jsonl(
                sym_dir / "orderbook_futures_5m.jsonl",
                [
                    {"ts": base, "payload": {"bids": [[100.0, 1]], "asks": [[100.2, 1]]}},
                ],
            )

            out_root = tdp / "features"
            # Build features for window covering our two bars
            with mock.patch("cryptostorm.feature.engine._utc_now_ms", return_value=base + 10 * 60 * 1000):
                build_features(eff, data_root=data_root, out_root=out_root)

            out_fp = out_root / "BTCUSDT" / "features_5m.csv"
            self.assertTrue(out_fp.exists())
            with out_fp.open() as fh:
                rows = list(csv.DictReader(fh))
            # Filter to our two bars
            bars = [r for r in rows if int(r["ts"]) in {base, base + 5 * 60 * 1000}]
            self.assertEqual(len(bars), 2)
            # Funding forward-filled
            self.assertAlmostEqual(float(bars[0]["funding_now"]), 0.01)
            self.assertAlmostEqual(float(bars[1]["funding_now"]), 0.01)
            # OI not forward-filled
            self.assertAlmostEqual(float(bars[0]["oi_now"]), 100.0)
            self.assertTrue(bars[1]["oi_now"] == '' or math.isnan(float(bars[1]["oi_now"])) )
            # OB staleness: first bar has spread, second should be NaN and data_ok False
            self.assertGreater(float(bars[0]["spread_bps"]), 0.0)
            self.assertTrue(bars[1]["spread_bps"] == '' or math.isnan(float(bars[1]["spread_bps"])) )
            self.assertEqual(bars[1]["data_ok"], 'False')


if __name__ == "__main__":
    unittest.main()
