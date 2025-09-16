from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from ..config import load_config, validate_config, EffectiveConfig


def _read_last_lines(path: Path, n: int) -> List[str]:
    if not path.exists() or n <= 0:
        return []
    try:
        # naive but fine for small files
        lines = path.read_text(encoding="utf-8").splitlines()
        return lines[-n:]
    except Exception:
        return []


def _read_scores(scores_fp: Path, n: int) -> List[Dict[str, Any]]:
    if not scores_fp.exists():
        return []
    out: List[Dict[str, Any]] = []
    try:
        with scores_fp.open("r", encoding="utf-8") as f:
            rdr = list(csv.DictReader(f))
        rows = rdr[-n:] if n > 0 else rdr
        for r in rows:
            try:
                out.append({
                    "ts": int(r.get("ts") or 0),
                    "symbol": r.get("symbol"),
                    "score": float(r.get("score")) if r.get("score") not in (None, "") else None,
                    "threshold": float(r.get("threshold")) if r.get("threshold") not in (None, "") else None,
                })
            except Exception:
                continue
    except Exception:
        return []
    return out


def _read_alerts(alerts_fp: Path, n: int) -> List[Dict[str, Any]]:
    if not alerts_fp.exists():
        return []
    out: List[Dict[str, Any]] = []
    try:
        with alerts_fp.open("r", encoding="utf-8") as f:
            rdr = list(csv.DictReader(f))
        rows = rdr[-n:] if n > 0 else rdr
        for r in rows:
            try:
                out.append({
                    "ts": int(r.get("ts") or 0),
                    "symbol": r.get("symbol"),
                    "kind": r.get("kind") or "pre_alert",
                    "score": float(r.get("score")) if r.get("score") not in (None, "") else None,
                    "threshold": float(r.get("threshold")) if r.get("threshold") not in (None, "") else None,
                })
            except Exception:
                continue
    except Exception:
        return []
    return out


def _build_app(cfg: Mapping[str, Any], eff: EffectiveConfig, *, data_root: Path, features_root: Path, artifacts_root: Path, reports_root: Path, api_token: Optional[str]):
    try:
        from fastapi import FastAPI, HTTPException, Depends
        from fastapi.responses import JSONResponse, PlainTextResponse, HTMLResponse
        from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("FastAPI is required: pip install fastapi uvicorn") from exc

    app = FastAPI(title="CryptoStorm API", version="0.1")

    def _auth_dep() -> None:
        if not api_token:
            return None
        # Lazy import to avoid hard dep if unused
        from fastapi import Security, HTTPException
        from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
        security = HTTPBearer(auto_error=False)
        creds: Optional[HTTPAuthorizationCredentials] = Security(security)
        if creds is None or creds.scheme.lower() != "bearer" or creds.credentials != api_token:
            raise HTTPException(status_code=401, detail="Unauthorized")
        return None

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/")
    def root():
        # Serve reports index.html by default if available
        fp = reports_root / "index.html"
        if fp.exists():
            return HTMLResponse(fp.read_text(encoding="utf-8"))
        # Fallback: simple landing page with helpful links
        html = (
            "<!doctype html><html><head><meta charset='utf-8'><title>CryptoStorm API</title>"
            "<style>body{font-family:-apple-system,system-ui,Segoe UI,Roboto,sans-serif;background:#111;color:#ddd;margin:0}"
            ".wrap{padding:14px} a{color:#9bd;text-decoration:none}</style></head><body><div class='wrap'>"
            "<h2>CryptoStorm API</h2>"
            "<p>No reports index found at <code>" + str(fp) + "</code>.</p>"
            "<ul>"
            "<li><a href='/docs'>/docs</a> (Swagger UI)</li>"
            "<li><a href='/healthz'>/healthz</a></li>"
            "</ul>"
            "</div></body></html>"
        )
        return HTMLResponse(html)

    @app.get("/symbols")
    def symbols(_: None = Depends(_auth_dep)):
        return {"symbols": eff.symbols}

    @app.get("/scores/{symbol}")
    def scores(symbol: str, n: int = 200, _: None = Depends(_auth_dep)):
        fp = artifacts_root / "scores" / f"{symbol}.csv"
        return {"symbol": symbol, "scores": _read_scores(fp, n)}

    @app.get("/alerts/{symbol}")
    def alerts(symbol: str, n: int = 200, _: None = Depends(_auth_dep)):
        fp = artifacts_root / "alerts" / f"{symbol}.csv"
        return {"symbol": symbol, "alerts": _read_alerts(fp, n)}

    @app.get("/latest/score/{symbol}")
    def latest_score(symbol: str, _: None = Depends(_auth_dep)):
        arr = _read_scores(artifacts_root / "scores" / f"{symbol}.csv", 1)
        return (arr[0] if arr else JSONResponse(status_code=404, content={"error": "not found"}))

    @app.get("/report/{symbol}")
    def report(symbol: str):
        fp = reports_root / f"{symbol}.html"
        if not fp.exists():
            raise HTTPException(status_code=404, detail="report not found")
        return HTMLResponse(fp.read_text(encoding="utf-8"))

    @app.get("/price/{symbol}")
    def report_price(symbol: str):
        fp = reports_root / f"{symbol}_price_alert.html"
        if not fp.exists():
            raise HTTPException(status_code=404, detail="price report not found")
        return HTMLResponse(fp.read_text(encoding="utf-8"))

    @app.get("/plotly/{symbol}")
    def report_plotly(symbol: str):
        fp = reports_root / f"{symbol}_plotly.html"
        if not fp.exists():
            raise HTTPException(status_code=404, detail="plotly report not found")
        return HTMLResponse(fp.read_text(encoding="utf-8"))

    @app.get("/metrics")
    def metrics(prom: bool = False):
        slo_fp = artifacts_root / "metrics" / "realtime.jsonl"
        last = None
        lines = _read_last_lines(slo_fp, 1)
        if lines:
            try:
                last = json.loads(lines[0])
            except Exception:
                last = None
        if prom:
            # minimal Prometheus exposition
            lines_out = []
            if isinstance(last, dict):
                def _m(name: str, value: Any):
                    try:
                        lines_out.append(f"cryptostorm_{name} {float(value)}")
                    except Exception:
                        pass
                _m("scheduler_lag_seconds", last.get("scheduler_lag_s"))
                _m("retrieve_ms", last.get("retrieve_ms"))
                _m("feature_ms", last.get("feature_ms"))
                _m("score_ms", last.get("score_ms"))
            return PlainTextResponse("\n".join(lines_out) + "\n")
        return {"realtime_last": last}

    return app


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="CryptoStorm API (FastAPI)")
    parser.add_argument("config", type=str)
    parser.add_argument("--data", type=str, default="data")
    parser.add_argument("--features", type=str, default="features")
    parser.add_argument("--artifacts", type=str)
    parser.add_argument("--reports", type=str, default="reports")
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="Auto-reload on code changes (dev)")
    parser.add_argument("--token", type=str, default=None, help="Bearer token for auth (optional)")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    eff, warns, errs = validate_config(cfg, require_env=False)
    if errs:
        for e in errs:
            print(f"error: {e}")
        return 2
    run_id = (cfg.get("run") or {}).get("run_id") or eff.run_id
    artifacts_root = Path(args.artifacts) if args.artifacts else Path((cfg.get("run") or {}).get("artifacts_root", "./artifacts")) / str(run_id)
    app = _build_app(cfg, eff, data_root=Path(args.data), features_root=Path(args.features), artifacts_root=artifacts_root, reports_root=Path(args.reports), api_token=args.token)

    try:
        import uvicorn  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("uvicorn is required to run the server: pip install uvicorn") from exc
    uvicorn.run(app, host=args.host, port=int(args.port), reload=bool(args.reload))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
