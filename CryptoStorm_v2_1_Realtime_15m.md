# CryptoStorm v2.1 — Realtime (15m) Spec

## 0) Scope and SLO
- Cadence: **15‑minute bars**; funding 8 h.  
- SLO: P95 ≤ 90 s, P99 ≤ 120 s from UTC bar close to Telegram delivery. fileciteturn1file0  
- Design rule: **no delta/slope features**. Use snapshots, TWAPs, percentiles, cumulative/rolling sums. fileciteturn1file1

---

## 1) Data to retrieve (per symbol unless “aggregated”)
Persist raw JSONL per series. **No resampling at retrieve time**. Build a canonical **UTC 15‑minute close** index and align during feature build. **Forward‑fill funding_8h only**. Endpoint policy: **aggregated** for positioning (OI, liq, onchain, ETF); **exchange=binance** for microstructure (OHLCV, taker, funding, OB). Units: taker volume **base**, liquidation notional **USDT**, OI **contracts**. Funding settlements at 00:00/08:00/16:00 UTC. fileciteturn1file0 fileciteturn1file1

| Dataset file | Cadence | Scope | Purpose | Key fields |
|---|---:|---|---|---|
| `futures_ohlcv_15m.jsonl` | 15m | exchange=binance | price & volume | `ts, open, high, low, close, volume` |
| `spot_ohlcv_15m.jsonl` | 15m | exchange=binance | basis proxy | `ts, open, high, low, close, volume` |
| `funding_8h_ohlc.jsonl` | 8h events | exchange=binance | carry context | `ts, rate` (ffill to 15m grid) |
| `oi_15m_ohlc.jsonl` | 15m (or 5m raw) | **aggregated coin** | positioning | `ts, oi_open, oi_high, oi_low, oi_close` |
| `liquidation_15m.jsonl` | 15m (or 5m raw) | **aggregated** | stress proxy | `ts, liq_notional, liq_count` (USDT) |
| `taker_futures_15m.jsonl` | 15m | exchange=binance | flows | `ts, takerBuyVol, takerSellVol` |
| `orderbook_futures_snap.jsonl` | 5m snapshots | exchange=binance | liquidity features | `ts, bids[], asks[]` (use latest snapshot ≤60s before 15m close) |
| *(optional)* `taker_futures_15m_coin.jsonl` | 15m | **aggregated coin** | coin‑level flows (for share) | `ts, takerBuyVol, takerSellVol` (or synthesize coin sum of per‑symbol volumes) |
| *(optional)* `onchain_balances_daily.jsonl` | 1d | aggregated | context | `ts_day, reserve_change,…` |
| *(optional)* `etf_flows_daily.jsonl` | 1d | aggregated | context | `ts_day, net_flow,…` |

---

## 2) Feature engineering (15‑minute table per symbol)
**Alignment:** build UTC 15‑minute close index for the last 30 days (~96 bars/day × 30 ≈ 2,880 bars). **Point‑in‑time safe**; no future leakage. **Only funding_8h is forward‑filled**. If any required input at `ts_close` is missing → NaN and `data_ok=false`. **OB snapshot max age ≤ 60 s** (select latest 5m OB snapshot at/≤ 15m close). fileciteturn1file1

### 2.1 Columns and formulas
**Core OHLCV**
- `open, high, low, close, volume` (futures).  
- `log_ret_15m` for realized‑variance only.

**Basis proxy** *(needs spot)*
- `basis_now = (fut_close / spot_close) − 1`  
- `basis_TWAP_60m = mean(basis_now over last 4 bars)`  
- `basis_TWAP_120m = mean(basis_now over last 8 bars)` fileciteturn1file0

**Funding**
- `funding_now` = 8 h settled rate **ffilled** to each 15 m bar.  
- `funding_pctile_30d` = rolling percentile over 30 d. fileciteturn1file1

**Open interest (aggregated coin)**
- `oi_now = oi_close` for `symbol_to_coin(symbol)`.  
- `oi_pctile_30d` = rolling percentile over coin series. fileciteturn1file0

**Liquidations (aggregated)**
- `liq_notional_15m`, `liq_count_15m`.  
- `liq_notional_60m = sum(liq_notional_15m over last 4 bars)`. fileciteturn1file0

**Flow & Share** *(needs `taker_futures_15m`)*
- `taker_delta_15m = takerBuyVol − takerSellVol` (base).  
- `cvd_perp = cumsum(taker_delta_15m)` initialized at train window start.  
- `cvd_perp_45m = rolling_sum(taker_delta_15m, 3 bars)`.  
- `perp_share_60m = sum_{4 bars}(symbol taker vol) / sum_{4 bars}(coin‑aggregated taker vol)`. fileciteturn1file0

**Liquidity (optional)**
- `spread_bps`, `depth_ratio` within ±`orderbook_range_bp`; snapshot age ≤ 60 s. fileciteturn1file0

**Meta**
- `data_ok` boolean per §2 rules. fileciteturn1file1

---

## 3) Isolation Forest — training and live scoring
**Training (every 8 h)**  
Window: last 30 d. Coverage ≥ 0.95. Robust scaler with p1–p99 clipping. Algorithm: scikit‑learn **IsolationForest** with fixed per‑tier params; seed fixed. Score = `-decision_function(X_scaled)`. Threshold = `quantile(train_scores, q=threshold_q)`. Persist artifacts + manifest. fileciteturn1file0

```yaml
per_tier_overrides:
  A: { n_estimators: 300, max_samples: 512,  max_features: 0.70, contamination: "auto" }
  B: { n_estimators: 500, max_samples: 1024, max_features: 0.70, contamination: "auto" }
  C: { n_estimators: 500, max_samples: 1024, max_features: 0.60, contamination: "auto" }
```
fileciteturn1file0

**Live scoring**  
On each new 15m bar: build row → if `data_ok=false`, skip or down‑weight per config → apply scaler → IF score → compare to threshold. Reload artifacts atomically every 8 h. fileciteturn1file1

---

## 4) Alerts
- `pre_alert` when score ≥ threshold for `persist_k_15m` bars.  
- `storm` when it persists for `storm_confirm_k_15m`.  
- Suppress re‑fires with `cooldown_bars`. Defaults below preserve the prior 5m durations. fileciteturn1file0

```yaml
alerts:
  persist_k_15m: 1
  storm_confirm_k_15m: { A: 1, default: 2 }
  cooldown_bars: 4               # ≈60 minutes
  gates:
    enable: false
    any_of:
      - oi_pctile_30d >= 0.90
      - funding_pctile_30d >= 0.90
      - liq_notional_60m >= 12_000_000
```
fileciteturn1file0

---

## 5) Reproducibility — `manifest.json`
Record `run_id`, git SHA, dependency versions, exchange, per‑series modes, **intervals (15m/8h)**, `orderbook_range_bp`, `symbol_to_coin`, **unit conventions**, model params, scaler stats, seed, and train window `[start_ts,end_ts]`. Enables exact reruns and audits. fileciteturn1file0

---

## 6) Minimal realtime config (15m)
```yaml
realtime:
  poll_offset_s: 12
  jitter_s: 3
  workers: 8
  rps_limit: 5
  datasets:
    futures_ohlcv_15m: { enable: true }
    spot_ohlcv_15m:    { enable: true }
    oi_15m_ohlc:       { enable: true }
    liquidation_15m:   { enable: true }
    funding_8h:        { enable: true }
    taker_futures_15m: { enable: true }
    onchain_balances_daily: { enable: false }
    etf_flows_daily:        { enable: false }

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
  persist_k_15m: 1
  storm_confirm_k_15m: { A: 1, default: 2 }
  cooldown_bars: 4
  gates:
    enable: false
    any_of:
      - oi_pctile_30d >= 0.90
      - funding_pctile_30d >= 0.90
      - liq_notional_60m >= 12_000_000

telegram:
  enable: true
  mode: inline
  only_new: true
  dedup_window_s: 300
```
fileciteturn1file0

---

## 7) Pseudocode deltas — Flow, Share, Gates (15m)
```python
# ----- Flow & Share (15m) -----
delta = taker.buy_vol - taker.sell_vol               # base units
cvd_perp = cvd_buf.push_and_get(delta)               # cumulative since window start
cvd_perp_45m = roll3.push_and_get(delta)             # last 3×15m bars

vol60_sym  = sum4.push_and_get(taker.buy_vol + taker.sell_vol)
vol60_coin = sum4_coin.push_and_get(agg_taker.buy_vol + agg_taker.sell_vol)
perp_share_60m = vol60_sym / vol60_coin if vol60_coin > 0 else np.nan

# ----- Gates (duration‑equivalent) -----
gates_ok = True
if cfg.alerts.gates.enable:
    gates_ok = (
        (row["oi_pctile_30d"] >= 0.90) or
        (row["funding_pctile_30d"] >= 0.90) or
        (row["liq_notional_60m"] >= 12_000_000)
    )
```
fileciteturn1file1

---

## 8) Testing checklist
- Funding events snap to **{00:00, 08:00, 16:00 UTC}**; plateaus after ffill.  
- `symbol_to_coin` mapping resolves; unresolved → NaNs and `data_ok=false`.  
- OB snapshot age ≤ **60 s** or features NaN and `data_ok=false`.  
- Deterministic scores/alerts with fixed `random_state`.  
- Flow, share, liquidation 60m roll match the backtest builder on overlap. fileciteturn1file1

---

## 9) Dashboard exporter (1‑day view)
Write on each 15m bar:
- `/ui/alerts_1d.json` → last 24 h alerts for all symbols: `[{ts, symbol, kind, score, tier}]`  
- `/ui/series/{SYMBOL}_1d.json` → last 24 h: candles, score+thr, oi, liq and 60m roll, funding, basis, gates flag. Serve statically; poll each 60–90 s. fileciteturn1file0
