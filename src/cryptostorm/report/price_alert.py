from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

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


def _extract_price_close(data_dir: Path) -> List[Dict[str, Any]]:
    # Prefer 15m OHLCV if present, else 5m
    fp15 = data_dir / _output_filename("futures_ohlcv_15m")
    fp = fp15 if fp15.exists() else (data_dir / _output_filename("futures_ohlcv_5m"))
    recs = _read_jsonl(fp)
    out: List[Dict[str, Any]] = []
    for r in recs:
        ts = _to_sec(r.get("ts"))
        pl = r.get("payload", {}) if isinstance(r, Mapping) else {}
        if ts is None or not isinstance(pl, Mapping):
            continue
        try:
            c = float(pl.get("close"))
        except Exception:
            continue
        out.append({"time": ts, "value": c})
    out.sort(key=lambda x: x.get("time", 0))
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


def _inline_plotly() -> str:
    # Try local vendor first, fallback to CDN
    for p in (
        Path("vendor/plotly-2.32.0.min.js"),
        Path("reports/vendor/plotly-2.32.0.min.js"),
        Path("vendor/plotly.min.js"),
        Path("reports/vendor/plotly.min.js"),
    ):
        if p.exists():
            return f"<script>{p.read_text(encoding='utf-8')}</script>"
    return "<script src=\"https://cdn.plot.ly/plotly-2.32.0.min.js\"></script>"


def _render_html(symbol: str, price: List[Dict[str, Any]], alerts: List[Dict[str, Any]], *, eval_info: Optional[Dict[str, Any]] = None) -> str:
    # Map alerts to price bars (use close at alert time). If not found, drop it.
    t_to_close = {p["time"]: p["value"] for p in price if isinstance(p.get("time"), int)}
    pre_points: List[Dict[str, Any]] = []
    storm_points: List[Dict[str, Any]] = []
    for a in alerts:
        tv = t_to_close.get(a.get("time"))
        if tv is None:
            continue
        kind = str(a.get("kind") or "").lower()
        pt = {"time": a["time"], "value": tv}
        if kind == "pre_alert":
            pre_points.append(pt)
        elif kind == "storm":
            storm_points.append(pt)
        else:
            pre_points.append(pt)

    plotly_js = _inline_plotly()
    # Build evaluation pills HTML outside of f-string expressions to avoid parser issues
    eval_html = ""
    if isinstance(eval_info, dict):
        pills: List[str] = []
        if eval_info.get("pct_move") is not None:
            pills.append(f"<span class=\"pill\">pct_move={eval_info.get('pct_move')}</span>")
        hz = eval_info.get("horizons")
        if isinstance(hz, list) and hz:
            try:
                hz_str = ",".join(str(x) for x in hz) + "m"
            except Exception:
                hz_str = ""
            if hz_str:
                pills.append(f"<span class=\"pill\">horizons={hz_str}</span>")
        for key in ("storm_count", "true_positive"):
            if eval_info.get(key) is not None:
                pills.append(f"<span class=\"pill\">{key.replace('_',' ')}={eval_info.get(key)}</span>")
        if eval_info.get("precision") is not None:
            try:
                pills.append(f"<span class=\"pill\">precision={float(eval_info.get('precision')):.3f}</span>")
            except Exception:
                pills.append(f"<span class=\"pill\">precision={eval_info.get('precision')}</span>")
        if eval_info.get("avg_lead_min") is not None:
            try:
                pills.append(f"<span class=\"pill\">avg_lead_min={float(eval_info.get('avg_lead_min')):.1f}</span>")
            except Exception:
                pills.append(f"<span class=\"pill\">avg_lead_min={eval_info.get('avg_lead_min')}</span>")
        if pills:
            eval_html = "<div class=\"metrics\">" + "".join(pills) + "</div>"

    html = f"""
<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <title>CryptoStorm Price+Alerts - {symbol}</title>
  <style>
    body {{ margin: 0; background: #111; color: #ddd; font-family: -apple-system, system-ui, Segoe UI, Roboto, sans-serif; }}
    .wrap {{ padding: 8px; }}
    .title {{ font-size: 14px; color: #bbb; margin-bottom: 6px; }}
    #chart {{ width: 100%; height: 80vh; }}
    .metrics {{ margin: 6px 0 10px; font-size: 12px; color: #ccc; }}
    .metrics .pill {{ display: inline-block; padding: 2px 6px; margin-right: 6px; border: 1px solid #2a2a2a; border-radius: 10px; background:#151515; }}
  </style>
  {plotly_js}
</head>
<body>
  <div class=\"wrap\"> 
    <div class=\"title\">{symbol} — Price + Alerts</div>
    {eval_html}
    <div id=\"chart\"></div>
  </div>
  <script>
    const priceData = {json.dumps(price)};
    const alertData = {json.dumps(pre_points + storm_points)};
    const preAlertData = {json.dumps(pre_points)};
    const stormAlertData = {json.dumps(storm_points)};
    const x = priceData.map(p => new Date(p.time * 1000));
    const y = priceData.map(p => p.value);
    const priceTrace = {{ type:'scatter', mode:'lines', x:x, y:y, line:{{ color:'#9bd', width:2 }}, name:'price' }};

    const traces = [priceTrace];
    if (preAlertData.length) {{
      const ax = preAlertData.map(a => new Date(a.time * 1000));
      const ay = preAlertData.map(a => a.value);
      traces.push({{ type:'scatter', mode:'markers', x:ax, y:ay, name:'pre_alert', marker:{{ color:'#f39c12', size:7, symbol:'circle' }} }});
    }}
    if (stormAlertData.length) {{
      const ax = stormAlertData.map(a => new Date(a.time * 1000));
      const ay = stormAlertData.map(a => a.value);
      traces.push({{ type:'scatter', mode:'markers', x:ax, y:ay, name:'storm', marker:{{ color:'#e74c3c', size:9, symbol:'triangle-up' }} }});
    }}
    const layout = {{
      paper_bgcolor:'#111', plot_bgcolor:'#111',
      font: {{ color:'#ddd' }},
      xaxis: {{ gridcolor:'#222', rangeslider: {{ visible: true }} }},
      yaxis: {{ gridcolor:'#222' }},
      margin: {{ l: 50, r: 20, t: 10, b: 40 }},
    }};
    Plotly.newPlot('chart', traces, layout, {{ displayModeBar: false, responsive: true }});
  </script>
</body>
</html>
"""
    return html


def build_reports(cfg: Mapping[str, Any], eff: EffectiveConfig, *, data_root: Path, artifacts_root: Path, out_root: Path) -> None:
    out_root.mkdir(parents=True, exist_ok=True)
    # Load per-run evaluation metrics if present
    eval_metrics: Dict[str, Any] = {}
    try:
        mfp = artifacts_root / "metrics" / "metrics.json"
        if mfp.exists():
            eval_metrics = json.loads(mfp.read_text(encoding="utf-8") or "{}")
    except Exception:
        eval_metrics = {}
    labels_cfg = (cfg.get("labels") or {}) if isinstance(cfg.get("labels"), Mapping) else {}
    pct_move = labels_cfg.get("pct_move")
    horizons = labels_cfg.get("horizons_min") if isinstance(labels_cfg.get("horizons_min"), list) else None
    for sym in eff.symbols:
        data_dir = data_root / sym
        price = _extract_price_close(data_dir)
        alerts_fp = artifacts_root / "alerts" / f"{sym}.csv"
        al = _read_alerts(alerts_fp)
        # Build per-symbol eval info for the panel
        em = None
        try:
            sym_stats = (eval_metrics.get("symbols") or {}).get(sym) if isinstance(eval_metrics.get("symbols"), Mapping) else None
            if isinstance(sym_stats, Mapping):
                em = {
                    "pct_move": pct_move,
                    "horizons": horizons,
                    "storm_count": sym_stats.get("storm_count"),
                    "true_positive": sym_stats.get("true_positive"),
                    "precision": sym_stats.get("precision"),
                    "avg_lead_min": sym_stats.get("avg_lead_min"),
                }
        except Exception:
            em = None
        html = _render_html(sym, price, al, eval_info=em)
        (out_root / f"{sym}_price_alert.html").write_text(html, encoding="utf-8")
    # Write a simple index to navigate reports
    try:
        items = []
        import time as _t
        now = _t.strftime("%Y-%m-%d %H:%M:%SZ", _t.gmtime())
        for s in eff.symbols:
            fp = out_root / f"{s}_price_alert.html"
            if fp.exists():
                items.append((s, fp.stat().st_mtime))
        items.sort(key=lambda x: x[0])
        lines = [
            "<!doctype html>",
            "<html><head><meta charset=\"utf-8\" />",
            "<title>CryptoStorm Price+Alerts Reports</title>",
            "<style>body{font-family:-apple-system,system-ui,Segoe UI,Roboto,sans-serif;background:#111;color:#ddd;margin:0}.wrap{padding:10px} a{color:#9bd;text-decoration:none} ul{list-style:none;padding:0} li{margin:4px 0} .ts{color:#aaa;font-size:12px}</style>",
            "</head><body><div class=wrap>",
            f"<h2>CryptoStorm Reports (Price+Alerts) <span class=ts>(generated {now})</span></h2>",
            "<ul>",
        ]
        for s, _m in items:
            lines.append(f"<li><a href=\"{s}_price_alert.html\">{s}</a></li>")
        lines.extend(["</ul>", "</div></body></html>"])
        (out_root / "index.html").write_text("\n".join(lines), encoding="utf-8")
    except Exception:
        pass


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="CryptoStorm price+alerts report (Plotly)")
    parser.add_argument("config", type=str)
    parser.add_argument("--data", type=str, default="data")
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
    build_reports(cfg, eff, data_root=Path(args.data), artifacts_root=artifacts_root, out_root=Path(args.out))
    print(f"Price+Alerts reports written to {args.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
