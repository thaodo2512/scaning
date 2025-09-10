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
        # Fallback to repo default location if present
        default_file = Path("secrets/coinglass_api_key.txt")
        if default_file.exists():
            return default_file.read_text(encoding="utf-8").strip()
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

    def test_live_futures_5m_30d_coverage_v4_timesliced(self):
        api_key = self._resolve_api_key()
        if os.getenv("RUN_LIVE_V4_5M") not in {"1", "true", "TRUE", "yes"}:
            self.skipTest("Set RUN_LIVE_V4_5M=1 to run 30d futures_5m coverage test (v4 time-sliced)")

        # --- 1. Clear Test Parameters ---
        # Define the test's core parameters at the top for clarity and easy modification.
        DAYS_TO_FETCH = 30
        EXPECTED_INTERVAL_MINUTES = 5
        COVERAGE_THRESHOLD_PERCENT = 80.0  # Test passes if we receive at least this percentage of data.

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cfg = {
                "run": {"run_id": "live-test-5m", "artifacts_root": str(tdp / "artifacts")},
                "universe": {"symbols": ["BTCUSDT"]},
                "acquisition": {
                    "days": DAYS_TO_FETCH, # Use the constant here
                    "coinglass": {
                        "base_url": "https://open-api-v4.coinglass.com",
                        "api_key_env": "COINGLASS_API_KEY",
                        "exchange": "binance",
                        "quote": "USDT",
                        "paging": {"page_limit": 500, "backoff_initial_s": 0.5, "backoff_max_s": 4},
                        "intervals": {"futures_ohlcv": "5m"},
                        "per_series_mode": {"futures_ohlcv": "exchange"},
                        "slice_days": 1, # Use 1-day slices for fetching a large range
                    },
                    "enable": {"futures_ohlcv_5m": True},
                },
                "conventions": {"symbol_to_coin": {"BTCUSDT": "BTC"}},
            }
            eff, warns, errs = validate_config(cfg, require_env=False)
            self.assertEqual(errs, [], f"invalid config: {errs}")
            out_root = tdp / "data"

            # Execute the data retrieval
            run_retrieve(
                eff,
                base_url="https://open-api-v4.coinglass.com",
                exchange="binance",
                quote="USDT",
                page_limit=500,
                backoff_initial=0.5,
                backoff_max=4.0,
                out_root=out_root,
                api_key=api_key,
                dry_run=False,
                slice_days=1,
            )

            # --- 2. More Detailed Verification ---
            import json as _json
            import time as _time

            fp = out_root / "BTCUSDT" / "futures_ohlcv_5m.jsonl"
            self.assertTrue(fp.exists(), "Output file futures_ohlcv_5m.jsonl was not created.")

            # Calculate the expected number of data points (bars) for the period.
            bars_per_day = (24 * 60) / EXPECTED_INTERVAL_MINUTES
            expected_bars = int(bars_per_day * DAYS_TO_FETCH)
            required_bars_for_pass = int(expected_bars * (COVERAGE_THRESHOLD_PERCENT / 100.0))

            # Collect unique timestamps from the downloaded file that are within the 30-day window.
            now_ms = int(_time.time() * 1000)
            start_ms = now_ms - (DAYS_TO_FETCH * 24 * 60 * 60 * 1000)
            ts_set = set()
            for line in fp.read_text(encoding="utf-8").splitlines():
                try:
                    ts = _json.loads(line).get("ts")
                    if isinstance(ts, (int, float)) and start_ms <= int(ts) <= now_ms:
                        ts_set.add(int(ts))
                except Exception:
                    continue

            observed_bars = len(ts_set)
            actual_coverage_percent = (observed_bars / expected_bars) * 100 if expected_bars > 0 else 0

            # --- 3. Improved Assertion Message ---
            # The failure message now provides precise numbers for easier debugging.
            self.assertGreaterEqual(
                observed_bars,
                required_bars_for_pass,
                f"Insufficient data coverage for the last {DAYS_TO_FETCH} days.\n"
                f"  - Observed: {observed_bars} bars\n"
                f"  - Expected: {expected_bars} bars\n"
                f"  - Coverage: {actual_coverage_percent:.1f}% "
                f"(Threshold is {COVERAGE_THRESHOLD_PERCENT}%).\n"
                f"  - This likely indicates an API plan limit or an issue with the Coinglass endpoint."
            )