import os
from pathlib import Path
import tempfile
import unittest

from cryptostorm.config import validate_config


def minimal_cfg(overrides=None):
    cfg = {
        "run": {"run_id": "20250101-000000", "artifacts_root": "./artifacts"},
        "universe": {"symbols": ["BTCUSDT", "ETHUSDT"]},
        "acquisition": {
            "days": 30,
            "coinglass": {
                "base_url": "https://open-api-v4.coinglass.com",
                "api_key_env": "COINGLASS_API_KEY",
                "exchange": "binance",
                "quote": "USDT",
                "paging": {"page_limit": 100, "backoff_initial_s": 0.1, "backoff_max_s": 1},
                "intervals": {
                    # use synonyms to exercise normalization
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
            # Disable some non-essential core datasets for smaller tests
            "enable": {
                "funding_8h": False,
                "liquidation_5m": False,
                "taker_futures_5m": False,
            },
        },
        "conventions": {
            "symbol_to_coin": {"BTCUSDT": "BTC", "ETHUSDT": "ETH"},
            "units": {"taker_volume": "base"},
            "orderbook": {"max_snapshot_age_s": 60},
        },
    }
    if overrides:
        # shallow update for test convenience
        for k, v in overrides.items():
            cfg[k] = v
    return cfg


class TestConfigValidation(unittest.TestCase):
    def test_normalization_and_required_datasets(self):
        cfg = minimal_cfg()
        eff, warns, errs = validate_config(cfg, require_env=False)
        self.assertEqual(errs, [], f"unexpected errors: {errs}")
        # Check normalized dataset keys present
        keys = set(eff.datasets.keys())
        for k in {"futures_ohlcv_5m", "oi_5m_ohlc", "orderbook_futures_5m"}:
            self.assertIn(k, keys)
        # intervals normalized
        self.assertEqual(eff.datasets["futures_ohlcv_5m"].interval, "5m")
        self.assertEqual(eff.datasets["oi_5m_ohlc"].interval, "5m")
        # orderbook interval sourced from orderbook_sample
        self.assertEqual(eff.datasets["orderbook_futures_5m"].interval, "last_of_5m")

    def test_api_key_file_presence(self):
        with tempfile.TemporaryDirectory() as td:
            key_path = Path(td) / "key.txt"
            key_path.write_text("DUMMY", encoding="utf-8")
            cfg = minimal_cfg()
            cfg["acquisition"]["coinglass"]["api_key_file"] = str(key_path)
            eff, warns, errs = validate_config(cfg, require_env=True)
            self.assertEqual(errs, [], f"unexpected errors: {errs}")
            self.assertTrue(eff.api_key_present)
            self.assertEqual(Path(eff.api_key_file), key_path)

    def test_api_key_env_presence(self):
        cfg = minimal_cfg()
        # Use default env var name from cfg: COINGLASS_API_KEY
        os.environ["COINGLASS_API_KEY"] = "DUMMY"
        try:
            eff, warns, errs = validate_config(cfg, require_env=True)
            self.assertEqual(errs, [], f"unexpected errors: {errs}")
            self.assertTrue(eff.api_key_present)
            self.assertIsNone(eff.api_key_file)
        finally:
            del os.environ["COINGLASS_API_KEY"]

    def test_orderbook_futures_enabled_spot_optional(self):
        cfg = minimal_cfg()
        eff, warns, errs = validate_config(cfg, require_env=False)
        self.assertTrue(eff.datasets["orderbook_futures_5m"].enabled)
        # spot OB optional -> disabled by default
        self.assertIn("orderbook_spot_5m", eff.datasets)
        self.assertFalse(eff.datasets["orderbook_spot_5m"].enabled)


if __name__ == "__main__":
    unittest.main()
