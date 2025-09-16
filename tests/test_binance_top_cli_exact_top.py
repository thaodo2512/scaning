from __future__ import annotations

from pathlib import Path
import tempfile


def test_binance_top_cli_updates_config_exact_top(monkeypatch):
    # Exercise the module CLI to update a config file and ensure it writes exactly --top symbols
    from cryptostorm.universe.binance import main as cli_main
    from cryptostorm.universe import binance as mod
    import yaml  # type: ignore

    N = 180
    syms = [f"ZZ{i}USDT" for i in range(N)]
    monkeypatch.setattr(mod, "_futures_usdt_perp_symbols", lambda verbose=False: list(syms))
    # Keep metrics trivial to avoid heavy work
    monkeypatch.setattr(
        mod,
        "_metrics_for_symbol",
        lambda symbol, end_ms, rps_delay_s: (1000.0, 0.0, 0.0, 0.0, 30, 0, 500.0),
    )

    with tempfile.TemporaryDirectory() as td:
        cfgp = Path(td) / "cfg.yaml"
        # Minimal config scaffold
        cfgp.write_text(yaml.safe_dump({"run": {"run_id": "t", "artifacts_root": "./artifacts"}}), encoding="utf-8")
        # Ask for 150 and assert the config is updated to 150
        rc = cli_main(["--top", "150", "--out", str(cfgp), "--print"])  # type: ignore[arg-type]
        assert rc == 0
        cfg = yaml.safe_load(cfgp.read_text(encoding="utf-8"))
        arr = (cfg.get("universe") or {}).get("symbols") or []
        assert isinstance(arr, list) and len(arr) == 150

