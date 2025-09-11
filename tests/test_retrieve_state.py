from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from cryptostorm.config import validate_config
from cryptostorm.retrieve.coinglass import run_retrieve


def cfg_one(tmp: Path):
    return {
        "run": {"run_id": "20250101-000000", "artifacts_root": str(tmp / "artifacts")},
        "universe": {"symbols": ["BTCUSDT"]},
        "acquisition": {
            "days": 1,
            "coinglass": {
                "base_url": "https://open-api-v4.coinglass.com",
                "api_key_env": "COINGLASS_API_KEY",
                "exchange": "binance",
                "quote": "USDT",
                "paging": {"page_limit": 50, "backoff_initial_s": 0.1, "backoff_max_s": 0.2},
                "intervals": {"futures_ohlcv": "5m"},
                "per_series_mode": {"futures_ohlcv": "exchange"},
            },
            "enable": {"futures_ohlcv_5m": True},
        },
        "conventions": {"symbol_to_coin": {"BTCUSDT": "BTC"}},
    }


class TestRetrieveState(unittest.TestCase):
    def test_delta_start_uses_sidecar_when_present(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cfg = cfg_one(tdp)
            eff, warns, errs = validate_config(cfg, require_env=False)
            self.assertEqual(errs, [])

            out_root = tdp / "data"
            sym_dir = out_root / "BTCUSDT"
            state_dir = sym_dir / ".state"
            state_dir.mkdir(parents=True, exist_ok=True)
            # seed sidecar with last_ts
            last_ts = 1700000000000
            (state_dir / "futures_ohlcv_5m.json").write_text(json.dumps({"last_ts": last_ts}), encoding="utf-8")

            calls = []

            def page_iter(base_url, headers, path, params, page_limit, backoff_initial, backoff_max):
                calls.append({"path": path, "params": dict(params)})
                return iter(())

            with mock.patch("cryptostorm.retrieve.coinglass._page_iter", side_effect=page_iter):
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

            # Assert startTime == last_ts + 1 (aligned up to step internally)
            self.assertTrue(calls, "No calls were made")
            st = calls[0]["params"]["startTime"]
            self.assertGreaterEqual(st, last_ts + 1)


if __name__ == "__main__":
    unittest.main()

