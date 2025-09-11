from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from ..config import load_config, validate_config, EffectiveConfig


def _export_raw_jsonl_to_parquet(src_fp: Path, dst_fp: Path) -> int:
    try:
        import pyarrow as pa  # type: ignore
        import pyarrow.parquet as pq  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("pyarrow is required for Parquet export: pip install pyarrow") from exc

    if not src_fp.exists():
        return 0
    rows: List[Dict[str, Any]] = []
    for line in src_fp.read_text(encoding="utf-8").splitlines():
        try:
            obj = json.loads(line)
        except Exception:
            continue
        rows.append({
            "ts": int(obj.get("ts") or 0),
            "symbol": str(obj.get("symbol") or ""),
            "interval": obj.get("interval"),
            "mode": obj.get("mode"),
            "source": obj.get("source"),
            "payload_json": json.dumps(obj.get("payload"), separators=(",", ":")),
        })
    if not rows:
        return 0
    table = pa.Table.from_pylist(rows)
    dst_fp.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, dst_fp)
    return len(rows)


def _export_features_csv_to_parquet(src_fp: Path, dst_fp: Path) -> int:
    try:
        import pyarrow as pa  # type: ignore
        import pyarrow.parquet as pq  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("pyarrow is required for Parquet export: pip install pyarrow") from exc

    if not src_fp.exists():
        return 0
    with src_fp.open("r", encoding="utf-8") as f:
        rdr = csv.DictReader(f)
        rows = []
        for r in rdr:
            rows.append({k: (None if v == "" else (float(v) if k != "symbol" and k != "ts" else int(v) if k == "ts" else v)) for k, v in r.items()})
    if not rows:
        return 0
    table = pa.Table.from_pylist(rows)
    dst_fp.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, dst_fp)
    return len(rows)


def export_parquet(cfg: Mapping[str, Any], eff: EffectiveConfig, *, data_root: Path, features_root: Path, out_root: Path) -> Dict[str, Any]:
    from ..retrieve.coinglass import _output_filename

    summary: Dict[str, Any] = {"symbols": {}}
    for sym in eff.symbols:
        out_sym = out_root / sym
        counts: Dict[str, int] = {}
        # Raw datasets (present ones)
        for ds_key in eff.datasets.keys():
            src = data_root / sym / _output_filename(ds_key)
            if src.exists():
                dst = out_sym / (src.name.replace(".jsonl", ".parquet"))
                counts[src.name] = _export_raw_jsonl_to_parquet(src, dst)
        # Features
        feat_csv = features_root / sym / "features_5m.csv"
        if feat_csv.exists():
            dst = out_sym / "features_5m.parquet"
            counts["features_5m.csv"] = _export_features_csv_to_parquet(feat_csv, dst)
        summary["symbols"][sym] = counts
    return summary


def _features_columns() -> List[str]:
    return [
        "ts","symbol","open","high","low","close","volume","funding_now","funding_pctile_30d","funding_pred_twap_60m","oi_now","oi_pctile_30d","spread_bps","depth_ratio","basis_now","basis_TWAP_60m","basis_TWAP_120m","delta_taker_5m","cvd_perp_5m","cvd_perp_15m","perp_share_60m","liq_notional_5m","liq_count_5m","liq_notional_60m","rv_15m","exch_reserve_flag","etf_flow_flag","data_ok",
    ]


def emit_clickhouse_sql(cfg: Mapping[str, Any], eff: EffectiveConfig, *, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    fp = out_dir / "clickhouse_ddl.sql"
    parts: List[str] = []
    # One raw table per dataset (payload_json as String)
    for ds_key in eff.datasets.keys():
        tname = f"cg_{ds_key}"
        parts.append(
            f"""
DROP TABLE IF EXISTS {tname};
CREATE TABLE {tname} (
  ts DateTime64(3) CODEC(DoubleDelta, ZSTD),
  symbol LowCardinality(String),
  interval LowCardinality(String),
  mode LowCardinality(String),
  source String,
  payload_json String
) ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (symbol, ts)
SETTINGS index_granularity = 8192;
""".strip()
        )
    # Features table
    cols = _features_columns()
    col_defs = [
        "ts DateTime64(3)",
        "symbol LowCardinality(String)",
    ] + [f"{c} Float64" for c in cols if c not in ("ts", "symbol")]
    parts.append(
        f"""
DROP TABLE IF EXISTS features_5m;
CREATE TABLE features_5m (
  {',\n  '.join(col_defs)}
) ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (symbol, ts)
SETTINGS index_granularity = 8192;
""".strip()
    )
    fp.write_text("\n\n".join(parts) + "\n", encoding="utf-8")
    return fp


def emit_timescale_sql(cfg: Mapping[str, Any], eff: EffectiveConfig, *, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    fp = out_dir / "timescale_ddl.sql"
    parts: List[str] = []
    for ds_key in eff.datasets.keys():
        tname = f"cg_{ds_key}"
        parts.append(
            f"""
DROP TABLE IF EXISTS {tname} CASCADE;
CREATE TABLE {tname} (
  ts TIMESTAMPTZ,
  symbol TEXT,
  interval TEXT,
  mode TEXT,
  source TEXT,
  payload_json TEXT
);
SELECT create_hypertable('{tname}', by_range('ts'), if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS on_{tname}_ts_sym ON {tname} (symbol, ts DESC);
""".strip()
        )
    cols = _features_columns()
    fdefs = [
        "ts TIMESTAMPTZ",
        "symbol TEXT",
    ] + [f"{c} DOUBLE PRECISION" for c in cols if c not in ("ts", "symbol")]
    parts.append(
        f"""
DROP TABLE IF EXISTS features_5m CASCADE;
CREATE TABLE features_5m (
  {',\n  '.join(fdefs)}
);
SELECT create_hypertable('features_5m', by_range('ts'), if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS on_features_5m_ts_sym ON features_5m (symbol, ts DESC);
""".strip()
    )
    fp.write_text("\n\n".join(parts) + "\n", encoding="utf-8")
    return fp


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="CryptoStorm storage/export tools")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_exp = sub.add_parser("export-parquet", help="Export raw JSONL and features CSV to Parquet")
    p_exp.add_argument("config", type=str)
    p_exp.add_argument("--data", type=str, default="data")
    p_exp.add_argument("--features", type=str, default="features")
    p_exp.add_argument("--out", type=str, default="parquet")

    p_sql = sub.add_parser("emit-ddl", help="Emit DDL for ClickHouse/Timescale")
    p_sql.add_argument("config", type=str)
    p_sql.add_argument("--kind", type=str, choices=["clickhouse", "timescale"], default="clickhouse")
    p_sql.add_argument("--out", type=str, default="./ddl")

    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    eff, warns, errs = validate_config(cfg, require_env=False)
    if errs:
        for e in errs:
            print(f"error: {e}")
        return 2

    if args.cmd == "export-parquet":
        summary = export_parquet(cfg, eff, data_root=Path(args.data), features_root=Path(args.features), out_root=Path(args.out))
        print(json.dumps({"exported": summary}))
        return 0
    if args.cmd == "emit-ddl":
        out_dir = Path(args.out)
        if args.kind == "clickhouse":
            fp = emit_clickhouse_sql(cfg, eff, out_dir=out_dir)
        else:
            fp = emit_timescale_sql(cfg, eff, out_dir=out_dir)
        print(f"DDL written to {fp}")
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

