from __future__ import annotations

import argparse
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
import pickle

from ..config import EffectiveConfig, load_config, validate_config
import logging


def _read_csv_features(fp: Path) -> List[Dict[str, Any]]:
    import csv

    if not fp.exists():
        return []
    with fp.open("r", encoding="utf-8") as f:
        rdr = csv.DictReader(f)
        rows: List[Dict[str, Any]] = []
        for r in rdr:
            obj: Dict[str, Any] = {}
            for k, v in r.items():
                if k == "symbol":
                    obj[k] = v
                    continue
                if k == "ts":
                    try:
                        obj[k] = int(v)
                    except Exception:
                        obj[k] = None
                    continue
                if v is None or v == "":
                    obj[k] = math.nan
                    continue
                try:
                    obj[k] = float(v)
                except Exception:
                    obj[k] = math.nan
            rows.append(obj)
        return rows


def _quantile(values: Sequence[float], q: float) -> float:
    xs = [x for x in values if isinstance(x, (int, float)) and not math.isnan(x)]
    if not xs:
        return math.nan
    xs.sort()
    pos = (len(xs) - 1) * min(max(q, 0.0), 1.0)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(xs[lo])
    frac = pos - lo
    return float(xs[lo] * (1 - frac) + xs[hi] * frac)


def _coverage(values: Sequence[float]) -> float:
    total = len(values)
    if total == 0:
        return 0.0
    ok = sum(1 for v in values if isinstance(v, (int, float)) and not math.isnan(v))
    return ok / total


def _select_features(rows: List[Dict[str, Any]], min_cov: float) -> List[str]:
    if not rows:
        return []
    keys = [k for k in rows[0].keys() if k not in {"ts", "symbol", "data_ok"}]
    selected: List[str] = []
    for k in keys:
        vals = [float(r.get(k)) if isinstance(r.get(k), (int, float)) else math.nan for r in rows]
        if _coverage(vals) >= min_cov:
            # also require variance
            xs = [v for v in vals if not math.isnan(v)]
            if len(xs) >= 3 and max(xs) > min(xs):
                selected.append(k)
    return selected


@dataclass
class RobustStats:
    median: float
    q1: float
    q3: float
    low: float
    high: float


def _robust_stats(vec: Sequence[float], clip_low: float, clip_high: float) -> RobustStats:
    xs = [x for x in vec if isinstance(x, (int, float)) and not math.isnan(x)]
    if not xs:
        return RobustStats(math.nan, math.nan, math.nan, math.nan, math.nan)
    med = _quantile(xs, 0.5)
    q1 = _quantile(xs, 0.25)
    q3 = _quantile(xs, 0.75)
    lo = _quantile(xs, clip_low)
    hi = _quantile(xs, clip_high)
    return RobustStats(med, q1, q3, lo, hi)


def _scale_value(x: float, st: RobustStats) -> float:
    if math.isnan(x):
        x = st.median
    # clip
    x = min(max(x, st.low), st.high)
    iqr = st.q3 - st.q1
    if not isinstance(iqr, (int, float)) or abs(iqr) < 1e-12:
        iqr = 1.0
    return (x - st.median) / iqr


def _fit_iforest_train_matrix(train_rows: List[Dict[str, Any]], features: List[str], clip_low: float, clip_high: float) -> Tuple[List[RobustStats], List[List[float]], List[float]]:
    # Compute robust stats per feature and scale training data
    stats: List[RobustStats] = []
    for k in features:
        vec = [float(r.get(k)) if isinstance(r.get(k), (int, float)) else math.nan for r in train_rows]
        stats.append(_robust_stats(vec, clip_low, clip_high))
    X: List[List[float]] = []
    for r in train_rows:
        row = []
        for i, k in enumerate(features):
            v = float(r.get(k)) if isinstance(r.get(k), (int, float)) else math.nan
            row.append(_scale_value(v, stats[i]))
        X.append(row)
    return stats, X, [float(r.get("ts")) for r in train_rows]


def _transform_rows(rows: List[Dict[str, Any]], features: List[str], stats: List[RobustStats]) -> List[List[float]]:
    X: List[List[float]] = []
    for r in rows:
        row: List[float] = []
        for i, k in enumerate(features):
            v = float(r.get(k)) if isinstance(r.get(k), (int, float)) else math.nan
            row.append(_scale_value(v, stats[i]))
        X.append(row)
    return X


def _iforest_scores(X_train: List[List[float]], X_eval: List[List[float]], params: Mapping[str, Any], random_state: int) -> Tuple[List[float], List[float]]:
    # Try sklearn, otherwise fall back to simple absolute-z aggregate
    try:
        from sklearn.ensemble import IsolationForest  # type: ignore

        # Normalize and cap parameters to avoid warnings (e.g., max_samples > n_train)
        eff = dict(params or {})
        n_train = len(X_train)
        ms = eff.get("max_samples")
        if isinstance(ms, (int, float)) and n_train > 0:
            if ms > n_train:
                eff["max_samples"] = n_train
        allowed = {"n_estimators", "max_samples", "max_features", "contamination", "bootstrap", "n_jobs"}
        kwargs = {k: v for k, v in eff.items() if k in allowed and v is not None}

        model = IsolationForest(random_state=random_state, **kwargs)
        model.fit(X_train)
        train_scores = (-model.decision_function(X_train)).tolist()
        eval_scores = (-model.decision_function(X_eval)).tolist()
        return train_scores, eval_scores
    except Exception:
        # Fallback: sum of absolute values across features as anomaly proxy
        def agg_abs(X: List[List[float]]) -> List[float]:
            return [float(sum(abs(v) for v in row)) for row in X]

        return agg_abs(X_train), agg_abs(X_eval)


def _save_online_artifact(models_dir: Path, symbol: str, *, features: List[str], stats: List[RobustStats], threshold: float, model_obj: Optional[Any], meta: Dict[str, Any]) -> None:
    """Atomically persist online artifact (JSON + optional pickle) to avoid races with realtime readers."""
    models_dir.mkdir(parents=True, exist_ok=True)
    # Prepare JSON payload
    stats_arr = [
        {"median": s.median, "q1": s.q1, "q3": s.q3, "low": s.low, "high": s.high}
        for s in stats
    ]
    payload = {
        "symbol": symbol,
        "features": features,
        "stats": stats_arr,
        "threshold": threshold,
        **meta,
    }
    meta_fp = models_dir / f"{symbol}.json"
    pkl_fp = models_dir / f"{symbol}.pkl"

    # 1) Write model pickle first (temp + replace) so JSON never points to a non-existent pkl for long
    if model_obj is not None:
        try:
            tmp_pkl = pkl_fp.with_suffix(pkl_fp.suffix + ".tmp")
            with tmp_pkl.open("wb") as fh:
                pickle.dump(model_obj, fh)
                try:
                    fh.flush()
                    os.fsync(fh.fileno())
                except Exception:
                    pass
            tmp_pkl.replace(pkl_fp)
        except Exception:
            # best-effort; if pkl fails, continue with JSON only
            pass

    # 2) Write JSON atomically (temp + replace)
    try:
        tmp_js = meta_fp.with_suffix(meta_fp.suffix + ".tmp")
        tmp_js.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        tmp_js.replace(meta_fp)
    except Exception:
        # If JSON write fails, leave previous artifact in place
        pass


def _load_online_artifact(models_dir: Path, symbol: str) -> Optional[Dict[str, Any]]:
    meta_fp = models_dir / f"{symbol}.json"
    if not meta_fp.exists():
        return None
    try:
        meta = json.loads(meta_fp.read_text(encoding="utf-8") or "{}")
        model = None
        pkl_fp = models_dir / f"{symbol}.pkl"
        if pkl_fp.exists():
            try:
                with pkl_fp.open("rb") as fh:
                    model = pickle.load(fh)
            except Exception:
                model = None
        meta["_model"] = model
        return meta
    except Exception:
        return None


def _symbol_tier(cfg: Mapping[str, Any], symbol: str) -> str:
    tiering = (cfg.get("universe") or {}).get("tiering") or {}
    a = tiering.get("A")
    if isinstance(a, list) and symbol in a:
        return "A"
    return "default"


def _retrain_block(ts: int, every_hours: int) -> int:
    block_ms = every_hours * 60 * 60 * 1000
    return (ts // block_ms) * block_ms


def _train_window_start(block_start: int, window_days: int) -> int:
    return block_start - window_days * 24 * 60 * 60 * 1000


def _clone_eff_for_symbols(eff: EffectiveConfig, symbols: List[str]) -> EffectiveConfig:
    from dataclasses import replace

    sub_map = {k: v for k, v in eff.symbol_to_coin.items() if k in symbols}
    return replace(eff, symbols=list(symbols), symbol_to_coin=sub_map)


def _backtest_one_symbol(
    cfg: Mapping[str, Any],
    eff: EffectiveConfig,
    *,
    features_root: Path,
    out_root: Path,
    features_interval: str,
    train_window_days: int,
    retrain_every_hours: int,
    threshold_q: float,
    min_cov: float,
    clip_low: float,
    clip_high: float,
    if_defaults: Mapping[str, Any],
    per_tier: Mapping[str, Any],
    random_state: int,
    pct_move: float,
    horizons: List[int],
    symbol: str,
) -> Optional[Dict[str, Any]]:
    LOG = logging.getLogger("cryptostorm.backtest")
    sym = symbol
    # Select features file by requested interval
    if features_interval == "15m":
        feat_fp = features_root / sym / "features_15m.csv"
    elif features_interval == "5m":
        feat_fp = features_root / sym / "features_5m.csv"
    else:  # auto
        feat_fp = features_root / sym / "features_15m.csv"
        if not feat_fp.exists():
            feat_fp = features_root / sym / "features_5m.csv"
    rows = _read_csv_features(feat_fp)
    rows.sort(key=lambda r: (r.get("ts") or 0))
    if not rows:
        return None

    # Prepare scores array initialized as NaN
    scores: List[float] = [math.nan] * len(rows)
    thresholds: List[float] = [math.nan] * len(rows)

    # Walk forward by retrain blocks
    blocks: Dict[int, List[int]] = {}
    for idx, r in enumerate(rows):
        ts = int(r.get("ts") or 0)
        b = _retrain_block(ts, retrain_every_hours)
        blocks.setdefault(b, []).append(idx)

    last_artifact: Optional[Dict[str, Any]] = None
    for b_start, idxs in sorted(blocks.items()):
        # Training window rows: those with ts in [w_start, b_start)
        w_start = _train_window_start(b_start, train_window_days)
        train_rows = [r for r in rows if (r.get("ts") or 0) >= w_start and (r.get("ts") or 0) < b_start]
        if len(train_rows) < 10:
            continue
        features = _select_features(train_rows, min_cov)
        if not features:
            continue
        # Fit scaler + model
        stats, X_train, _ = _fit_iforest_train_matrix(train_rows, features, clip_low, clip_high)
        eval_rows = [rows[i] for i in idxs]
        X_eval = _transform_rows(eval_rows, features, stats)

        # Choose IF params per tier
        tier = _symbol_tier(cfg, sym)
        params = {**if_defaults, **(per_tier.get(tier) or {})}
        train_scores, eval_scores = _iforest_scores(X_train, X_eval, params, random_state)
        thr = _quantile(train_scores, threshold_q)
        # Record artifact snapshot (latest wins)
        last_artifact = {
            "features": features,
            "stats": stats,
            "threshold": float(thr),
            "params": params,
            "block_start": int(b_start),
            "window_start": int(w_start),
        }
        for j, i in enumerate(idxs):
            scores[i] = float(eval_scores[j])
            thresholds[i] = float(thr)

    # Build alerts using persistence + cooldown
    alerts_cfg = (cfg.get("alerts") or {})
    # Back-compat: prefer *_bars keys; fall back to legacy *_5m
    persist_k = int(alerts_cfg.get("persist_k_bars", alerts_cfg.get("persist_k_5m", 2)))
    confirm_map = alerts_cfg.get("storm_confirm_k_bars", alerts_cfg.get("storm_confirm_k_5m", {"A": 1, "default": 2}))
    cooldown_bars = int(alerts_cfg.get("cooldown_bars", 12))
    tier = _symbol_tier(cfg, sym)
    confirm_k = int((confirm_map.get(tier) if isinstance(confirm_map, Mapping) else None) or confirm_map.get("default", 2))
    ge_count = 0
    cooldown = 0
    pre_alerts: List[Dict[str, Any]] = []
    storms: List[Dict[str, Any]] = []
    for i, r in enumerate(rows):
        s = scores[i]
        thr = thresholds[i]
        if isinstance(s, float) and isinstance(thr, float) and not math.isnan(s) and not math.isnan(thr) and s >= thr:
            ge_count += 1
        else:
            ge_count = 0
        if ge_count == persist_k:
            pre_alerts.append({"ts": int(r.get("ts") or 0), "symbol": sym, "score": s, "threshold": thr, "kind": "pre_alert"})
        if cooldown > 0:
            cooldown -= 1
        elif ge_count == confirm_k:
            storms.append({"ts": int(r.get("ts") or 0), "symbol": sym, "score": s, "threshold": thr, "kind": "storm"})
            cooldown = cooldown_bars

    # Write per-symbol outputs
    import csv

    out_alerts = out_root / "alerts"
    out_scores = out_root / "scores"
    out_alerts.mkdir(parents=True, exist_ok=True)
    out_scores.mkdir(parents=True, exist_ok=True)

    sco_fp = out_scores / f"{sym}.csv"
    with sco_fp.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ts", "symbol", "score", "threshold"])
        for i, r in enumerate(rows):
            w.writerow([int(r.get("ts") or 0), sym, scores[i], thresholds[i]])

    def write_alerts(fp: Path, arr: List[Dict[str, Any]]):
        with fp.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["ts", "symbol", "kind", "score", "threshold"])
            w.writeheader()
            for a in arr:
                w.writerow(a)

    write_alerts(out_alerts / f"{sym}.csv", pre_alerts + storms)

    # Persist online model artifact for this symbol (latest block only)
    if last_artifact is None and len(rows) >= 10:
        # Fallback: build an artifact from the available history
        train_rows = rows[:-1] if len(rows) > 1 else rows
        features = _select_features(train_rows, min_cov)
        if features:
            stats_fallback, X_train_fb, _ = _fit_iforest_train_matrix(train_rows, features, clip_low, clip_high)
            train_scores_fb, _ = _iforest_scores(X_train_fb, X_train_fb, if_defaults, random_state)
            thr_fb = _quantile(train_scores_fb, threshold_q)
            last_artifact = {
                "features": features,
                "stats": stats_fallback,
                "threshold": float(thr_fb),
                "params": if_defaults,
                "block_start": int(rows[-1].get("ts") or 0),
                "window_start": int(rows[0].get("ts") or 0),
            }
    if last_artifact is not None:
        models_dir = out_root / "models"
        model_obj = None
        try:
            params = last_artifact.get("params", {})
            stats_la = last_artifact.get("stats")
            features_list = last_artifact.get("features") or []
            if stats_la and features_list:
                train_rows_full = [r for r in rows if (r.get("ts") or 0) >= last_artifact["window_start"] and (r.get("ts") or 0) < last_artifact["block_start"]]
                if len(train_rows_full) >= 10:
                    stats2, X_train2, _ = _fit_iforest_train_matrix(train_rows_full, features_list, clip_low, clip_high)
                    from sklearn.ensemble import IsolationForest  # type: ignore

                    eff_params = {k: v for k, v in params.items() if k in {"n_estimators", "max_samples", "max_features", "contamination", "bootstrap", "n_jobs"}}
                    model_obj = IsolationForest(random_state=random_state, **eff_params)
                    model_obj.fit(X_train2)
                    last_artifact["stats"] = stats2
        except Exception:
            model_obj = None
        _save_online_artifact(
            models_dir,
            sym,
            features=last_artifact["features"],
            stats=last_artifact["stats"],
            threshold=float(last_artifact["threshold"]),
            model_obj=model_obj,
            meta={
                "block_start": last_artifact["block_start"],
                "window_start": last_artifact["window_start"],
                "random_state": random_state,
            },
        )

    # Metrics per symbol
    ts_to_close = {int(r.get("ts") or 0): float(r.get("close")) for r in rows if isinstance(r.get("close"), (int, float)) and not math.isnan(r.get("close"))}
    step = 5 * 60 * 1000
    true_positive = 0
    total_storms = len(storms)
    lead_times: List[int] = []
    for a in storms:
        t0 = a["ts"]
        p0 = ts_to_close.get(t0)
        if not isinstance(p0, (int, float)):
            continue
        hit = False
        earliest_lead = None
        for h in horizons:
            horizon_ms = int(h) * 60 * 1000
            end_t = t0 + horizon_ms
            cur_t = t0 + step
            while cur_t <= end_t:
                p = ts_to_close.get(cur_t)
                if isinstance(p, (int, float)):
                    move = abs((p - p0) / p0)
                    if move >= pct_move:
                        hit = True
                        if earliest_lead is None:
                            earliest_lead = int((cur_t - t0) / (60 * 1000))
                        break
                cur_t += step
            if hit:
                break
        if hit:
            true_positive += 1
            if earliest_lead is not None:
                lead_times.append(earliest_lead)

    precision = (true_positive / total_storms) if total_storms else None
    avg_lead = (sum(lead_times) / len(lead_times)) if lead_times else None
    return {
        "symbol": sym,
        "storm_count": total_storms,
        "true_positive": true_positive,
        "precision": precision,
        "avg_lead_min": avg_lead,
    }


def run_backtest(
    cfg: Mapping[str, Any],
    eff: EffectiveConfig,
    *,
    features_root: Path,
    out_root: Path,
    features_interval: str = "auto",
    workers: int = 1,
) -> Dict[str, Any]:
    # Extract model and alert params
    model_cfg = (cfg.get("model") or {})
    alerts_cfg = (cfg.get("alerts") or {})
    labels_cfg = (cfg.get("labels") or {})

    train_window_days = int(model_cfg.get("train_window_days", eff.days))
    retrain_every_hours = int(model_cfg.get("retrain_every_hours", 8))
    threshold_q = float(model_cfg.get("threshold_q", 0.97))
    min_cov = float(model_cfg.get("min_feature_coverage", 0.95))
    scaler = model_cfg.get("scaler", {})
    clip_low, clip_high = (0.01, 0.99)
    if isinstance(scaler, Mapping) and isinstance(scaler.get("clip_quantiles"), list) and len(scaler["clip_quantiles"]) == 2:
        clip_low, clip_high = float(scaler["clip_quantiles"][0]), float(scaler["clip_quantiles"][1])
    if_defaults = model_cfg.get("iforest_defaults", {})
    per_tier = model_cfg.get("per_tier_overrides", {})
    random_state = int(model_cfg.get("random_state", 42))

    persist_k = int(alerts_cfg.get("persist_k_bars", alerts_cfg.get("persist_k_5m", 2)))
    confirm_map = alerts_cfg.get("storm_confirm_k_bars", alerts_cfg.get("storm_confirm_k_5m", {"A": 1, "default": 2}))
    cooldown_bars = int(alerts_cfg.get("cooldown_bars", 12))

    pct_move = float(labels_cfg.get("pct_move", 0.05))
    horizons = labels_cfg.get("horizons_min", [30, 60, 90, 120])
    if not isinstance(horizons, list):
        horizons = [30, 60, 90, 120]

    metrics: Dict[str, Any] = {"symbols": {}, "summary": {}}
    out_alerts = out_root / "alerts"
    out_scores = out_root / "scores"
    metrics_dir = out_root / "metrics"
    out_alerts.mkdir(parents=True, exist_ok=True)
    out_scores.mkdir(parents=True, exist_ok=True)
    metrics_dir.mkdir(parents=True, exist_ok=True)

    symbols = list(eff.symbols)
    if int(workers) > 1:
        # Parallel execution per symbol
        import concurrent.futures as cf
        import os as _os
        import time as _time
        LOG = logging.getLogger("cryptostorm.backtest")
        max_workers = max(1, int(workers))
        LOG.info("parallel backtest: symbols=%d workers=%d interval=%s", len(symbols), max_workers, features_interval)
        # Ensure directories exist before workers write
        (out_root / "alerts").mkdir(parents=True, exist_ok=True)
        (out_root / "scores").mkdir(parents=True, exist_ok=True)
        (out_root / "models").mkdir(parents=True, exist_ok=True)
        submit_ts: dict[str, float] = {}
        done = 0
        total = len(symbols)
        with cf.ProcessPoolExecutor(max_workers=max_workers) as ex:
            futs = {}
            for s in symbols:
                submit_ts[s] = _time.monotonic()
                fut = ex.submit(
                    _backtest_one_symbol,
                    cfg,
                    eff,
                    features_root=features_root,
                    out_root=out_root,
                    features_interval=features_interval,
                    train_window_days=train_window_days,
                    retrain_every_hours=retrain_every_hours,
                    threshold_q=threshold_q,
                    min_cov=min_cov,
                    clip_low=clip_low,
                    clip_high=clip_high,
                    if_defaults=if_defaults,
                    per_tier=per_tier,
                    random_state=random_state,
                    pct_move=pct_move,
                    horizons=horizons,
                    symbol=s,
                )
                futs[fut] = s
            for fut in cf.as_completed(futs):
                s = futs[fut]
                done += 1
                try:
                    res = fut.result()
                    if isinstance(res, dict):
                        metrics["symbols"][res["symbol"]] = {k: res[k] for k in ("storm_count", "true_positive", "precision", "avg_lead_min")}
                    LOG.info("[%d/%d] backtest %s done", done, total, s)
                except Exception as e:  # noqa: BLE001
                    dur = _time.monotonic() - submit_ts.get(s, _time.monotonic())
                    LOG.warning("[%d/%d] backtest %s failed after %.2fs: %s", done, total, s, dur, e)
        # Rollup metrics and write file
        totals = [metrics["symbols"][s]["storm_count"] for s in metrics["symbols"]]
        tps = [metrics["symbols"][s]["true_positive"] for s in metrics["symbols"]]
        total_storms = sum(totals) if totals else 0
        total_tp = sum(tps) if tps else 0
        precision = (total_tp / total_storms) if total_storms else None
        metrics["summary"] = {"storm_count": total_storms, "true_positive": total_tp, "precision": precision}
        (out_root / "metrics").mkdir(parents=True, exist_ok=True)
        (out_root / "metrics" / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
        return metrics

    for sym in symbols:
        # Select features file by requested interval
        if features_interval == "15m":
            feat_fp = features_root / sym / "features_15m.csv"
        elif features_interval == "5m":
            feat_fp = features_root / sym / "features_5m.csv"
        else:  # auto
            feat_fp = features_root / sym / "features_15m.csv"
            if not feat_fp.exists():
                feat_fp = features_root / sym / "features_5m.csv"
        rows = _read_csv_features(feat_fp)
        rows.sort(key=lambda r: (r.get("ts") or 0))
        if not rows:
            continue

        # Prepare scores array initialized as NaN
        scores: List[float] = [math.nan] * len(rows)
        thresholds: List[float] = [math.nan] * len(rows)

        # Walk forward by retrain blocks
        # Build map from block_start -> indices in rows belonging to that block
        blocks: Dict[int, List[int]] = {}
        for idx, r in enumerate(rows):
            ts = int(r.get("ts") or 0)
            b = _retrain_block(ts, retrain_every_hours)
            blocks.setdefault(b, []).append(idx)

        last_artifact: Optional[Dict[str, Any]] = None
        for b_start, idxs in sorted(blocks.items()):
            # Training window rows: those with ts in [w_start, b_start)
            w_start = _train_window_start(b_start, train_window_days)
            train_rows = [r for r in rows if (r.get("ts") or 0) >= w_start and (r.get("ts") or 0) < b_start]
            if len(train_rows) < 10:
                # not enough data
                continue
            features = _select_features(train_rows, min_cov)
            if not features:
                continue
            # Fit scaler + model
            stats, X_train, _ = _fit_iforest_train_matrix(train_rows, features, clip_low, clip_high)
            eval_rows = [rows[i] for i in idxs]
            X_eval = _transform_rows(eval_rows, features, stats)

            # Choose IF params per tier
            tier = _symbol_tier(cfg, sym)
            params = {**if_defaults, **(per_tier.get(tier) or {})}
            train_scores, eval_scores = _iforest_scores(X_train, X_eval, params, random_state)
            thr = _quantile(train_scores, threshold_q)
            # Record artifact snapshot (latest wins)
            last_artifact = {
                "features": features,
                "stats": stats,
                "threshold": float(thr),
                "params": params,
                "block_start": int(b_start),
                "window_start": int(w_start),
            }
            for j, i in enumerate(idxs):
                scores[i] = float(eval_scores[j])
                thresholds[i] = float(thr)

        # Build alerts using persistence + cooldown
        tier = _symbol_tier(cfg, sym)
        confirm_k = int((confirm_map.get(tier) if isinstance(confirm_map, Mapping) else None) or confirm_map.get("default", 2))
        ge_count = 0
        cooldown = 0
        pre_alerts: List[Dict[str, Any]] = []
        storms: List[Dict[str, Any]] = []
        for i, r in enumerate(rows):
            s = scores[i]
            thr = thresholds[i]
            if isinstance(s, float) and isinstance(thr, float) and not math.isnan(s) and not math.isnan(thr) and s >= thr:
                ge_count += 1
            else:
                ge_count = 0
            if ge_count == persist_k:
                pre_alerts.append({"ts": int(r.get("ts") or 0), "symbol": sym, "score": s, "threshold": thr, "kind": "pre_alert"})
            if cooldown > 0:
                cooldown -= 1
            elif ge_count == confirm_k:
                storms.append({"ts": int(r.get("ts") or 0), "symbol": sym, "score": s, "threshold": thr, "kind": "storm"})
                cooldown = cooldown_bars

        # Write per-symbol outputs
        import csv

        sco_fp = out_scores / f"{sym}.csv"
        with sco_fp.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["ts", "symbol", "score", "threshold"])
            for i, r in enumerate(rows):
                w.writerow([int(r.get("ts") or 0), sym, scores[i], thresholds[i]])

        def write_alerts(fp: Path, arr: List[Dict[str, Any]]):
            with fp.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["ts", "symbol", "kind", "score", "threshold"])
                w.writeheader()
                for a in arr:
                    w.writerow(a)

        write_alerts(out_alerts / f"{sym}.csv", pre_alerts + storms)

        # Persist online model artifact for this symbol (latest block only)
        if last_artifact is None and len(rows) >= 10:
            # Fallback: build an artifact from the available history
            train_rows = rows[:-1] if len(rows) > 1 else rows
            features = _select_features(train_rows, min_cov)
            if features:
                stats_fallback, X_train_fb, _ = _fit_iforest_train_matrix(train_rows, features, clip_low, clip_high)
                train_scores_fb, _ = _iforest_scores(X_train_fb, X_train_fb, if_defaults, random_state)
                thr_fb = _quantile(train_scores_fb, threshold_q)
                last_artifact = {
                    "features": features,
                    "stats": stats_fallback,
                    "threshold": float(thr_fb),
                    "params": if_defaults,
                    "block_start": int(rows[-1].get("ts") or 0),
                    "window_start": int(rows[0].get("ts") or 0),
                }
        if last_artifact is not None:
            models_dir = out_root / "models"
            model_obj = None
            try:
                # Try to fit full model on train data for persistence
                params = last_artifact.get("params", {})
                stats = last_artifact.get("stats")
                features_list = last_artifact.get("features") or []
                if stats and features_list:
                    # Recompute X_train to persist model (best-effort)
                    train_rows_full = [r for r in rows if (r.get("ts") or 0) >= last_artifact["window_start"] and (r.get("ts") or 0) < last_artifact["block_start"]]
                    if len(train_rows_full) >= 10:
                        stats2, X_train2, _ = _fit_iforest_train_matrix(train_rows_full, features_list, clip_low, clip_high)
                        from sklearn.ensemble import IsolationForest  # type: ignore

                        eff_params = {k: v for k, v in params.items() if k in {"n_estimators", "max_samples", "max_features", "contamination", "bootstrap", "n_jobs"}}
                        model_obj = IsolationForest(random_state=random_state, **eff_params)
                        model_obj.fit(X_train2)
                        # Use stats2 for persistence
                        last_artifact["stats"] = stats2
            except Exception:
                model_obj = None
            _save_online_artifact(
                models_dir,
                sym,
                features=last_artifact["features"],
                stats=last_artifact["stats"],
                threshold=float(last_artifact["threshold"]),
                model_obj=model_obj,
                meta={
                    "block_start": last_artifact["block_start"],
                    "window_start": last_artifact["window_start"],
                    "random_state": random_state,
                },
            )

        # Labels and metrics
        # Build a map ts->close
        ts_to_close = {int(r.get("ts") or 0): float(r.get("close")) for r in rows if isinstance(r.get("close"), (int, float)) and not math.isnan(r.get("close"))}
        # Build list of all ts
        ts_list = [int(r.get("ts") or 0) for r in rows]
        step = 5 * 60 * 1000
        true_positive = 0
        total_storms = len(storms)
        lead_times: List[int] = []
        for a in storms:
            t0 = a["ts"]
            p0 = ts_to_close.get(t0)
            if not isinstance(p0, (int, float)):
                continue
            hit = False
            earliest_lead = None
            for h in horizons:
                horizon_ms = int(h) * 60 * 1000
                end_t = t0 + horizon_ms
                # iterate bars within horizon and compute max move
                cur_t = t0 + step
                while cur_t <= end_t:
                    p = ts_to_close.get(cur_t)
                    if isinstance(p, (int, float)):
                        move = abs((p - p0) / p0)
                        if move >= pct_move:
                            hit = True
                            if earliest_lead is None:
                                earliest_lead = int((cur_t - t0) / (60 * 1000))
                            break
                    cur_t += step
                if hit:
                    break
            if hit:
                true_positive += 1
                if earliest_lead is not None:
                    lead_times.append(earliest_lead)

        precision = (true_positive / total_storms) if total_storms else None
        avg_lead = (sum(lead_times) / len(lead_times)) if lead_times else None
        metrics["symbols"][sym] = {
            "storm_count": total_storms,
            "true_positive": true_positive,
            "precision": precision,
            "avg_lead_min": avg_lead,
        }

    # Summary rollup
    totals = [metrics["symbols"][s]["storm_count"] for s in metrics["symbols"]]
    tps = [metrics["symbols"][s]["true_positive"] for s in metrics["symbols"]]
    total_storms = sum(totals) if totals else 0
    total_tp = sum(tps) if tps else 0
    precision = (total_tp / total_storms) if total_storms else None
    metrics["summary"] = {"storm_count": total_storms, "true_positive": total_tp, "precision": precision}
    (out_root / "metrics" / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    return metrics


def _score_online_one_symbol(
    cfg: Mapping[str, Any],
    eff: EffectiveConfig,
    *,
    features_root: Path,
    out_root: Path,
    features_interval: str,
    symbol: str,
) -> Optional[Tuple[str, int, float]]:
    import csv as _csv
    sym = symbol
    model_dir = out_root / "models"
    alerts_dir = out_root / "alerts"
    scores_dir = out_root / "scores"
    alerts_dir.mkdir(parents=True, exist_ok=True)
    scores_dir.mkdir(parents=True, exist_ok=True)

    alerts_cfg = (cfg.get("alerts") or {})
    persist_k = int(alerts_cfg.get("persist_k_bars", alerts_cfg.get("persist_k_5m", 2)))
    confirm_map = alerts_cfg.get("storm_confirm_k_bars", alerts_cfg.get("storm_confirm_k_5m", {"A": 1, "default": 2}))
    cooldown_bars = int(alerts_cfg.get("cooldown_bars", 12))

    art = _load_online_artifact(model_dir, sym)
    if not art:
        return None
    # Load latest row from features
    if features_interval == "15m":
        feat_fp = features_root / sym / "features_15m.csv"
    elif features_interval == "5m":
        feat_fp = features_root / sym / "features_5m.csv"
    else:
        feat_fp = features_root / sym / "features_15m.csv"
        if not feat_fp.exists():
            feat_fp = features_root / sym / "features_5m.csv"
    rows = _read_csv_features(feat_fp)
    if not rows:
        return None
    rows.sort(key=lambda r: (r.get("ts") or 0))
    latest = rows[-1]
    ts = int(latest.get("ts") or 0)
    sco_fp = scores_dir / f"{sym}.csv"
    # Skip if this ts already scored
    if sco_fp.exists():
        try:
            with sco_fp.open("r", encoding="utf-8") as f:
                rdr = _csv.DictReader(f)
                last_ts = None
                for r in rdr:
                    try:
                        last_ts = int(r.get("ts") or 0)
                    except Exception:
                        continue
                if last_ts == ts:
                    return (sym, 0, 0.0)
        except Exception:
            pass

    # Transform latest row using persisted stats
    features_list: List[str] = list(art.get("features") or [])
    stats_list: List[RobustStats] = []
    for s in (art.get("stats") or []):
        try:
            stats_list.append(RobustStats(float(s["median"]), float(s["q1"]), float(s["q3"]), float(s["low"]), float(s["high"])) )
        except Exception:
            pass
    x_row = []
    for i, k in enumerate(features_list):
        v = float(latest.get(k)) if isinstance(latest.get(k), (int, float)) else math.nan
        st = stats_list[i] if i < len(stats_list) else RobustStats(0.0, -1.0, 1.0, -3.0, 3.0)
        x_row.append(_scale_value(v, st))

    # Score using model if available, else fallback aggregator
    model = art.get("_model")
    try:
        if model is not None and hasattr(model, "decision_function"):
            score_val = -float(model.decision_function([x_row])[0])
        else:
            score_val = float(sum(abs(v) for v in x_row))
    except Exception:
        score_val = float(sum(abs(v) for v in x_row))
    threshold = float(art.get("threshold", math.nan))

    # Append to scores CSV
    write_header = not sco_fp.exists()
    with sco_fp.open("a", newline="", encoding="utf-8") as f:
        w = _csv.writer(f)
        if write_header:
            w.writerow(["ts", "symbol", "score", "threshold"])
        w.writerow([ts, sym, score_val, threshold])

    # Recompute alerts state efficiently from scores
    try:
        with sco_fp.open("r", encoding="utf-8") as f:
            rdr = list(_csv.DictReader(f))
    except Exception:
        rdr = []
    tier = _symbol_tier(cfg, sym)
    confirm_k = int((confirm_map.get(tier) if isinstance(confirm_map, Mapping) else None) or confirm_map.get("default", 2))
    ge_count = 0
    cooldown = 0
    pre_alert = None
    storm_alert = None
    for r in rdr:
        try:
            t = int(r.get("ts") or 0)
            s = float(r.get("score") or math.nan)
            thr = float(r.get("threshold") or math.nan)
        except Exception:
            continue
        if isinstance(s, float) and isinstance(thr, float) and not math.isnan(s) and not math.isnan(thr) and s >= thr:
            ge_count += 1
        else:
            ge_count = 0
        if ge_count == persist_k:
            pre_alert = {"ts": t, "symbol": sym, "kind": "pre_alert", "score": s, "threshold": thr}
        if cooldown > 0:
            cooldown -= 1
        elif ge_count == confirm_k:
            storm_alert = {"ts": t, "symbol": sym, "kind": "storm", "score": s, "threshold": thr}
            cooldown = cooldown_bars

    al_fp = out_root / "alerts" / f"{sym}.csv"
    new_alerts = 0
    if pre_alert or storm_alert:
        try:
            write_header = not al_fp.exists()
            existing: set[Tuple[int, str]] = set()
            if al_fp.exists():
                with al_fp.open("r", encoding="utf-8") as f:
                    rdr2 = _csv.DictReader(f)
                    for r in rdr2:
                        try:
                            existing.add((int(r.get("ts") or 0), str(r.get("kind") or "")))
                        except Exception:
                            continue
            with al_fp.open("a", newline="", encoding="utf-8") as f:
                w = _csv.DictWriter(f, fieldnames=["ts", "symbol", "kind", "score", "threshold"])
                if write_header:
                    w.writeheader()
                for a in [pre_alert, storm_alert]:
                    if a and (a["ts"], a["kind"]) not in existing:
                        w.writerow(a)
                        new_alerts += 1
        except Exception:
            pass

    return (sym, 1, float(new_alerts))


def score_online(cfg: Mapping[str, Any], eff: EffectiveConfig, *, features_root: Path, out_root: Path, features_interval: str = "auto", workers: int = 1) -> Dict[str, Any]:
    """Score only the latest row per symbol using persisted online artifacts.

    Appends to scores and updates alerts incrementally. Skips symbols without artifacts.
    """
    model_dir = out_root / "models"
    alerts_dir = out_root / "alerts"
    scores_dir = out_root / "scores"
    alerts_dir.mkdir(parents=True, exist_ok=True)
    scores_dir.mkdir(parents=True, exist_ok=True)

    model_cfg = (cfg.get("model") or {})
    alerts_cfg = (cfg.get("alerts") or {})
    persist_k = int(alerts_cfg.get("persist_k_bars", alerts_cfg.get("persist_k_5m", 2)))
    confirm_map = alerts_cfg.get("storm_confirm_k_bars", alerts_cfg.get("storm_confirm_k_5m", {"A": 1, "default": 2}))
    cooldown_bars = int(alerts_cfg.get("cooldown_bars", 12))

    summary: Dict[str, Any] = {"symbols": {}, "appended_scores": 0, "new_alerts": 0}

    symbols = list(eff.symbols)
    if int(workers) > 1:
        import concurrent.futures as cf
        import time as _time
        import os as _os
        LOG = logging.getLogger("cryptostorm.backtest")
        max_workers = max(1, int(workers))
        LOG.info("parallel score_online: symbols=%d workers=%d interval=%s", len(symbols), max_workers, features_interval)
        done = 0
        total = len(symbols)
        submit_ts: dict[str, float] = {}
        with cf.ProcessPoolExecutor(max_workers=max_workers) as ex:
            futs = {}
            for s in symbols:
                submit_ts[s] = _time.monotonic()
                futs[ex.submit(_score_online_one_symbol, cfg, eff, features_root=features_root, out_root=out_root, features_interval=features_interval, symbol=s)] = s
            for fut in cf.as_completed(futs):
                s = futs[fut]
                done += 1
                try:
                    res = fut.result()
                    if res is not None:
                        sym, appended, new_alerts = res
                        summary["appended_scores"] += int(appended)
                        summary["new_alerts"] += int(new_alerts)
                        summary["symbols"][sym] = {"appended": int(appended), "new_alerts": int(new_alerts)}
                    LOG.info("[%d/%d] score_online %s done", done, total, s)
                except Exception as e:  # noqa: BLE001
                    dur = _time.monotonic() - submit_ts.get(s, _time.monotonic())
                    LOG.warning("[%d/%d] score_online %s failed after %.2fs: %s", done, total, s, dur, e)
        return summary

    for sym in symbols:
        art = _load_online_artifact(model_dir, sym)
        if not art:
            continue
        # Load latest row from features
        if features_interval == "15m":
            feat_fp = features_root / sym / "features_15m.csv"
        elif features_interval == "5m":
            feat_fp = features_root / sym / "features_5m.csv"
        else:
            feat_fp = features_root / sym / "features_15m.csv"
            if not feat_fp.exists():
                feat_fp = features_root / sym / "features_5m.csv"
        rows = _read_csv_features(feat_fp)
        if not rows:
            continue
        rows.sort(key=lambda r: (r.get("ts") or 0))
        latest = rows[-1]
        ts = int(latest.get("ts") or 0)
        sco_fp = scores_dir / f"{sym}.csv"
        # Skip if this ts already scored
        if sco_fp.exists():
            try:
                with sco_fp.open("r", encoding="utf-8") as f:
                    import csv as _csv

                    rdr = _csv.DictReader(f)
                    last_ts = None
                    for r in rdr:
                        try:
                            last_ts = int(r.get("ts") or 0)
                        except Exception:
                            continue
                    if last_ts == ts:
                        continue
            except Exception:
                pass

        # Transform latest row using persisted stats
        features_list: List[str] = list(art.get("features") or [])
        stats_list: List[RobustStats] = []
        for s in (art.get("stats") or []):
            try:
                stats_list.append(RobustStats(float(s["median"]), float(s["q1"]), float(s["q3"]), float(s["low"]), float(s["high"])) )
            except Exception:
                pass
        x_row = []
        for i, k in enumerate(features_list):
            v = float(latest.get(k)) if isinstance(latest.get(k), (int, float)) else math.nan
            st = stats_list[i] if i < len(stats_list) else RobustStats(0.0, -1.0, 1.0, -3.0, 3.0)
            x_row.append(_scale_value(v, st))

        # Score using model if available, else fallback aggregator
        model = art.get("_model")
        score_val: float
        try:
            if model is not None and hasattr(model, "decision_function"):
                score_val = -float(model.decision_function([x_row])[0])
            else:
                score_val = float(sum(abs(v) for v in x_row))
        except Exception:
            score_val = float(sum(abs(v) for v in x_row))
        threshold = float(art.get("threshold", math.nan))

        # Append to scores CSV
        try:
            import csv as _csv

            write_header = not sco_fp.exists()
            with sco_fp.open("a", newline="", encoding="utf-8") as f:
                w = _csv.writer(f)
                if write_header:
                    w.writerow(["ts", "symbol", "score", "threshold"])
                w.writerow([ts, sym, score_val, threshold])
            summary["appended_scores"] += 1
        except Exception:
            continue

        # Recompute alerts state efficiently from scores
        try:
            with sco_fp.open("r", encoding="utf-8") as f:
                import csv as _csv

                rdr = list(_csv.DictReader(f))
        except Exception:
            rdr = []
        tier = _symbol_tier(cfg, sym)
        confirm_k = int((confirm_map.get(tier) if isinstance(confirm_map, Mapping) else None) or confirm_map.get("default", 2))
        ge_count = 0
        cooldown = 0
        pre_alert = None
        storm_alert = None
        for r in rdr:
            try:
                t = int(r.get("ts") or 0)
                s = float(r.get("score") or math.nan)
                thr = float(r.get("threshold") or math.nan)
            except Exception:
                continue
            if isinstance(s, float) and isinstance(thr, float) and not math.isnan(s) and not math.isnan(thr) and s >= thr:
                ge_count += 1
            else:
                ge_count = 0
            if ge_count == persist_k:
                pre_alert = {"ts": t, "symbol": sym, "kind": "pre_alert", "score": s, "threshold": thr}
            if cooldown > 0:
                cooldown -= 1
            elif ge_count == confirm_k:
                storm_alert = {"ts": t, "symbol": sym, "kind": "storm", "score": s, "threshold": thr}
                cooldown = cooldown_bars

        # Append only latest alerts to alerts CSV
        al_fp = alerts_dir / f"{sym}.csv"
        if pre_alert or storm_alert:
            try:
                import csv as _csv

                write_header = not al_fp.exists()
                existing: set[Tuple[int, str]] = set()
                if al_fp.exists():
                    with al_fp.open("r", encoding="utf-8") as f:
                        rdr2 = _csv.DictReader(f)
                        for r in rdr2:
                            try:
                                existing.add((int(r.get("ts") or 0), str(r.get("kind") or "")))
                            except Exception:
                                continue
                with al_fp.open("a", newline="", encoding="utf-8") as f:
                    w = _csv.DictWriter(f, fieldnames=["ts", "symbol", "kind", "score", "threshold"])
                    if write_header:
                        w.writeheader()
                    for a in [pre_alert, storm_alert]:
                        if a and (a["ts"], a["kind"]) not in existing:
                            w.writerow(a)
                            summary["new_alerts"] += 1
            except Exception:
                pass

        summary["symbols"][sym] = {"ts": ts, "score": score_val, "threshold": threshold}

    return summary


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="CryptoStorm backtest (walk-forward)")
    parser.add_argument("config", type=str)
    parser.add_argument("--features", type=str, default="features")
    parser.add_argument("--artifacts-root", type=str, help="Override artifacts root; defaults to run.artifacts_root/run_id")
    parser.add_argument("--online", action="store_true", help="Score only latest row using persisted artifacts (no retrain)")
    parser.add_argument("--features-interval", type=str, choices=["5m", "15m", "auto"], default="auto", help="Select which features cadence to use (default: auto)")
    parser.add_argument("--workers", type=int, default=1, help="Worker processes for per-symbol parallelism (default: 1)")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    eff, warns, errs = validate_config(cfg, require_env=False)
    if errs:
        for e in errs:
            print(f"error: {e}")
        return 2
    run_id = (cfg.get("run") or {}).get("run_id") or eff.run_id
    artifacts_root = Path(args.artifacts_root) if args.artifacts_root else Path((cfg.get("run") or {}).get("artifacts_root", "./artifacts")) / str(run_id)
    artifacts_root.mkdir(parents=True, exist_ok=True)

    if args.online:
        summary = score_online(
            cfg,
            eff,
            features_root=Path(args.features),
            out_root=artifacts_root,
            features_interval=str(args.features_interval),
            workers=int(args.workers),
        )
        print(json.dumps({"online_summary": summary}))
    else:
        run_backtest(
            cfg,
            eff,
            features_root=Path(args.features),
            out_root=artifacts_root,
            features_interval=str(args.features_interval),
            workers=int(args.workers),
        )
        print(f"Backtest artifacts written to {artifacts_root}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
