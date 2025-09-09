import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from cryptostorm.config import validate_config
from cryptostorm.retrieve.coinglass import run_retrieve


def base_cfg_all(tmp: Path):
    return {
        "run": {"run_id": "20250101-000000", "artifacts_root": str(tmp / "artifacts")},
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
                    "spot_ohlcv": "5m",
                    "funding_8h": "8h",
                    "funding_5m": "5m",
                    "oi_ohlc": "5m",
                    "taker_volume": "5m",
                    "taker_spot_5m": "5m",
                    "liquidation": "5m",
                    "orderbook_sample": "last_of_5m",
                },
                "per_series_mode": {
                    "futures_ohlcv": "exchange",
                    "spot_ohlcv": "exchange",
                    "funding_8h": "exchange",
                    "funding_5m": "exchange",
                    "oi_5m": "aggregated",
                    "taker_futures": "exchange",
                    "taker_spot": "exchange",
                    "liquidation": "aggregated",
                    "orderbook": "exchange",
                },
            },
            "enable": {
                "spot_ohlcv_5m": True,
                "funding_pred_5m": True,
                "taker_spot_5m": True,
                "orderbook_spot_5m": True,
            },
        },
        "conventions": {
            "symbol_to_coin": {"BTCUSDT": "BTC", "ETHUSDT": "ETH"},
            "units": {"taker_volume": "base"},
            "orderbook": {"max_snapshot_age_s": 60},
        },
    }


class TestRetrieveEndpoints(unittest.TestCase):
    def test_all_preferred_endpoints_used(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cfg = base_cfg_all(tdp)
            eff, warns, errs = validate_config(cfg, require_env=False)
            self.assertEqual(errs, [])

            def page_iter(base_url, headers, path, params, page_limit, backoff_initial, backoff_max):
                # Always return a non-empty page so fallback is not used
                return iter([
                    {"ts": 1700000000000, "path": path},
                ])

            with mock.patch("cryptostorm.retrieve.coinglass._page_iter", side_effect=page_iter):
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

            # Expected dataset files and preferred sources
            expected = {
                "futures_ohlcv_5m.jsonl": "/api/price/ohlc-history",
                "spot_ohlcv_5m.jsonl": "/api/spot/price/history",
                "funding_8h_ohlc.jsonl": "/api/futures/funding-rate/history",
                "funding_pred_5m_ohlc.jsonl": "/api/futures/funding-rate/history",
                "oi_5m_ohlc.jsonl": "/api/futures/openInterest/ohlc-aggregated-history",
                "taker_futures_5m.jsonl": "/api/futures/taker-buy-sell-volume/history",
                "taker_spot_5m.jsonl": "/api/spot/taker-buy-sell-volume/history",
                "liquidation_5m.jsonl": "/api/futures/liquidation/aggregated-history",
                "orderbook_futures_5m.jsonl": "/api/futures/orderbook/ask-bids-history",
                "orderbook_spot_5m.jsonl": "/api/spot/orderbook/ask-bids-history",
            }
            sym_dir = out_root / "BTCUSDT"
            for fname, preferred in expected.items():
                fp = sym_dir / fname
                self.assertTrue(fp.exists(), f"missing {fname}")
                rec = json.loads(fp.read_text(encoding="utf-8").splitlines()[0])
                self.assertEqual(rec["source"], preferred, f"wrong source for {fname}")

    def test_all_fallbacks_triggered_where_supported(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cfg = base_cfg_all(tdp)
            eff, warns, errs = validate_config(cfg, require_env=False)
            self.assertEqual(errs, [])

            def page_iter(base_url, headers, path, params, page_limit, backoff_initial, backoff_max):
                # Return empty for preferred paths that have fallbacks; otherwise return data
                preferred_to_empty = {
                    "/api/futures/openInterest/ohlc-aggregated-history",
                    "/api/futures/taker-buy-sell-volume/history",
                    "/api/futures/liquidation/aggregated-history",
                    "/api/futures/funding-rate/history",
                }
                if path in preferred_to_empty:
                    return iter(())
                return iter([
                    {"ts": 1700000000000, "path": path},
                ])

            with mock.patch("cryptostorm.retrieve.coinglass._page_iter", side_effect=page_iter):
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

            expected_fallback = {
                "oi_5m_ohlc.jsonl": "/api/futures/openInterest/ohlc-history",
                "taker_futures_5m.jsonl": "/api/futures/aggregated-taker-buy-sell-volume/history",
                "liquidation_5m.jsonl": "/api/futures/liquidation/history",
                "funding_8h_ohlc.jsonl": "/api/futures/fundingRate/ohlc-history",
                "funding_pred_5m_ohlc.jsonl": "/api/futures/fundingRate/ohlc-history",
            }
            expected_preferred = {
                "futures_ohlcv_5m.jsonl": "/api/price/ohlc-history",
                "spot_ohlcv_5m.jsonl": "/api/spot/price/history",
                "taker_spot_5m.jsonl": "/api/spot/taker-buy-sell-volume/history",
                "orderbook_futures_5m.jsonl": "/api/futures/orderbook/ask-bids-history",
                "orderbook_spot_5m.jsonl": "/api/spot/orderbook/ask-bids-history",
            }
            sym_dir = (tdp / "data" / "BTCUSDT")
            for fname, fallback in expected_fallback.items():
                fp = sym_dir / fname
                self.assertTrue(fp.exists(), f"missing {fname}")
                rec = json.loads(fp.read_text(encoding="utf-8").splitlines()[0])
                self.assertEqual(rec["source"], fallback, f"wrong source for {fname}")
            for fname, preferred in expected_preferred.items():
                fp = sym_dir / fname
                self.assertTrue(fp.exists(), f"missing {fname}")
                rec = json.loads(fp.read_text(encoding="utf-8").splitlines()[0])
                self.assertEqual(rec["source"], preferred, f"wrong source for {fname}")

