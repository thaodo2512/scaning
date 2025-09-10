from __future__ import annotations

import argparse
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..config import EffectiveConfig, load_config, validate_config


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


def run_backtest(cfg: Mapping[str, Any], eff: EffectiveConfig, *, features_root: Path, out_root: Path) -> Dict[str, Any]:
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

    persist_k = int(alerts_cfg.get("persist_k_5m", 2))
    confirm_map = alerts_cfg.get("storm_confirm_k_5m", {"A": 1, "default": 2})
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

    for sym in eff.symbols:
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


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="CryptoStorm backtest (walk-forward)")
    parser.add_argument("config", type=str)
    parser.add_argument("--features", type=str, default="features")
    parser.add_argument("--artifacts-root", type=str, help="Override artifacts root; defaults to run.artifacts_root/run_id")
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

    run_backtest(cfg, eff, features_root=Path(args.features), out_root=artifacts_root)
    print(f"Backtest artifacts written to {artifacts_root}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
