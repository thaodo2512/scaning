from __future__ import annotations

import tempfile
from pathlib import Path
import unittest
import csv

from cryptostorm.alerts import main as alerts_main


class TestAlertsMerge(unittest.TestCase):
    def _write_cfg(self, tdp: Path) -> Path:
        cfg = {
            "run": {"run_id": "r1", "artifacts_root": str(tdp / "artifacts")},
            "universe": {"symbols": ["BTCUSDT", "ETHUSDT"]},
        }
        import yaml  # type: ignore
        p = tdp / "cfg.yaml"
        p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        return p

    def test_merge_alerts(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cfg_path = self._write_cfg(tdp)
            art = tdp / "artifacts" / "r1" / "alerts"
            art.mkdir(parents=True, exist_ok=True)
            for sym, ts in ("BTCUSDT", 1700000000000), ("ETHUSDT", 1700000100000):
                with (art / f"{sym}.csv").open("w", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=["ts", "symbol", "kind", "score", "threshold"])
                    w.writeheader()
                    w.writerow({"ts": ts, "symbol": sym, "kind": "storm", "score": 0.6, "threshold": 0.5})
            rc = alerts_main(["merge", str(cfg_path)])
            self.assertEqual(rc, 0)
            merged = (tdp / "artifacts" / "r1" / "alerts" / "all_alerts.csv").read_text(encoding="utf-8").splitlines()
            self.assertGreaterEqual(len(merged), 3)  # header + 2 rows
            self.assertIn("BTCUSDT", merged[1])
            self.assertIn("ETHUSDT", merged[2])


if __name__ == "__main__":
    unittest.main()

