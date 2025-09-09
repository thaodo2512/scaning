import csv
import json
from pathlib import Path
import tempfile
import unittest

from cryptostorm.backtest.engine import main as backtest_main


def write_features(sym_dir: Path, rows):
    sym_dir.mkdir(parents=True, exist_ok=True)
    fp = sym_dir / "features_5m.csv"
    import csv as _csv
    with fp.open("w", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=["ts", "symbol", "open", "high", "low", "close", "volume", "funding_now", "oi_now", "spread_bps", "data_ok"])
        w.writeheader()
        for r in rows:
            w.writerow(r)


class TestBacktest(unittest.TestCase):
    def test_backtest_generates_scores_alerts_metrics(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            # Config with lenient settings to trigger alerts easily
            cfg_path = tdp / "cfg.yaml"
            cfg = {
                "run": {"run_id": "test", "artifacts_root": str(tdp / "artifacts")},
                "universe": {"symbols": ["BTCUSDT"], "tiering": {"A": ["BTCUSDT"], "B": 0, "C": 0}},
                "acquisition": {
                    "days": 1,
                    "coinglass": {
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
                },
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
                "labels": {"pct_move": 0.01, "horizons_min": [15]},
            }
            import yaml
            cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

            # Create features with a spike to trigger anomaly
            base = 1700000000000
            rows = []
            for i in range(24):  # 2 hours
                ts = base + i * 5 * 60 * 1000
                row = {
                    "ts": ts,
                    "symbol": "BTCUSDT",
                    "open": 100.0,
                    "high": 100.0,
                    "low": 100.0,
                    "close": 100.0 + (10.0 if i == 10 else 0.0),  # spike
                    "volume": 1.0,
                    "funding_now": 0.0,
                    "oi_now": 1.0,
                    "spread_bps": 5.0,
                    "data_ok": True,
                }
                rows.append(row)
            features_root = tdp / "features"
            write_features(features_root / "BTCUSDT", rows)

            # Run backtest
            rc = backtest_main([str(cfg_path), "--features", str(features_root), "--artifacts-root", str(tdp / "art_root")])
            self.assertEqual(rc, 0)

            # Check artifacts
            sco = tdp / "art_root" / "scores" / "BTCUSDT.csv"
            al = tdp / "art_root" / "alerts" / "BTCUSDT.csv"
            met = tdp / "art_root" / "metrics" / "metrics.json"
            self.assertTrue(sco.exists())
            self.assertTrue(al.exists())
            self.assertTrue(met.exists())
            # Ensure at least one storm alert
            with al.open() as f:
                reader = csv.DictReader(f)
                kinds = [r["kind"] for r in reader]
            self.assertIn("storm", kinds)
            # Metrics json loads
            js = json.loads(met.read_text())
            self.assertIn("summary", js)


if __name__ == "__main__":
    unittest.main()
