from pathlib import Path
import tempfile
import json
import unittest

from cryptostorm.config import validate_config
from cryptostorm.audit import audit_coverage


def cfg(days=1):
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
                "orderbook_futures_5m": False,
                "spot_ohlcv_5m": False,
                "taker_spot_5m": False,
                "orderbook_spot_5m": False,
                "funding_pred_5m": False,
            },
        },
        "conventions": {"symbol_to_coin": {"BTCUSDT": "BTC"}},
    }


class TestAudit(unittest.TestCase):
    def test_audit_basic_coverage(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            config = cfg(days=1)
            eff, warns, errs = validate_config(config, require_env=False)
            self.assertEqual(errs, [])

            # Create minimal data files with a handful of timestamps
            data_root = tdp / "data"
            sym_dir = data_root / "BTCUSDT"
            sym_dir.mkdir(parents=True, exist_ok=True)
            # futures 5m: write 12 bars (~1 hour)
            fut_fp = sym_dir / "futures_ohlcv_5m.jsonl"
            for i in range(12):
                fut_fp.write_text("", encoding="utf-8") if not fut_fp.exists() else None
                fut_fp.write_text("", encoding="utf-8") if not fut_fp.exists() else None
            with fut_fp.open("w", encoding="utf-8") as fh:
                base = 1700000000000
                for i in range(12):
                    fh.write(json.dumps({"ts": base + i * 5 * 60 * 1000}) + "\n")

            # funding 8h: write 1 event
            fund_fp = sym_dir / "funding_8h_ohlc.jsonl"
            with fund_fp.open("w", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": 1700000000000}) + "\n")

            rows, failures = audit_coverage(eff, data_root, min_ratio=0.0)
            self.assertTrue(any(r.dataset == "futures_ohlcv_5m" for r in rows))
            self.assertTrue(any(r.dataset == "funding_8h" for r in rows))
            # With min_ratio 1.0 it should fail due to partial coverage
            rows2, failures2 = audit_coverage(eff, data_root, min_ratio=1.0)
            self.assertTrue(len(failures2) >= 1)


if __name__ == "__main__":
    unittest.main()
