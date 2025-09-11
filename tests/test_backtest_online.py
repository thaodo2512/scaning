from __future__ import annotations

import csv
import json
from pathlib import Path
import tempfile
import unittest

from cryptostorm.backtest.engine import main as backtest_main


def _write_features(sym_dir: Path, rows):
    sym_dir.mkdir(parents=True, exist_ok=True)
    fp = sym_dir / "features_5m.csv"
    with fp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "ts","symbol","open","high","low","close","volume","funding_now","funding_pctile_30d","funding_pred_twap_60m","oi_now","oi_pctile_30d","spread_bps","depth_ratio","basis_now","basis_TWAP_60m","basis_TWAP_120m","delta_taker_5m","cvd_perp_5m","cvd_perp_15m","perp_share_60m","liq_notional_5m","liq_count_5m","liq_notional_60m","rv_15m","exch_reserve_flag","etf_flow_flag","data_ok",
        ])
        w.writeheader()
        for r in rows:
            w.writerow(r)


class TestBacktestOnline(unittest.TestCase):
    def _cfg(self, tdp: Path) -> Path:
        cfg = {
            "run": {"run_id": "onlinetest", "artifacts_root": str(tdp / "artifacts")},
            "universe": {"symbols": ["BTCUSDT"], "tiering": {"A": ["BTCUSDT"], "B": 0, "C": 0}},
            "acquisition": {"days": 1, "coinglass": {"intervals": {"futures_ohlcv": "5m", "funding_8h": "8h", "oi_ohlc": "5m"}, "per_series_mode": {"futures_ohlcv": "exchange", "funding_8h": "exchange", "oi_5m": "aggregated"}}},
            "model": {
                "random_state": 42,
                "train_window_days": 1,
                "retrain_every_hours": 1,
                "threshold_q": 0.8,
                "min_feature_coverage": 0.0,
                "scaler": {"type": "robust", "clip_quantiles": [0.01, 0.99]},
                "iforest_defaults": {"contamination": "auto", "max_features": 0.7, "bootstrap": False},
                "per_tier_overrides": {"A": {"n_estimators": 50, "max_samples": 64}},
            },
            "alerts": {"persist_k_5m": 1, "storm_confirm_k_5m": {"A": 1, "default": 1}, "cooldown_bars": 2},
        }
        import yaml
        p = tdp / "cfg.yaml"
        p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        return p

    def test_online_scoring_appends_one_row(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cfg_path = self._cfg(tdp)
            # Prepare features: 20 rows
            base = 1700000000000
            rows = []
            for i in range(20):
                ts = base + i * 5 * 60 * 1000
                rows.append({
                    "ts": ts,
                    "symbol": "BTCUSDT",
                    "open": 100.0,
                    "high": 100.0,
                    "low": 100.0,
                    "close": 100.0 + (10.0 if i == 10 else 0.0),
                    "volume": 1.0,
                    "funding_now": 0.0,
                    "funding_pctile_30d": 0.5,
                    "funding_pred_twap_60m": 0.0,
                    "oi_now": 1.0,
                    "oi_pctile_30d": 0.5,
                    "spread_bps": 5.0,
                    "depth_ratio": 0.0,
                    "basis_now": 0.0,
                    "basis_TWAP_60m": 0.0,
                    "basis_TWAP_120m": 0.0,
                    "delta_taker_5m": 0.0,
                    "cvd_perp_5m": 0.0,
                    "cvd_perp_15m": 0.0,
                    "perp_share_60m": 0.5,
                    "liq_notional_5m": 0.0,
                    "liq_count_5m": 0.0,
                    "liq_notional_60m": 0.0,
                    "rv_15m": 0.0,
                    "exch_reserve_flag": False,
                    "etf_flow_flag": False,
                    "data_ok": True,
                })
            features_root = tdp / "features"
            _write_features(features_root / "BTCUSDT", rows)

            # Train backtest to create artifacts & initial scores
            rc = backtest_main([str(cfg_path), "--features", str(features_root), "--artifacts-root", str(tdp / "art_root")])
            self.assertEqual(rc, 0)
            sco_fp = tdp / "art_root" / "scores" / "BTCUSDT.csv"
            with sco_fp.open() as f:
                initial_count = sum(1 for _ in csv.DictReader(f))

            # Append one new features row
            new_ts = base + 20 * 5 * 60 * 1000
            rows.append({**rows[-1], "ts": new_ts})
            _write_features(features_root / "BTCUSDT", rows)

            # Online scoring should append exactly one new row
            rc2 = backtest_main([str(cfg_path), "--features", str(features_root), "--artifacts-root", str(tdp / "art_root"), "--online"])
            self.assertEqual(rc2, 0)
            with sco_fp.open() as f2:
                new_count = sum(1 for _ in csv.DictReader(f2))
            self.assertEqual(new_count, initial_count + 1)


if __name__ == "__main__":
    unittest.main()

