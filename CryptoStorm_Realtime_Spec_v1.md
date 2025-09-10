# CryptoStorm — Realtime Storm Detect (v1)

Scope: Extend batch CryptoStorm (30‑day, 5‑minute) into a low‑latency realtime pipeline that detects and publishes alerts within seconds after each 5‑minute UTC bar close, across all Binance USDⓈ‑M perpetuals (and related spot pairs for basis/share when enabled).

Goals
- Latency: < 20s from bar close (UTC) to alert emission (P95). Strict upper bound 60s.
- Coverage: ≥ 0.95 of bars across enabled core datasets over rolling 30d.
- Scale: 200–300 perp symbols; optional matching USDT spot symbols for basis/share.
- Robustness: Idempotent, resumable, rate‑limit aware, with clear backoff and fallbacks.

Non‑Goals (v1)
- Sub‑minute signals, tick‑level OB/trade streams, or exchange WebSocket fusion (considered for v2).
- Automated model re‑training beyond periodic refresh of the last trained artifacts.

Architecture (Realtime)
1) Scheduler (5‑minute aligned)
   - Align to UTC bar close; start work at close + offset (e.g., +5–15s).
   - Drives: retrieve_latest → feature_update → score_online → alert_emit.

2) Retrieve Latest (idempotent)
   - Fetch only new bars since last persisted ts per dataset/symbol; enforce interval‑grid alignment.
   - Respect v4 endpoints with time slicing for resilience; fallback to v3 per spec if needed.
   - Shared state of last_ts and recent hashes to avoid duplicates (file/Redis).

3) Incremental Feature Engine
   - Maintain per‑symbol ring buffers for last 30d window (in memory/Redis) for rolling features:
     funding ffills, basis, percentiles, TWAPs, rolling sums, CVDs, RV15, depth ratio.
   - Compute a single new feature row per symbol as new bars land.

4) Online Scoring + Alerting
   - Load latest IsolationForest artifacts (or parameters) on a fixed cadence (e.g., every 8h).
   - Score the new row; compare to threshold; apply persistence/cooldown; emit pre_alert / storm.
   - Sinks: Telegram, Webhook (future), on‑disk CSV (existing).

5) Monitoring + Control
   - Prometheus metrics: scheduler lag, dataset coverage/lag, retrieve success, feature/scoring latency, alert counts.
   - Logs: structured, leveled; HTTP debug opt‑in only.

Data Flow (per 5‑minute tick)
1. Wait until utc_now aligned to bar close + offset.
2. For each dataset/symbol (enabled): fetch new pages/slice; persist JSONL (dedupe by ts/hash); update last_ts.
3. Compute features for ts_close just produced; update CSV/DB.
4. Score row using current model; update scores CSV/DB.
5. Apply alerting rules; write alerts CSV; emit to Telegram/Webhook.

Config Additions (proposed)
```yaml
realtime:
  enable: true                 # turn on realtime scheduler/workers
  poll_offset_s: 8             # seconds after UTC close to start
  jitter_s: 2                  # random delay to avoid thundering herd
  workers: 4                   # parallel symbol workers
  rps_limit: 2                 # global max requests/sec across workers
  state:
    kind: file                 # file|redis
    file_root: ./.state        # for file state (default)
    redis_url: redis://localhost:6379/0  # when kind=redis
  datasets:
    # optionally override retrieve options per dataset
    futures_ohlcv_5m: { enable: true }
    funding_8h: { enable: true }
    oi_5m_ohlc: { enable: true }
    liquidation_5m: { enable: true }
    taker_futures_5m: { enable: false }
    orderbook_futures_5m: { enable: false }

model:
  live_scoring:
    enable: true
    reload_every_hours: 8      # refresh trained artifacts periodically
    artifacts_root: ./artifacts  # where to resolve latest run
```

CLI Additions (proposed)
- Retrieve watch: `python -m cryptostorm retrieve --watch --out data --rps 2 --workers 4`
- Feature update: `python -m cryptostorm feature --data data --out features --update-last`
- Online scoring: `python -m cryptostorm backtest --features features --online` (scores + alerts only, no training)
- Combined realtime runner: `python -m cryptostorm realtime run configs/example.yaml`

State & Idempotency
- last_ts map per symbol/dataset: { dataset/symbol → ts_ms }
- recent_hashes per output file (sliding Bloom filter or LRU) to avoid duplicates inside a slice
- storage backends: file (default), Redis (for multi‑process workers)

Scheduling & Alignment
- Align bar = floor(now_ms, 5m). Start work at bar + poll_offset_s.
- Guardrails: if any dataset behind by > 1 bar for a symbol, mark data_ok=false for that bar; continue.
- Retry policy: exponential backoff with cap; give up after N attempts per tick; keep next runs catching up.

Parallelism & Rate Limits
- Worker pool: symbol batches per process/thread; bounded queue.
- Token bucket for global RPS.
- Backoff shared across workers to avoid hot‑looping an endpoint.

Persistence & Storage
- Keep JSONL lineage as today. Consider Parquet daily roll‑ups in v2.
- Optional: ClickHouse/TimescaleDB for realtime querying (v2).
- Features: append CSV and/or write to DB.

Alerting
- Thresholding as per batch (IF score ≥ threshold, with persistence and cooldown).
- Telegram sink (existing), Slack/Webhook (future).
- Dedup TTL (e.g., do not re‑emit the same (symbol, kind, ts) more than once).

Observability
- Metrics (Prometheus):
  - cryptostorm_realtime_tick_lag_seconds
  - cryptostorm_retrieve_requests_total, failures_total
  - cryptostorm_dataset_lag_bars{symbol,dataset}
  - cryptostorm_features_latency_ms, scoring_latency_ms
  - cryptostorm_alerts_total{kind}
- Health endpoints (v2) via FastAPI: `/metrics`, `/healthz`.

Security & Ops
- Secrets via env/files only (API keys, Telegram token/chat).
- Respect provider ToS and rate limits; configurable RPS and backoff.
- Graceful shutdown: flush in‑flight writes, persist last_ts.

Phased Implementation Plan
Phase 0 — Groundwork (already done)
- Align request windows to interval grid; dedupe; robust retrieve paging/time slicing.
- Incremental features (MVP), online alert sinks (Telegram), Plotly live report.

Phase 1 — Watch Mode + Incremental Features
- Add `retrieve --watch` with 5‑minute aligned scheduler, idempotent last_ts, RPS token bucket.
- Add `feature --update-last` to compute exactly one new row per symbol based on new raw inputs.
- Wire a single‑process `realtime run` CLI looping: retrieve → feature → score → alert.

Phase 2 — Parallelization & State
- Introduce `realtime workers` (process/thread pool); per‑symbol batching.
- Add Redis state backend for last_ts and rolling windows (optional; file remains default).
- Add backpressure and retries per dataset.

Phase 3 — Model Reload & SLOs
- Periodic artifacts refresh without service restart.
- SLO monitors for lag, coverage, and error budgets; basic dashboards.

Phase 4 — API & Live UI (optional)
- FastAPI service: latest scores/alerts per symbol; WebSocket push to a live Plotly view.
- Access‑controlled HTTP basic auth or token.

Phase 5 — Storage Upgrade (optional)
- Parquet roll‑ups + ClickHouse/Timescale adapters for analytics.

Acceptance Criteria
- Correctness: Alerts produced for synthetic inputs match batch pipeline outcomes on overlapping windows.
- Timeliness: P95 alert latency ≤ 20s from UTC bar close in a 24h soak test.
- Stability: 0 crashes in 24h; automatic recovery after network hiccups; no duplicate alerts.
- Coverage: `audit` passes with configured min_ratio (e.g., 0.95) under normal conditions.

Testing Strategy
- Unit tests: scheduler alignment; last_ts state; incremental feature calculators; alert dedup.
- Integration tests: simulate N symbols with fake endpoints returning slices; verify outputs and latencies.
- Chaos tests: endpoint 429/5xx, empty pages, partial windows; ensure retries/backoff and catch‑up.

Risks & Mitigations
- Provider rate limits → RPS token bucket + exponential backoff + partial dataset enable.
- Skewed bar times → Align to UTC, add poll_offset_s, and tolerate one bar lag for non‑critical datasets.
- Large symbol sets → shard workers across processes; optional Redis for shared state.

Open Questions
- Do we centralize model artifacts (registry) or read from latest run folder? (v1: latest run folder by timestamp).
- Do we need ClickHouse/Timescale in v1? (optional; JSONL/CSV acceptable for a start).

