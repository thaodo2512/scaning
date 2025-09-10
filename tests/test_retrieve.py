import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from cryptostorm.config import validate_config
from cryptostorm.retrieve.coinglass import run_retrieve


def cfg_for_retrieve(tmpdir: Path):
    cfg = {
        "run": {"run_id": "20250101-000000", "artifacts_root": str(tmpdir / "artifacts")},
        "universe": {"symbols": ["BTCUSDT", "ETHUSDT"]},
        "acquisition": {
            "days": 1,
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
                # Enable only datasets we exercise below
                "futures_ohlcv_5m": True,
                "oi_5m_ohlc": True,
                "orderbook_futures_5m": True,
                # Disable the rest
                "funding_8h": False,
                "liquidation_5m": False,
                "taker_futures_5m": False,
                "spot_ohlcv_5m": False,
                "taker_spot_5m": False,
                "orderbook_spot_5m": False,
            },
        },
        "conventions": {
            "symbol_to_coin": {"BTCUSDT": "BTC", "ETHUSDT": "ETH"},
            "orderbook": {"max_snapshot_age_s": 60},
        },
    }
    return cfg


class TestRetrieve(unittest.TestCase):
    def test_retrieve_requires_api_key_unless_dry_run(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cfg = cfg_for_retrieve(tdp)
            eff, warns, errs = validate_config(cfg, require_env=False)
            self.assertEqual(errs, [])
            with self.assertRaises(RuntimeError):
                run_retrieve(
                    eff,
                    base_url="https://open-api-v4.coinglass.com",
                    exchange="binance",
                    quote="USDT",
                    page_limit=50,
                    backoff_initial=0.01,
                    backoff_max=0.02,
                    out_root=tdp / "data",
                    api_key=None,
                    dry_run=False,
                )
    def test_retrieve_fallback_and_persist_and_dedup(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cfg = cfg_for_retrieve(tdp)
            eff, warns, errs = validate_config(cfg, require_env=False)
            self.assertEqual(errs, [])

            calls = []

            def fake_page_iter(base_url, headers, path, params, page_limit, backoff_initial, backoff_max):
                # record the call
                calls.append({"path": path, "params": dict(params)})
                # Return empty for aggregated OI to force fallback
                if path.endswith("/api/futures/open-interest/aggregated-history"):
                    if params.get("symbol") in {"BTC", "ETH"}:
                        return iter(())
                # Return data for fallback OI
                if path.endswith("/api/futures/openInterest/ohlc-history"):
                    return iter([
                        {"ts": 1700000000000, "v": 1},
                        {"ts": 1700000005000, "v": 2},
                    ])
                # Futures OHLCV
                if path.endswith("/api/price/ohlc-history"):
                    return iter([
                        {"ts": 1700000000000, "open": 1, "close": 2},
                    ])
                # Orderbook sample
                if "/orderbook/ask-bids-history" in path:
                    return iter([
                        {"ts": 1700000000000, "bids": [], "asks": []},
                    ])
                return iter(())

            with mock.patch("cryptostorm.retrieve.coinglass._page_iter", side_effect=fake_page_iter):
                out_root = tdp / "data"
                run_retrieve(
                    eff,
                    base_url="https://open-api-v4.coinglass.com",
                    exchange="binance",
                    quote="USDT",
                    page_limit=50,
                    backoff_initial=0.01,
                    backoff_max=0.02,
                    out_root=out_root,
                    api_key="DUMMY",
                    dry_run=False,
                )

                # Assert fallback used for OI by checking source field
                oi_path = out_root / "BTCUSDT" / "oi_5m_ohlc.jsonl"
                self.assertTrue(oi_path.exists())
                lines = oi_path.read_text(encoding="utf-8").strip().splitlines()
                self.assertEqual(len(lines), 2)
                first = json.loads(lines[0])
                self.assertEqual(first["source"], "/api/futures/openInterest/ohlc-history")

                # Futures OHLCV written
                fut_path = out_root / "BTCUSDT" / "futures_ohlcv_5m.jsonl"
                self.assertTrue(fut_path.exists())
                self.assertGreater(len(fut_path.read_text().splitlines()), 0)

                # Orderbook sample written
                ob_path = out_root / "BTCUSDT" / "orderbook_futures_5m.jsonl"
                self.assertTrue(ob_path.exists())
                self.assertGreater(len(ob_path.read_text().splitlines()), 0)

                # Run again to ensure dedup (line counts unchanged)
                before_counts = {
                    p: len(p.read_text(encoding="utf-8").splitlines()) for p in [oi_path, fut_path, ob_path]
                }
                run_retrieve(
                    eff,
                    base_url="https://open-api-v4.coinglass.com",
                    exchange="binance",
                    quote="USDT",
                    page_limit=50,
                    backoff_initial=0.01,
                    backoff_max=0.02,
                    out_root=out_root,
                    api_key="DUMMY",
                    dry_run=False,
                )
                after_counts = {
                    p: len(p.read_text(encoding="utf-8").splitlines()) for p in [oi_path, fut_path, ob_path]
                }
                self.assertEqual(before_counts, after_counts)

                # Confirm aggregated call used mapped coins via 'symbol'
                agg_calls = [c for c in calls if c["path"].endswith("/api/futures/open-interest/aggregated-history")]
                syms = sorted({c["params"].get("symbol") for c in agg_calls})
                self.assertEqual(syms, ["BTC", "ETH"])

    def test_incremental_start_time_uses_last_saved_ts_plus_one(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cfg = cfg_for_retrieve(tdp)
            # Use 1 day, but control 'now' near the inserted ts via mock
            cfg["acquisition"]["days"] = 1
            eff, warns, errs = validate_config(cfg, require_env=False)
            self.assertEqual(errs, [])

            # First run writes data
            def first_iter(base_url, headers, path, params, page_limit, backoff_initial, backoff_max):
                if path.endswith("/api/price/ohlc-history"):
                    return iter([{"ts": 1700000000000}])
                if path.endswith("/api/futures/openInterest/ohlc-history"):
                    return iter([{ "ts": 1700000005000 }])
                if "/orderbook/ask-bids-history" in path:
                    return iter([{ "ts": 1700000000000 }])
                if path.endswith("/api/futures/open-interest/aggregated-history"):
                    return iter(())
                return iter(())

            with mock.patch("cryptostorm.retrieve.coinglass._page_iter", side_effect=first_iter):
                run_retrieve(
                    eff,
                    base_url="https://open-api-v4.coinglass.com",
                    exchange="binance",
                    quote="USDT",
                    page_limit=50,
                    backoff_initial=0.01,
                    backoff_max=0.02,
                    out_root=tdp / "data",
                    api_key="DUMMY",
                    dry_run=False,
                )

            # Second run: capture params and assert startTime equals last_ts+1, using fixed now
            calls = []

            def second_iter(base_url, headers, path, params, page_limit, backoff_initial, backoff_max):
                calls.append({"path": path, "params": dict(params)})
                return iter(())

            with mock.patch("cryptostorm.retrieve.coinglass._page_iter", side_effect=second_iter), \
                 mock.patch("cryptostorm.retrieve.coinglass._utc_now_ms", return_value=1700000006000):
                run_retrieve(
                    eff,
                    base_url="https://open-api-v4.coinglass.com",
                    exchange="binance",
                    quote="USDT",
                    page_limit=50,
                    backoff_initial=0.01,
                    backoff_max=0.02,
                    out_root=tdp / "data",
                    api_key="DUMMY",
                    dry_run=False,
                )

            # Map expected last ts per dataset for BTCUSDT
            expected_start = {
                "/api/price/ohlc-history": 1700000000001,
                "/api/futures/openInterest/ohlc-history": 1700000005001,
                "/api/futures/orderbook/ask-bids-history": 1700000000001,
            }
            for c in calls:
                path = c["path"]
                if path in expected_start:
                    self.assertEqual(c["params"]["startTime"], expected_start[path])


if __name__ == "__main__":
    unittest.main()
