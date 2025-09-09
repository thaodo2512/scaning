from __future__ import annotations

import argparse
import csv
import json
import os
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
    if isinstance(ts_ms, (int, float)):
        v = int(ts_ms)
        # assume ms
        if v > 10_000_000_000:  # > ~2001 in seconds
            return v // 1000
        return v
    try:
        return int(ts_ms)
    except Exception:
        return None


def _extract_price(data_dir: Path) -> List[Dict[str, Any]]:
    recs = _read_jsonl(data_dir / _output_filename("futures_ohlcv_5m"))
    out: List[Dict[str, Any]] = []
    for r in recs:
        ts = _to_sec(r.get("ts"))
        pl = r.get("payload", {}) if isinstance(r, Mapping) else {}
        if ts is None or not isinstance(pl, Mapping):
            continue
        try:
            out.append(
                {
                    "time": ts,
                    "open": float(pl.get("open")),
                    "high": float(pl.get("high")),
                    "low": float(pl.get("low")),
                    "close": float(pl.get("close")),
                }
            )
        except Exception:
            continue
    return out


def _extract_oi(data_dir: Path) -> List[Dict[str, Any]]:
    recs = _read_jsonl(data_dir / _output_filename("oi_5m_ohlc"))
    out: List[Dict[str, Any]] = []
    for r in recs:
        ts = _to_sec(r.get("ts"))
        pl = r.get("payload", {})
        if ts is None or not isinstance(pl, Mapping):
            continue
        val = None
        for k in ("close", "value", "oi"):
            if isinstance(pl.get(k), (int, float)):
                val = float(pl.get(k))
                break
        if val is not None:
            out.append({"time": ts, "value": val})
    return out


def _extract_liq(data_dir: Path) -> List[Dict[str, Any]]:
    recs = _read_jsonl(data_dir / _output_filename("liquidation_5m"))
    out: List[Dict[str, Any]] = []
    for r in recs:
        ts = _to_sec(r.get("ts"))
        pl = r.get("payload", {})
        if ts is None or not isinstance(pl, Mapping):
            continue
        val = None
        for k in ("notional", "value", "close", "amount", "sumNotional"):
            if isinstance(pl.get(k), (int, float)):
                val = float(pl.get(k))
                break
        if val is not None:
            out.append({"time": ts, "value": val})
    return out


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
                out.append({"time": ts, "kind": kind})
            except Exception:
                continue
    return out


def _inline_lightweight_charts() -> str:
    # Try to inline a local copy if present; else use CDN
    candidates = [
        Path("vendor/lightweight-charts.standalone.production.js"),
        Path("reports/vendor/lightweight-charts.standalone.production.js"),
    ]
    for p in candidates:
        if p.exists():
            return f"<script>{p.read_text(encoding='utf-8')}</script>"
    # CDN fallback (not offline). Users can drop a local file at vendor/ to inline
    return "<script src=\"https://unpkg.com/lightweight-charts@4.2.1/dist/lightweight-charts.standalone.production.js\"></script>"


def _render_html(symbol: str, price: List[Dict[str, Any]], scores: List[Dict[str, Any]], oi: List[Dict[str, Any]], liq: List[Dict[str, Any]], overlays: Dict[str, List[Dict[str, Any]]], alerts: List[Dict[str, Any]]) -> str:
    # Prepare arrays for JS
    score_line = [{"time": s["time"], "value": s["score"]} for s in scores]
    thr_line = [{"time": s["time"], "value": s["threshold"]} for s in scores if s.get("threshold") is not None]
    pre_markers = [{"time": a["time"], "position": "belowBar", "shape": "arrowUp", "color": "#f39c12", "text": "pre"} for a in alerts if a.get("kind") == "pre_alert"]
    storm_markers = [{"time": a["time"], "position": "aboveBar", "shape": "arrowUp", "color": "#e74c3c", "text": "storm"} for a in alerts if a.get("kind") == "storm"]
    markers = pre_markers + storm_markers

    lwc = _inline_lightweight_charts()
    html = f"""
<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <title>CryptoStorm Report - {symbol}</title>
  <style>
    body {{ margin: 0; font-family: -apple-system, system-ui, Segoe UI, Roboto, sans-serif; background: #111; color: #ddd; }}
    .wrap {{ display: grid; grid-template-rows: 48vh 24vh 24vh; grid-gap: 6px; padding: 6px; }}
    .panel {{ position: relative; border: 1px solid #333; border-radius: 6px; }}
    .title {{ position: absolute; top: 6px; left: 10px; font-size: 12px; color: #bbb; z-index: 10; }}
    #top, #mid, #bot {{ height: 100%; }}
  </style>
  {lwc}
</head>
<body>
  <div class=\"wrap\">
    <div class=\"panel\"><div class=\"title\">{symbol} — Price + Alerts</div><div id=\"top\"></div></div>
    <div class=\"panel\"><div class=\"title\">IsolationForest Score</div><div id=\"mid\"></div></div>
    <div class=\"panel\"><div class=\"title\">Open Interest + Liquidations</div><div id=\"bot\"></div></div>
  </div>
  <script>
    const price = {json.dumps(price)};
    const score = {json.dumps(score_line)};
    const thr = {json.dumps(thr_line)};
    const oi = {json.dumps(oi)};
    const liq = {json.dumps(liq)};
    const markers = {json.dumps(markers)};
    const overlays = {json.dumps(overlays)};

    function makeChart(container, opts) {{
      const chart = LightweightCharts.createChart(container, Object.assign({{ layout: {{ background: {{ type:'Solid', color:'#111' }}, textColor:'#CCC' }}, rightPriceScale: {{ borderVisible:false }}, timeScale: {{ borderVisible:false }}, grid: {{ vertLines: {{ color:'#222' }}, horzLines: {{ color:'#222' }} }} }}, opts||{{}}));
      const resize = () => chart.applyOptions({{ width: container.clientWidth, height: container.clientHeight }});
      new ResizeObserver(resize).observe(container);
      resize();
      return chart;
    }}

    const topEl = document.getElementById('top');
    const midEl = document.getElementById('mid');
    const botEl = document.getElementById('bot');

    const top = makeChart(topEl);
    const mid = makeChart(midEl);
    const bot = makeChart(botEl);

    const candle = top.addCandlestickSeries({{ upColor:'#26a69a', downColor:'#ef5350', borderVisible:false, wickUpColor:'#26a69a', wickDownColor:'#ef5350' }});
    candle.setData(price);
    if (markers.length) candle.setMarkers(markers);

    // Optional overlays
    if (overlays.funding) {{
      const fline = top.addLineSeries({{ color:'#f1c40f', lineWidth:1 }});
      fline.setData(overlays.funding);
    }}

    const scoreLine = mid.addLineSeries({{ color:'#3498db', lineWidth:2 }});
    scoreLine.setData(score);
    if (thr.length) {{
      const thrLine = mid.addLineSeries({{ color:'#95a5a6', lineWidth:1, lineStyle: LightweightCharts.LineStyle.Dotted }});
      thrLine.setData(thr);
    }}

    const oiArea = bot.addAreaSeries({{ lineColor:'#2ecc71', topColor:'rgba(46, 204, 113, 0.4)', bottomColor:'rgba(46, 204, 113, 0.0)' }});
    oiArea.setData(oi);
    const liqHist = bot.addHistogramSeries({{ color:'#9b59b6' }});
    liqHist.setData(liq);

    // Sync visible time range across charts
    function sync(from, toA, toB) {{
      from.timeScale().subscribeVisibleTimeRangeChange((range) => {{
        if (range) {{ toA.timeScale().setVisibleRange(range); toB.timeScale().setVisibleRange(range); }}
      }});
    }}
    sync(top, mid, bot);
    sync(mid, top, bot);
    sync(bot, top, mid);
  </script>
</body>
</html>
"""
    return html


def build_reports(cfg: Mapping[str, Any], eff: EffectiveConfig, *, data_root: Path, features_root: Path, artifacts_root: Path, out_root: Path) -> None:
    out_root.mkdir(parents=True, exist_ok=True)
    for sym in eff.symbols:
        data_dir = data_root / sym
        price = _extract_price(data_dir)
        oi = _extract_oi(data_dir)
        liq = _extract_liq(data_dir)
        scores_fp = artifacts_root / "scores" / f"{sym}.csv"
        alerts_fp = artifacts_root / "alerts" / f"{sym}.csv"
        sc = _read_scores(scores_fp)
        al = _read_alerts(alerts_fp)
        overlays: Dict[str, List[Dict[str, Any]]] = {}
        # Optional funding overlay: use features if present
        feat_fp = features_root / sym / "features_5m.csv"
        if feat_fp.exists():
            try:
                arr: List[Dict[str, Any]] = []
                with feat_fp.open("r", encoding="utf-8") as f:
                    rdr = csv.DictReader(f)
                    for r in rdr:
                        ts = _to_sec(int(r["ts"])) if r.get("ts") else None
                        fv = float(r["funding_now"]) if r.get("funding_now") not in (None, "") else None
                        if ts is not None and fv is not None:
                            arr.append({"time": ts, "value": fv})
                if arr:
                    overlays["funding"] = arr
            except Exception:
                pass
        html = _render_html(sym, price, sc, oi, liq, overlays, al)
        out_fp = out_root / f"{sym}.html"
        out_fp.write_text(html, encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="CryptoStorm report generator (Lightweight Charts)")
    parser.add_argument("config", type=str)
    parser.add_argument("--data", type=str, default="data")
    parser.add_argument("--features", type=str, default="features")
    parser.add_argument("--artifacts", type=str, help="Path to artifacts root; defaults to run.artifacts_root/run_id from config")
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
    build_reports(cfg, eff, data_root=Path(args.data), features_root=Path(args.features), artifacts_root=artifacts_root, out_root=Path(args.out))
    print(f"Reports written to {args.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

