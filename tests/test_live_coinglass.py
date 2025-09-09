import os
from pathlib import Path
import tempfile
import unittest

from cryptostorm.config import validate_config
from cryptostorm.retrieve.coinglass import run_retrieve


RUN_LIVE = os.getenv("RUN_LIVE_COINGLASS") == "1"


@unittest.skipUnless(RUN_LIVE, "Set RUN_LIVE_COINGLASS=1 to enable live Coinglass test")
class TestLiveCoinglass(unittest.TestCase):
    def _resolve_api_key(self) -> str:
        key = os.getenv("COINGLASS_API_KEY")
        if key:
            return key
        key_file = os.getenv("COINGLASS_API_KEY_FILE")
        if key_file and Path(key_file).exists():
            return Path(key_file).read_text(encoding="utf-8").strip()
        self.skipTest(
            "Provide COINGLASS_API_KEY or COINGLASS_API_KEY_FILE to run live test"
        )

    def test_live_funding_8h_smoke(self):
        api_key = self._resolve_api_key()
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cfg = {
                "run": {"run_id": "live-test", "artifacts_root": str(tdp / "artifacts")},
                "universe": {"symbols": ["BTCUSDT"]},
                "acquisition": {
                    "days": 1,
                    "coinglass": {
                        "base_url": "https://open-api-v4.coinglass.com",
                        "api_key_env": "COINGLASS_API_KEY",
                        "exchange": "binance",
                        "quote": "USDT",
                        "paging": {"page_limit": 200, "backoff_initial_s": 0.5, "backoff_max_s": 4},
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
                    "enable": {
                        "funding_8h": True,
                        "futures_ohlcv_5m": False,
                        "oi_5m_ohlc": False,
                        "taker_futures_5m": False,
                        "liquidation_5m": False,
                        "orderbook_futures_5m": False,
                        "spot_ohlcv_5m": False,
                        "taker_spot_5m": False,
                        "orderbook_spot_5m": False,
                    },
                },
                "conventions": {
                    "symbol_to_coin": {"BTCUSDT": "BTC"},
                    "units": {"taker_volume": "base"},
                    "orderbook": {"max_snapshot_age_s": 60},
                },
            }
            eff, warns, errs = validate_config(cfg, require_env=False)
            if errs:
                self.fail(f"Config invalid: {errs}")

            out_root = tdp / "data"
            run_retrieve(
                eff,
                base_url="https://open-api-v4.coinglass.com",
                exchange="binance",
                quote="USDT",
                page_limit=200,
                backoff_initial=0.5,
                backoff_max=4.0,
                out_root=out_root,
                api_key=api_key,
                dry_run=False,
            )

            fp = out_root / "BTCUSDT" / "funding_8h_ohlc.jsonl"
            self.assertTrue(fp.exists(), "funding_8h file not created")
            contents = fp.read_text(encoding="utf-8").strip().splitlines()
            self.assertGreater(len(contents), 0, "No funding_8h rows returned")

