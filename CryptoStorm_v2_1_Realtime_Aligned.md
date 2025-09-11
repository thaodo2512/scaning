# CryptoStorm v2.1 — Realtime Data, Features, and Isolation Forest

---

## 0) Scope and SLO
- Cadence: 5‑minute bars; funding 8 h.  
- SLO: P95 ≤ 90 s, P99 ≤ 120 s from UTC bar close to Telegram delivered.  
- Design rule: **no delta/slope features** — only snapshots, TWAPs, percentiles, cumulative/rolling sums. fileciteturn4file0

---

## 1) Data to retrieve (per symbol unless marked “aggregated”)
Persist raw JSONL per series. Do not resample at retrieve time. Build a canonical UTC 5‑minute **close** index and align during feature build. Forward‑fill funding_8h only. fileciteturn4file1

| Dataset file | Cadence | Scope | Purpose | Key fields |
|---|---:|---|---|---|
| `futures_ohlcv_5m.jsonl` | 5m | exchange=binance | price & volume | `ts, open, high, low, close, volume` |
| `spot_ohlcv_5m.jsonl` | 5m | exchange=binance | basis proxy | `ts, open, high, low, close, volume` |
| `funding_8h_ohlc.jsonl` | 8h events | exchange=binance | carry context | `ts_settlement, rate` (ffill to 5m grid) |
| `oi_5m_ohlc.jsonl` | 5m | **aggregated coin** | positioning | `ts, oi_open, oi_high, oi_low, oi_close` (map via `symbol_to_coin`) |
| `liquidation_5m.jsonl` | 5m | **aggregated** | stress proxy | `ts, liq_notional, liq_count` (USDT) |
| `taker_futures_5m.jsonl` | 5m | exchange=binance | flows | `ts, takerBuyVol, takerSellVol` |
| *(optional)* `onchain_balances_daily.jsonl` | 1d | aggregated | context | `ts_day, reserve_change,…` |
| *(optional)* `etf_flows_daily.jsonl` | 1d | aggregated | context | `ts_day, net_flow,…` |

**Endpoint policy:** aggregated for positioning (OI, liq, onchain, ETF). exchange=binance for microstructure (OHLCV, taker, funding, OB). Units: taker volume **base**, liquidation notional **USDT**, OI **contracts**. Funding settlements at 00:00/08:00/16:00 UTC. fileciteturn4file0

---

## 2) Feature engineering (5‑minute table per symbol)
**Bar alignment:** build UTC 5‑minute close index for the last 30 days. Join by `ts_close`. If any required input is missing at `ts_close`, leave NaN and set `data_ok=false`. Only `funding_8h` is forward‑filled to the 5‑minute grid. fileciteturn4file1

### 2.1 Columns and formulas (superset to match v2.1)
**Core OHLCV**
- `open, high, low, close, volume` from futures.  
- `log_ret_5m = ln(close_t / close_{t-1})` (only for realized‑variance).

**Basis proxy** *(requires spot)*
- `basis_now = (fut_close / spot_close) − 1`  
- `basis_TWAP_60m = mean(basis_now over last 12 bars)`  
- `basis_TWAP_120m = mean(basis_now over last 24 bars)`

**Funding**
- `funding_now` = 8 h settled rate **forward‑filled** to each 5 m bar.  
- `funding_pctile_30d` = rolling percentile of `funding_now` over 30 d. fileciteturn4file1

**Open interest (aggregated coin)**
- `oi_now = oi_close` for `symbol_to_coin(symbol)`.  
- `oi_pctile_30d` = rolling percentile over the coin series. fileciteturn4file0

**Liquidations (aggregated)**
- `liq_notional_5m`, `liq_count_5m`.  
- **Include** `liq_notional_60m = sum(liq_notional_5m over last 12 bars)`. fileciteturn4file0

**Flow & Share** *(requires `taker_futures_5m`)*
- `taker_delta_5m = takerBuyVol − takerSellVol` (base units).  
- `cvd_perp_5m = cumsum(taker_delta_5m)` initialized at start of train window.  
- `cvd_perp_15m = rolling_sum(taker_delta_5m, 3 bars)` (last 15 m).  
- `perp_share_60m = sum_{12 bars}(takerBuyVol + takerSellVol for symbol) / sum_{12 bars}(takerBuyVol + takerSellVol for **coin aggregated**)`; use aggregated taker endpoint for the denominator if available, else compute coin‑sum across exchange=binance symbols. fileciteturn4file0

**Liquidity (optional if OB enabled later)**
- `spread_bps`, `depth_ratio` within ±`orderbook_range_bp`; only if snapshot age ≤ 60 s. fileciteturn4file0

**Context flags (optional)**
- `exch_reserve_flag` from `onchain_balances_daily` (boolean prepared upstream).  
- `etf_flow_flag` from `etf_flows_daily` (boolean prepared upstream).  
These are daily flags broadcast to each 5 m bar within the day. fileciteturn4file0

**Meta**
- `data_ok` boolean per bar. See §2.3.

### 2.2 Point‑in‑time rules
- No future bars in any rolling calc. Maintain per‑symbol ring buffers (length ~8,640 for 30 d).  
- Only funding is forward‑filled. Do **not** ffill prices, OI, liquidations, taker flows, or OB. fileciteturn4file1

### 2.3 `data_ok` policy (extended)
Set `data_ok=false` if **any enabled dependency** is missing at `ts_close`:
- Required base: {futures close, OI (coin), liquidations, funding_now}.  
- Plus, when enabled: {spot close (basis), taker_futures_5m (Flow & Share), order book (if using OB features)}; also onchain/ETF flags for that day if configured **required**. fileciteturn4file1

---

## 3) Isolation Forest — training and live scoring
### 3.1 Training (every 8 h)
- Window: last 30 days.  
- Feature filter: keep columns with coverage ≥ 0.95.  
- Scaling: robust scaler with p1–p99 clipping (persist stats).  
- Algorithm: scikit‑learn **IsolationForest** with **fixed per‑tier params**; seed fixed.  
- Score: `anomaly = -decision_function(X_scaled)` (higher = more anomalous).  
- Threshold: `quantile(train_scores, q=threshold_q)` (e.g., 0.97). fileciteturn4file0

**Tier overrides (match v2.1 exactly, including `contamination`)**
```yaml
per_tier_overrides:
  A: { n_estimators: 300, max_samples: 512,  max_features: 0.70, contamination: "auto" }
  B: { n_estimators: 500, max_samples: 1024, max_features: 0.70, contamination: "auto" }
  C: { n_estimators: 500, max_samples: 1024, max_features: 0.60, contamination: "auto" }
```
fileciteturn4file0

**Artifacts per training run:** IF model, scaler stats, feature list/order, threshold, seed, window [start,end], and a **manifest.json** (see §5). fileciteturn4file0

### 3.2 Live scoring
- On each new bar, build the feature row. If `data_ok=false` or required features missing, either **skip** or **down‑weight** per config.  
- Reload artifacts every 8 h **atomically**; apply scaler → IF score → compare to threshold. fileciteturn4file1

---

## 4) Alerts
- `pre_alert` when score ≥ threshold for `persist_k_5m` bars; `storm` when it persists for `storm_confirm_k_5m`; suppress re‑fires for `cooldown_bars`. fileciteturn4file0

### 4.1 **Gates** (explicit mechanism)
Alerts fire only if **gates.enable = true** **and** at least one gate condition holds at the alert bar:
```yaml
alerts:
  gates:
    enable: false
    any_of:
      - oi_pctile_30d >= 0.90
      - funding_pctile_30d >= 0.90
      - liq_notional_5m >= 1_000_000
```
Use gates to restrict to high‑stress regimes and cut false positives. fileciteturn4file0

---

## 5) Reproducibility — **manifest.json**
Write one `manifest.json` next to artifacts for each training run. Include:
- `run_id`, `git_sha`, dependency versions.  
- `exchange`, per‑series modes (e.g., `oi_5m: aggregated`), intervals, `orderbook_range_bp`.  
- `symbol_to_coin` map; **unit conventions**.  
- Full model params and scaler statistics; `random_state`.  
- Training window `[start_ts, end_ts]`.  
This guarantees exact re‑runs and auditability across realtime and backtest. fileciteturn4file0

---

## 6) Minimal realtime config (v2.1 aligned)
```yaml
realtime:
  poll_offset_s: 12
  jitter_s: 3
  workers: 8
  rps_limit: 5
  datasets:
    futures_ohlcv_5m: { enable: true }
    spot_ohlcv_5m:    { enable: true }      # needed for basis/share
    oi_5m_ohlc:       { enable: true }      # aggregated coin
    liquidation_5m:   { enable: true }      # aggregated
    funding_8h:       { enable: true }
    taker_futures_5m: { enable: true }      # enables Flow & Share features
    onchain_balances_daily: { enable: false }
    etf_flows_daily:  { enable: false }
model:
  live_scoring:
    enable: true
    reload_every_hours: 8
    artifacts_root: ./artifacts
  per_tier_overrides:
    A: { n_estimators: 300, max_samples: 512,  max_features: 0.70, contamination: "auto" }
    B: { n_estimators: 500, max_samples: 1024, max_features: 0.70, contamination: "auto" }
    C: { n_estimators: 500, max_samples: 1024, max_features: 0.60, contamination: "auto" }
alerts:
  persist_k_5m: 2
  storm_confirm_k_5m: { A: 1, default: 2 }
  cooldown_bars: 12
  gates:
    enable: false
    any_of:
      - oi_pctile_30d >= 0.90
      - funding_pctile_30d >= 0.90
      - liq_notional_5m >= 1_000_000
  telegram:
    enable: true
    mode: inline
    only_new: true
    dedup_window_s: 300
```
Values mirror the backtest spec while keeping soft realtime defaults. fileciteturn4file0

---

## 7) Pseudocode additions — Flow & Share and gates
```python
# ----- Flow & Share -----
delta = taker.buy_vol - taker.sell_vol                # base units
cvd_perp_5m = cvd_buf.push_and_get(delta)             # cumulative since window start
cvd_perp_15m = roll3.push_and_get(delta)              # last 3 bars

vol60_sym = sum60.push_and_get(taker.buy_vol + taker.sell_vol)
vol60_coin = sum60_coin.push_and_get(agg_taker.buy_vol + agg_taker.sell_vol)
perp_share_60m = vol60_sym / vol60_coin if vol60_coin > 0 else np.nan

# ----- Gates -----
gates_ok = True
if cfg.alerts.gates.enable:
    gates_ok = (
        (row["oi_pctile_30d"] >= 0.90) or
        (row["funding_pctile_30d"] >= 0.90) or
        (row["liq_notional_5m"] >= 1_000_000)
    )

if score >= threshold and gates_ok:
    streak += 1
    ...
```

---

## 8) Testing checklist (delta from previous guide)
- Flow features equal backtest feature builder on overlap (cvd, 15 m, share).  
- Gates suppress alerts when disabled and allow when any condition holds.  
- `data_ok` turns false when taker/onchain/ETF inputs are missing while enabled.  
- `manifest.json` present and complete per §5. fileciteturn4file1

---

## 9) Outputs
- `features/<SYM>.csv` now includes Flow & Share, Context flags, and `liq_notional_60m`.  
- Artifacts directory contains model, scaler, threshold, and **manifest.json**. fileciteturn4file0