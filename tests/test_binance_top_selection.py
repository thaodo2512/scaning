from __future__ import annotations

from pathlib import Path
import tempfile


def test_binance_first_returns_exact_top(monkeypatch):
    # Import module under test
    from cryptostorm.universe import binance as mod

    # Prepare a synthetic Binance symbol list (> top)
    N = 250
    syms = [f"SYM{i}USDT" for i in range(N)]

    def fake_binance_symbols(verbose: bool = False):
        return list(syms)

    def fake_metrics(symbol: str, end_ms: int, rps_delay_s: float):
        # Determine index by parsing the symbol
        i = int(symbol.removeprefix("SYM").removesuffix("USDT"))
        # vol30 and vol24 strictly decreasing by index so ranking is deterministic
        vol30 = float(100000 - i)
        vol7 = 0.0
        ret30 = 0.0
        rv30 = 0.0
        valid = 30
        zero_days = 0
        vol24 = float(50000 - i)
        return vol30, vol7, ret30, rv30, valid, zero_days, vol24

    monkeypatch.setattr(mod, "_futures_usdt_perp_symbols", fake_binance_symbols)
    monkeypatch.setattr(mod, "_metrics_for_symbol", fake_metrics)

    rows = mod.select_top_binance_perps(200, rps=0.0, verbose=False, data_root=None)
    assert len(rows) == 200
    # Ensure ranking kept order by decreasing vol30 (SYM0USDT first)
    assert rows[0].symbol == "SYM0USDT"
    assert rows[-1].symbol == "SYM199USDT"


def test_fallback_to_local_when_binance_empty(monkeypatch):
    from cryptostorm.universe import binance as mod

    # Force Binance candidate list to be empty to trigger fallback
    monkeypatch.setattr(mod, "_futures_usdt_perp_symbols", lambda verbose=False: [])
    # Local metrics stub (shape matches _coinglass_local_metrics return)
    monkeypatch.setattr(
        mod,
        "_coinglass_local_metrics",
        lambda symbol, data_root, now_ms=None: (1.0, 1.0, 1.0, 0.0, 0.0, 30),
    )

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        data_root = tdp
        # Create three local symbols with the expected JSONL filename so they are discovered
        for s in ["AAAUSDT", "BBBUSDT", "CCCUSDT"]:
            symdir = data_root / s
            symdir.mkdir(parents=True, exist_ok=True)
            (symdir / "futures_ohlcv_15m.jsonl").write_text("{}\n", encoding="utf-8")

        rows = mod.select_top_binance_perps(5, rps=0.0, verbose=True, data_root=data_root)
        assert len(rows) == 3
        symbols = sorted(r.symbol for r in rows)
        assert symbols == ["AAAUSDT", "BBBUSDT", "CCCUSDT"]

