from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from ..config import EffectiveConfig, load_config, validate_config
from ..retrieve.coinglass import _output_filename


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    return out


def _to_sec(ts_ms: Any) -> Optional[int]:
    try:
        v = int(ts_ms)
    except Exception:
        return None
    return v // 1000 if v > 10_000_000_000 else v


def _extract_price_ohlc(data_dir: Path) -> List[Dict[str, Any]]:
    recs = _read_jsonl(data_dir / _output_filename("futures_ohlcv_5m"))
    out: List[Dict[str, Any]] = []
    for r in recs:
        ts = _to_sec(r.get("ts"))
        pl = r.get("payload", {}) if isinstance(r, Mapping) else {}
        if ts is None or not isinstance(pl, Mapping):
            continue
        try:
            o = float(pl.get("open"))
            h = float(pl.get("high"))
            l = float(pl.get("low"))
            c = float(pl.get("close"))
            if not all(math.isfinite(x) for x in (o, h, l, c)):
                continue
            out.append({"time": ts, "open": o, "high": h, "low": l, "close": c})
        except Exception:
            continue
    out.sort(key=lambda x: x.get("time", 0))
    return out


def _extract_single_value_series(data_dir: Path, dataset_key: str, value_keys: List[str]) -> List[Dict[str, Any]]:
    recs = _read_jsonl(data_dir / _output_filename(dataset_key))
    out: List[Dict[str, Any]] = []
    for r in recs:
        ts = _to_sec(r.get("ts"))
        pl = r.get("payload", {})
        if ts is None or not isinstance(pl, Mapping):
            continue
        val = None
        for k in value_keys:
            v = pl.get(k)
            if isinstance(v, (int, float)) and math.isfinite(float(v)):
                val = float(v)
                break
        if val is not None:
            out.append({"time": ts, "value": val})
    out.sort(key=lambda x: x.get("time", 0))
    return out


def _extract_oi(data_dir: Path) -> List[Dict[str, Any]]:
    return _extract_single_value_series(data_dir, "oi_5m_ohlc", ["close", "value", "oi"])


def _extract_liq(data_dir: Path) -> List[Dict[str, Any]]:
    return _extract_single_value_series(data_dir, "liquidation_5m", ["notional", "value", "close", "amount", "sumNotional"])


def _read_scores(scores_fp: Path) -> List[Dict[str, Any]]:
    if not scores_fp.exists():
        return []
    out: List[Dict[str, Any]] = []
    with scores_fp.open("r", encoding="utf-8") as f:
        rdr = csv.DictReader(f)
        for r in rdr:
            try:
                ts = _to_sec(int(r["ts"]))
                if ts is None:
                    continue
                s = float(r["score"]) if r.get("score") not in (None, "") else None
                t = float(r["threshold"]) if r.get("threshold") not in (None, "") else None
                if s is not None:
                    out.append({"time": ts, "score": s, "threshold": t})
            except Exception:
                continue
    return out


def _read_alerts(alerts_fp: Path) -> List[Dict[str, Any]]:
    if not alerts_fp.exists():
        return []
    out: List[Dict[str, Any]] = []
    with alerts_fp.open("r", encoding="utf-8") as f:
        rdr = csv.DictReader(f)
        for r in rdr:
            try:
                ts = _to_sec(int(r["ts"]))
                if ts is None:
                    continue
                kind = r.get("kind", "pre_alert")
                s = float(r["score"]) if r.get("score") not in (None, "") else None
                t = float(r["threshold"]) if r.get("threshold") not in (None, "") else None
                out.append({"time": ts, "kind": kind, "score": s, "threshold": t})
            except Exception:
                continue
    return out


def _inline_plotly() -> str:
    for p in (
        Path("vendor/plotly-2.32.0.min.js"),
        Path("reports/vendor/plotly-2.32.0.min.js"),
        Path("vendor/plotly.min.js"),
        Path("reports/vendor/plotly.min.js"),
    ):
        if p.exists():
            return f"<script>{p.read_text(encoding='utf-8')}</script>"
    return "<script src=\"https://cdn.plot.ly/plotly-2.32.0.min.js\"></script>"


def _render_html(symbol: str, ohlc: List[Dict[str, Any]], scores: List[Dict[str, Any]], oi: List[Dict[str, Any]], liq: List[Dict[str, Any]], alerts: List[Dict[str, Any]]) -> str:
    plotly_js = _inline_plotly()
    # Build JS arrays
    o = [x["open"] for x in ohlc]
    h = [x["high"] for x in ohlc]
    l = [x["low"] for x in ohlc]
    c = [x["close"] for x in ohlc]
    t = [x["time"] for x in ohlc]

    s_t = [x["time"] for x in scores]
    s_v = [x["score"] for x in scores]
    thr_v = [x.get("threshold") for x in scores]

    oi_t = [x["time"] for x in oi]
    oi_v = [x["value"] for x in oi]

    liq_t = [x["time"] for x in liq]
    liq_v = [x["value"] for x in liq]

    pre_t = [a["time"] for a in alerts if a.get("kind") == "pre_alert"]
    pre_v = []
    storm_t = [a["time"] for a in alerts if a.get("kind") == "storm"]
    storm_v = []
    # Map alerts to last close price when available
    t_to_close = {tt: vv for tt, vv in zip(t, c)}
    pre_v = [t_to_close.get(ts) for ts in pre_t]
    storm_v = [t_to_close.get(ts) for ts in storm_t]

    html = f"""
<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <title>CryptoStorm Report (Plotly) - {symbol}</title>
  <style>
    body {{ margin: 0; background: #111; color: #ddd; font-family: -apple-system, system-ui, Segoe UI, Roboto, sans-serif; }}
    .wrap {{ padding: 8px; display: grid; grid-gap: 8px; grid-template-rows: 50vh 25vh 25vh; }}
    .panel {{ border: 1px solid #333; border-radius: 6px; padding: 6px; background: #141414; }}
    .title {{ font-size: 12px; color: #bbb; margin: 2px 0 6px; }}
    #p1,#p2,#p3 {{ width: 100%; height: calc(100% - 22px); }}
  </style>
  {plotly_js}
</head>
<body>
  <div class=\"wrap\">
    <div class=\"panel\"><div class=\"title\">{symbol} — Price + Alerts</div><div id=\"p1\"></div></div>
    <div class=\"panel\"><div class=\"title\">IsolationForest Score</div><div id=\"p2\"></div></div>
    <div class=\"panel\"><div class=\"title\">Open Interest + Liquidations</div><div id=\"p3\"></div></div>
  </div>
  <script>
    const t = {json.dumps(t)};
    const o = {json.dumps(o)};
    const h = {json.dumps(h)};
    const l = {json.dumps(l)};
    const c = {json.dumps(c)};
    const score_t = {json.dumps(s_t)};
    const score_v = {json.dumps(s_v)};
    const thr_v = {json.dumps(thr_v)};
    const oi_t = {json.dumps(oi_t)};
    const oi_v = {json.dumps(oi_v)};
    const liq_t = {json.dumps(liq_t)};
    const liq_v = {json.dumps(liq_v)};
    const pre_t = {json.dumps(pre_t)};
    const pre_v = {json.dumps(pre_v)};
    const storm_t = {json.dumps(storm_t)};
    const storm_v = {json.dumps(storm_v)};

    const p1 = document.getElementById('p1');
    const p2 = document.getElementById('p2');
    const p3 = document.getElementById('p3');

    const candle = {{
      type: 'candlestick',
      x: t.map(x => new Date(x * 1000)),
      open: o, high: h, low: l, close: c,
      name: 'OHLC',
      increasing: {{ line: {{ color: '#26a69a' }} }},
      decreasing: {{ line: {{ color: '#ef5350' }} }},
    }};
    const pre = pre_t.length ? {{ type:'scatter', mode:'markers', x: pre_t.map(x => new Date(x * 1000)), y: pre_v, name: 'pre_alert', marker: {{ color:'#f39c12', size:7, symbol:'circle' }} }} : null;
    const storm = storm_t.length ? {{ type:'scatter', mode:'markers', x: storm_t.map(x => new Date(x * 1000)), y: storm_v, name: 'storm', marker: {{ color:'#e74c3c', size:9, symbol:'triangle-up' }} }} : null;
    const p1traces = [candle];
    if (pre) p1traces.push(pre);
    if (storm) p1traces.push(storm);
    Plotly.newPlot(p1, p1traces, {{ paper_bgcolor:'#141414', plot_bgcolor:'#141414', font:{{ color:'#ddd' }}, xaxis:{{ gridcolor:'#333' }}, yaxis:{{ gridcolor:'#333' }}, margin:{{ l:40,r:20,t:10,b:30 }} }}, {{ displayModeBar:false, responsive:true }});

    const scoreTrace = {{ type:'scatter', mode:'lines', x: score_t.map(x => new Date(x * 1000)), y: score_v, name:'score', line:{{ color:'#3498db', width:2 }} }};
    const thrTrace = {{ type:'scatter', mode:'lines', x: score_t.map(x => new Date(x * 1000)), y: thr_v, name:'threshold', line:{{ color:'#95a5a6', width:1, dash:'dot' }} }};
    Plotly.newPlot(p2, [scoreTrace, thrTrace], {{ paper_bgcolor:'#141414', plot_bgcolor:'#141414', font:{{ color:'#ddd' }}, xaxis:{{ gridcolor:'#333' }}, yaxis:{{ gridcolor:'#333' }}, margin:{{ l:40,r:20,t:10,b:30 }} }}, {{ displayModeBar:false, responsive:true }});

    const oiTrace = {{ type:'scatter', mode:'lines', x: oi_t.map(x => new Date(x * 1000)), y: oi_v, name:'oi', line:{{ color:'#2ecc71', width:2 }} }};
    const liqTrace = {{ type:'bar', x: liq_t.map(x => new Date(x * 1000)), y: liq_v, name:'liq', marker:{{ color:'#9b59b6' }} }};
    Plotly.newPlot(p3, [oiTrace, liqTrace], {{ barmode:'overlay', paper_bgcolor:'#141414', plot_bgcolor:'#141414', font:{{ color:'#ddd' }}, xaxis:{{ gridcolor:'#333' }}, yaxis:{{ gridcolor:'#333' }}, margin:{{ l:40,r:20,t:10,b:30 }} }}, {{ displayModeBar:false, responsive:true }});
  </script>
</body>
</html>
"""
    return html


def build_reports(cfg: Mapping[str, Any], eff: EffectiveConfig, *, data_root: Path, artifacts_root: Path, out_root: Path) -> None:
    out_root.mkdir(parents=True, exist_ok=True)
    for sym in eff.symbols:
        data_dir = data_root / sym
        ohlc = _extract_price_ohlc(data_dir)
        oi = _extract_oi(data_dir)
        liq = _extract_liq(data_dir)
        scores_fp = artifacts_root / "scores" / f"{sym}.csv"
        alerts_fp = artifacts_root / "alerts" / f"{sym}.csv"
        sc = _read_scores(scores_fp)
        al = _read_alerts(alerts_fp)
        html = _render_html(sym, ohlc, sc, oi, liq, al)
        (out_root / f"{sym}_plotly.html").write_text(html, encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="CryptoStorm plotly full report")
    parser.add_argument("config", type=str)
    parser.add_argument("--data", type=str, default="data")
    parser.add_argument("--artifacts", type=str)
    parser.add_argument("--out", type=str, default="reports")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    eff, warns, errs = validate_config(cfg, require_env=False)
    if errs:
        for e in errs:
            print(f"error: {e}")
        return 2
    run_id = (cfg.get("run") or {}).get("run_id") or eff.run_id
    artifacts_root = Path(args.artifacts) if args.artifacts else Path((cfg.get("run") or {}).get("artifacts_root", "./artifacts")) / str(run_id)
    build_reports(cfg, eff, data_root=Path(args.data), artifacts_root=artifacts_root, out_root=Path(args.out))
    print(f"Plotly reports written to {args.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

