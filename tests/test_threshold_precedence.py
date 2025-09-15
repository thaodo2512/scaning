import types

from cryptostorm.backtest.engine import resolve_threshold_q


def mkcfg(glob=0.985, per_tier=None, per_symbol=None):
    cfg = {
        "model": {
            "threshold_q": glob,
            "threshold_q_per_tier": per_tier or {},
            "threshold_q_per_symbol": per_symbol or {},
        }
    }
    return cfg


def test_per_symbol_wins():
    cfg = mkcfg(0.980, {"A": 0.990, "default": 0.985}, {"BTCUSDT": 0.995})
    assert resolve_threshold_q(cfg, "BTCUSDT", tier="A") == 0.995


def test_per_tier_used_when_no_symbol_match():
    cfg = mkcfg(0.980, {"A": 0.990, "default": 0.985}, {})
    assert resolve_threshold_q(cfg, "ETHUSDT", tier="A") == 0.990


def test_per_tier_default_used_when_tier_unknown():
    cfg = mkcfg(0.980, {"default": 0.987}, {})
    assert resolve_threshold_q(cfg, "XRPUSDT", tier=None) == 0.987


def test_global_fallback():
    cfg = mkcfg(0.981, {}, {})
    assert resolve_threshold_q(cfg, "SOLUSDT", tier="Z") == 0.981

