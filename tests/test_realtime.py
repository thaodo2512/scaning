from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from cryptostorm.realtime.engine import main as realtime_main


class TestRealtimePhase1(unittest.TestCase):
    def _cfg_path(self, tdp: Path) -> Path:
        cfg = {
            "run": {"run_id": "rt-test", "artifacts_root": str(tdp / "artifacts")},
            "universe": {"symbols": ["BTCUSDT"]},
            "acquisition": {
                "days": 1,
                "coinglass": {
                    "base_url": "https://open-api-v4.coinglass.com",
                    "api_key_env": "COINGLASS_API_KEY",
                    "exchange": "binance",
                    "quote": "USDT",
                    "intervals": {"futures_ohlcv": "5m", "funding_8h": "8h", "oi_ohlc": "5m", "orderbook_sample": "last_of_5m"},
                    "per_series_mode": {"futures_ohlcv": "exchange", "funding_8h": "exchange", "oi_5m": "aggregated", "orderbook": "exchange"},
                },
            },
            "conventions": {"symbol_to_coin": {"BTCUSDT": "BTC"}, "orderbook": {"max_snapshot_age_s": 60}},
        }
        import yaml

        p = tdp / "cfg.yaml"
        p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        return p

    def test_realtime_once_invokes_stages(self):
        os.environ["COINGLASS_API_KEY"] = "DUMMY"
        try:
            with tempfile.TemporaryDirectory() as td:
                tdp = Path(td)
                cfg_path = self._cfg_path(tdp)
                data_root = tdp / "data"
                features_root = tdp / "features"
                artifacts_root = tdp / "artifacts_rt"

                with mock.patch("cryptostorm.realtime.engine.run_retrieve") as m_ret, \
                     mock.patch("cryptostorm.realtime.engine.update_features_last") as m_feat, \
                     mock.patch("cryptostorm.realtime.engine.run_backtest") as m_bt:
                    rc = realtime_main(["run", str(cfg_path), "--data", str(data_root), "--features", str(features_root), "--artifacts", str(artifacts_root), "--once"])  # noqa: E501
                    self.assertEqual(rc, 0)
                    self.assertTrue(m_ret.called)
                    self.assertTrue(m_feat.called)
                    self.assertTrue(m_bt.called)
        finally:
            del os.environ["COINGLASS_API_KEY"]


if __name__ == "__main__":
    unittest.main()

