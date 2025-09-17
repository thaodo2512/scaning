from __future__ import annotations

import csv
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock


class TestTelegramNotifyNoFilters(unittest.TestCase):
    def _write_cfg(self, tdp: Path, symbols=("AAAUSDT",), run_id: str = "nf") -> Path:
        cfg = {
            "run": {"run_id": run_id, "artifacts_root": str(tdp / "artifacts")},
            "universe": {"symbols": list(symbols)},
            "acquisition": {"days": 1, "coinglass": {"intervals": {"futures_ohlcv": "15m"}, "per_series_mode": {"futures_ohlcv": "exchange"}}},
            "notifications": {"telegram": {"only_new": True, "cooldown_min": 60, "limit": 1}},
        }
        import yaml  # type: ignore

        p = tdp / "cfg.yaml"
        p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        return p

    def _write_alerts(self, art_root: Path, run_id: str, symbol: str, rows: list[dict]) -> Path:
        adir = art_root / run_id / "alerts"
        adir.mkdir(parents=True, exist_ok=True)
        fp = adir / f"{symbol}.csv"
        with fp.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["ts", "symbol", "kind", "score", "threshold"])
            w.writeheader()
            for r in rows:
                w.writerow(r)
        return fp

    def test_no_filters_overrides_all(self):
        from cryptostorm.notify.telegram import main as tg_main

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            sym = "AAAUSDT"
            cfg_path = self._write_cfg(tdp, symbols=(sym,), run_id="nf")
            base = 1700000000000
            # Three alerts (would be limited/only-new/cooldown by config), expect all to send with --no-filters
            rows = [
                {"ts": base + 0, "symbol": sym, "kind": "pre_alert", "score": 0.52, "threshold": 0.50},
                {"ts": base + 60000, "symbol": sym, "kind": "storm", "score": 0.60, "threshold": 0.50},
                {"ts": base + 120000, "symbol": sym, "kind": "storm", "score": 0.62, "threshold": 0.50},
            ]
            self._write_alerts(tdp / "artifacts", "nf", sym, rows)

            calls: list[str] = []

            def fake_send(token: str, chat_id: str, text: str, **kwargs):
                calls.append(text)
                return 1

            with mock.patch("cryptostorm.notify.telegram._send_telegram", side_effect=fake_send):
                with mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "T", "TELEGRAM_CHAT_ID": "1"}, clear=False):
                    buf = io.StringIO()
                    with mock.patch("sys.stdout", new=buf):
                        rc = tg_main([str(cfg_path), "--no-filters", "--kinds", "storm,pre_alert"])  # no --artifacts needed
                    self.assertEqual(rc, 0)
            self.assertEqual(len(calls), 3)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

