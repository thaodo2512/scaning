from cryptostorm.backtest.engine import resolve_threshold_q, _quantile


def test_threshold_moves_boundary():
    # synthetic train scores (monotonic increasing)
    train = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 1.0, 1.1, 1.2, 1.5]
    # configs with different qs
    cfg_hi = {"model": {"threshold_q": 0.98}}
    cfg_lo = {"model": {"threshold_q": 0.90}}

    q_hi = resolve_threshold_q(cfg_hi, "AAAUSDT", tier=None)
    q_lo = resolve_threshold_q(cfg_lo, "AAAUSDT", tier=None)
    thr_hi = _quantile(train, q_hi)
    thr_lo = _quantile(train, q_lo)
    assert thr_hi > thr_lo

    # online scores: fewer exceedances for higher threshold
    online = [0.55, 0.65, 0.95, 1.05, 1.25]
    n_hi = sum(1 for v in online if v > thr_hi)
    n_lo = sum(1 for v in online if v > thr_lo)
    assert n_hi <= n_lo

