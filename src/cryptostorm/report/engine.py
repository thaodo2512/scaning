from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
import math
try:  # optional dependency for templating
    from jinja2 import Environment, FileSystemLoader  # type: ignore
except Exception:  # pragma: no cover
    Environment = None  # type: ignore
    FileSystemLoader = None  # type: ignore

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
    def _finite(v: Any) -> bool:
        return isinstance(v, (int, float)) and not math.isnan(v) and math.isfinite(v)

    score_line = [{"time": s["time"], "value": float(s["score"])} for s in scores if _finite(s.get("score"))]
    thr_line = [{"time": s["time"], "value": float(s["threshold"])} for s in scores if _finite(s.get("threshold"))]
    score_line.sort(key=lambda x: x.get("time", 0))
    thr_line.sort(key=lambda x: x.get("time", 0))
    pre_markers = [{"time": a["time"], "position": "belowBar", "shape": "arrowUp", "color": "#f39c12", "text": "pre"} for a in alerts if a.get("kind") == "pre_alert"]
    storm_markers = [{"time": a["time"], "position": "aboveBar", "shape": "arrowUp", "color": "#e74c3c", "text": "storm"} for a in alerts if a.get("kind") == "storm"]
    markers = pre_markers + storm_markers

    lwc = _inline_lightweight_charts()

    # Prefer Jinja2 template if available
    if Environment is not None:
        try:  # pragma: no cover
            here = Path(__file__).parent
            env = Environment(loader=FileSystemLoader([str(here / "templates"), ".", str(Path.cwd())]))
            tmpl = env.get_template("report_template.html")
            chart_config = {
                "default": {
                    "layout": {"background": {"type": "Solid", "color": "#111"}, "textColor": "#CCC"},
                    "rightPriceScale": {"borderVisible": False},
                    "timeScale": {"borderVisible": False},
                    "grid": {"vertLines": {"color": "#222"}, "horzLines": {"color": "#222"}},
                },
                "candleSeries": {"upColor": "#26a69a", "downColor": "#ef5350", "borderVisible": False, "wickUpColor": "#26a69a", "wickDownColor": "#ef5350"},
                "scoreLine": {"color": "#3498db", "lineWidth": 2},
                "thresholdLine": {"color": "#95a5a6", "lineWidth": 1, "lineStyle": 1},
                "oiArea": {"lineColor": "#2ecc71", "topColor": "rgba(46, 204, 113, 0.4)", "bottomColor": "rgba(46, 204, 113, 0.0)"},
                "liqHistogram": {"color": "#9b59b6"},
            }
            return tmpl.render(
                symbol=symbol,
                lightweight_charts_script=lwc,
                price_data=price,
                score_data=score_line,
                threshold_data=thr_line,
                oi_data=oi,
                liq_data=liq,
                markers=markers,
                overlays=overlays,
                chart_config=chart_config,
                # For compatibility with existing tests (variable names)
                price_raw=price,
                score_raw=score_line,
                thr_raw=thr_line,
                oi_raw=oi,
                liq_raw=liq,
            )
        except Exception:
            pass

    html = f"""
<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <link rel=\"icon\" href=\"data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg'/>\"> 
  <title>CryptoStorm Report - {symbol}</title>
  <style>
    body {{ margin: 0; font-family: -apple-system, system-ui, Segoe UI, Roboto, sans-serif; background: #111; color: #ddd; }}
    .wrap {{ display: grid; grid-template-rows: 60vh 40vh; grid-gap: 6px; padding: 6px; }}
    .panel {{ position: relative; border: 1px solid #333; border-radius: 6px; }}
    .title {{ position: absolute; top: 6px; left: 10px; font-size: 12px; color: #bbb; z-index: 10; }}
    #top, #mid {{ height: 100%; }}
    .empty-msg {{ position:absolute; inset:0; display:flex; align-items:center; justify-content:center; color:#777; font-size:12px; }}
  </style>
  {lwc}
</head>
<body>
  <div class=\"wrap\">
    <div class=\"panel\"><div class=\"title\">{symbol} — Price + Alerts</div><div id=\"top\"></div></div>
    <div class=\"panel\"><div class=\"title\">IsolationForest Score</div><div id=\"mid\"></div></div>
  </div>
  <script>
    const priceRaw = {json.dumps(price)};
    const scoreRaw = {json.dumps(score_line)};
    const thrRaw = {json.dumps(thr_line)};
    const oiRaw = {json.dumps(oi)};
    const liqRaw = {json.dumps(liq)};
    const markers = {json.dumps(markers)};
    const overlays = {json.dumps(overlays)};

    // Sanitize data to avoid null/NaN issues
    const isFiniteNum = (v) => typeof v === 'number' && Number.isFinite(v);
    const candles = priceRaw.filter(p => p && isFiniteNum(p.time) && isFiniteNum(p.open) && isFiniteNum(p.high) && isFiniteNum(p.low) && isFiniteNum(p.close));
    const score = scoreRaw.filter(p => p && isFiniteNum(p.time) && isFiniteNum(p.value));
    const thr = thrRaw.filter(p => p && isFiniteNum(p.time) && isFiniteNum(p.value));
    const oi = oiRaw.filter(p => p && isFiniteNum(p.time) && isFiniteNum(p.value));
    const liq = liqRaw.filter(p => p && isFiniteNum(p.time) && isFiniteNum(p.value));

    function makeChart(container, opts) {{
      const chart = LightweightCharts.createChart(container, Object.assign({{ layout: {{ background: {{ type:'Solid', color:'#111' }}, textColor:'#CCC' }}, rightPriceScale: {{ borderVisible:false }}, timeScale: {{ borderVisible:false }}, grid: {{ vertLines: {{ color:'#222' }}, horzLines: {{ color:'#222' }} }} }}, opts||{{}}));
      const resize = () => chart.applyOptions({{ width: container.clientWidth, height: container.clientHeight }});
      new ResizeObserver(resize).observe(container);
      resize();
      return chart;
    }}

    const topEl = document.getElementById('top');
    const midEl = document.getElementById('mid');

    // Avoid using global name 'top' which conflicts with window.top in browsers
    const chartTop = makeChart(topEl, {{ leftPriceScale: {{ borderVisible:false, visible:true }} }});
    const chartMid = makeChart(midEl);

    const priceLineData = candles.map(c => ({{ time: c.time, value: c.close }}));
    const priceLine = chartTop.addLineSeries({{ color:'#888', lineWidth:2 }});
    if (priceLineData.length) {{
      priceLine.setData(priceLineData);
      if (markers.length) priceLine.setMarkers(markers);
    }} else {{
      const m = document.createElement('div');
      m.className = 'empty-msg';
      m.textContent = 'No price data available';
      topEl.appendChild(m);
    }}

    // Optional overlays
    if (overlays.funding) {{
      const fline = chartTop.addLineSeries({{ color:'#f1c40f', lineWidth:1, priceScaleId: 'left' }});
      fline.setData(overlays.funding);
    }}

    const scoreLine = chartMid.addLineSeries({{ color:'#3498db', lineWidth:2 }});
    if (score.length) {{
      scoreLine.setData(score);
    }} else {{
      const m = document.createElement('div');
      m.className = 'empty-msg';
      m.textContent = 'No model scores — run backtest for this run_id';
      midEl.appendChild(m);
    }}
    if (thr.length) {{
      const thrLine = chartMid.addLineSeries({{ color:'#95a5a6', lineWidth:1, lineStyle: LightweightCharts.LineStyle.Dotted }});
      thrLine.setData(thr);
    }}

    // OI/Liquidations panel removed

    // Sync visible time range across charts
    function sync(from, toA) {{
      from.timeScale().subscribeVisibleTimeRangeChange((range) => {{
        if (range && range.from != null && range.to != null) {{
          const vr = {{ from: range.from, to: range.to }};
          try {{ toA.timeScale().setVisibleRange(vr); }} catch (e) {{ /* ignore */ }}
        }}
      }});
    }}
    sync(chartTop, chartMid);
    sync(chartMid, chartTop);
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
        oi = []
        liq = []
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
                        fv = None
                        try:
                            if r.get("funding_now") not in (None, ""):
                                fv = float(r["funding_now"])
                        except Exception:
                            fv = None
                        if ts is not None and fv is not None and not math.isnan(fv) and math.isfinite(fv):
                            arr.append({"time": ts, "value": fv})
                if arr:
                    overlays["funding"] = arr
            except Exception:
                pass
        html = _render_html(sym, price, sc, oi, liq, overlays, al)
        out_fp = out_root / f"{sym}.html"
        out_fp.write_text(html, encoding="utf-8")
    # Write a simple index to navigate reports
    try:
        items = []
        import time as _t
        now = _t.strftime("%Y-%m-%d %H:%M:%SZ", _t.gmtime())
        for s in eff.symbols:
            fp = out_root / f"{s}.html"
            if fp.exists():
                items.append((s, fp.stat().st_mtime))
        items.sort(key=lambda x: x[0])
        lines = [
            "<!doctype html>",
            "<html><head><meta charset=\"utf-8\" />",
            "<title>CryptoStorm Reports</title>",
            "<style>body{font-family:-apple-system,system-ui,Segoe UI,Roboto,sans-serif;background:#111;color:#ddd;margin:0}.wrap{padding:10px} a{color:#9bd;text-decoration:none} ul{list-style:none;padding:0} li{margin:4px 0} .ts{color:#aaa;font-size:12px}</style>",
            "</head><body><div class=wrap>",
            f"<h2>CryptoStorm Reports (Lightweight) <span class=ts>(generated {now})</span></h2>",
            "<ul>",
        ]
        for s, _m in items:
            lines.append(f"<li><a href=\"{s}.html\">{s}</a></li>")
        lines.extend(["</ul>", "</div></body></html>"])
        (out_root / "index.html").write_text("\n".join(lines), encoding="utf-8")
    except Exception:
        pass


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
