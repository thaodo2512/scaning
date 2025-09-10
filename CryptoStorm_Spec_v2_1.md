# CryptoStorm Detect — Modular Specification (v2.1)

**Scope:** Binance USDⓈ-M perpetuals (perps) + Binance spot (basis/share), rolling last **30 calendar days** (UTC).  
**Design rule:** **No delta-over-time features** — only snapshots, TWAPs, percentiles, cumulative/rolling sums.

---

## 0) System Overview

- **Pipeline:** `retrieve → feature → backtest → report`  
- **Artifacts:** `ARTIFACTS_ROOT/<RUN_ID>/…`  
- **Run window:** rolling **30 days** ending “now” (UTC)  
- **Symbols:** Tiered — Tier-A (BTCUSDT/ETHUSDT), Tier-B (top-20), Tier-C (+30 optional)

---

## 1) Configuration (YAML, Coinglass-only)

```yaml
run:
  run_id: "YYYYMMDD-HHMMSS"
  artifacts_root: "./artifacts"

universe:
  symbols: ["BTCUSDT","ETHUSDT"]
  tiering: { A:["BTCUSDT","ETHUSDT"], B: 20, C: 30 }

acquisition:
  mode: coinglass_only
  days: 30
  coinglass:
    base_url: https://open-api-v4.coinglass.com
    api_key_env: COINGLASS_API_KEY
    exchange: binance
    quote: USDT
    prefer_aggregated: true
    paging: { page_limit: 500, backoff_initial_s: 1, backoff_max_s: 64 }
    intervals:
      futures_ohlcv: "5m"
      spot_ohlcv:    "5m"
      funding_8h:    "8h"   # settled funding (canonical)
      funding_5m:    "5m"   # predicted funding (optional proxy)
      oi_ohlc:       "5m"
      taker_volume:  "5m"
      liquidation:   "5m"
      orderbook_sample: "last_of_5m"
      orderbook_range_bp: 10         # ±10 bps aggregation band
    per_series_mode:
      futures_ohlcv: exchange        # microstructure → exchange (binance)
      spot_ohlcv:    exchange
      funding_8h:    exchange
      funding_5m:    exchange
      oi_5m:         aggregated      # positioning → aggregated coin-level
      ls_ratio:      aggregated
      taker_futures: exchange
      taker_spot:    exchange
      liquidation:   aggregated
      orderbook:     exchange
      onchain:       aggregated
      etf:           aggregated
    enable:
      spot_ohlcv_5m: true
      funding_pred_5m: false
      taker_spot_5m:  false
      orderbook_spot_5m: false
      onchain_balances_daily: false
      etf_flows_daily: false

features:
  # Only snapshots/TWAP/percentiles/cumulative sums — no deltas/slopes.
  qc:
    drop_day_if_missing_minute_ratio_gt: 0.01
  funding:
    settlement_hours_utc: [0, 8, 16]   # snap settlement events to these hours

model:
  algo: isolation_forest
  random_state: 42
  train_window_days: 30
  retrain_every_hours: 8
  threshold_q: 0.97
  min_feature_coverage: 0.95
  scaler: { type: robust, clip_quantiles: [0.01, 0.99] }
  iforest_defaults: { contamination: "auto", max_features: 0.70, bootstrap: false, n_jobs: -1 }
  # FIXED per tier (no auto-tuning)
  per_tier_overrides:
    A: { n_estimators: 300, max_samples: 512,  max_features: 0.70, contamination: "auto" }
    B: { n_estimators: 500, max_samples: 1024, max_features: 0.70, contamination: "auto" }
    C: { n_estimators: 500, max_samples: 1024, max_features: 0.60, contamination: "auto" }

alerts:
  persist_k_5m: 2                       # bars ≥ threshold → pre_alert
  storm_confirm_k_5m: { A: 1, default: 2 }
  cooldown_bars: 12
  gates:
    enable: false
    any_of:
      - oi_pctile_30d >= 0.90
      - funding_pctile_30d >= 0.90
      - liq_notional_5m >= 1_000_000

labels:
  pct_move: 0.05
  horizons_min: [30, 60, 90, 120]

conventions:
  symbol_to_coin:
    BTCUSDT: BTC
    ETHUSDT: ETH
  units:
    taker_volume: base        # Δ and CVD in base units
    liquidation_notional: USDT
    open_interest: contracts
  orderbook:
    max_snapshot_age_s: 60
```

---

## 2) Module A — Retrieve (Coinglass, with endpoint policy)

**Policy:**  
- **Aggregated** for **positioning** signals: **open interest (coin)**, **global long/short**, **liquidations (aggregated)**, **on-chain**, **ETF**.  
- **Exchange=binance** for **microstructure**: **futures/spot OHLCV**, **funding (8h & 5m)**, **taker buy/sell**, **order book**.  
- If an aggregated endpoint is unavailable/unsuitable → **fallback to exchange=binance**.

**Outputs (`data/<SYM>/`):**
```
futures_ohlcv_5m.jsonl
spot_ohlcv_5m.jsonl
funding_8h_ohlc.jsonl
funding_pred_5m_ohlc.jsonl
oi_5m_ohlc.jsonl
taker_futures_5m.jsonl
taker_spot_5m.jsonl
liquidation_5m.jsonl
orderbook_futures_5m.jsonl
orderbook_spot_5m.jsonl
onchain_exchange_balance_daily.jsonl
etf_flows_daily.jsonl
```

**Endpoints (preferred → fallback):**


| Dataset                       | Preferred (v4)                                                               | Fallback/Notes                                                                                                           |
| ----------------------------- | ---------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| Futures OHLCV 5m              | `/api/futures/price/history?exchange=binance&interval=5m`                    | v3: `/api/price/ohlc-history?exchange=binance&interval=5m`                                                               |
| Spot OHLCV 5m                 | `/api/spot/price/history?exchange=binance&interval=5m`                       | v3: `/api/spot/price/ohlc-history?exchange=binance&interval=5m`                                                          |
| Funding 8h (settled)          | `/api/futures/funding-rate/history?exchange=binance&interval=8h`             | v3: `/api/futures/fundingRate/ohlc-history?exchange=binance&interval=8h`                                                 |
| Funding 5m (predicted)        | `/api/futures/funding-rate/history?exchange=binance&interval=5m`             | Weighted predictions via `/api/futures/funding-rate/oi-weight-history` or `/api/futures/funding-rate/vol-weight-history` |
| Open Interest 5m (aggregated) | `/api/futures/open-interest/aggregated-history?interval=5m`                  | v3 per‑exchange: `/api/futures/openInterest/ohlc-history?exchange=binance`                                               |
| Long/Short ratios             | `/api/futures/global-long-short-account-ratio/history`                       | Top-trader exchange: `/api/futures/top-long-short-account-ratio/history`                                                 |
| Taker Buy/Sell 5m (perps)     | `/api/futures/v2/taker-buy-sell-volume/history?exchange=binance&interval=5m` | Aggregated: `/api/futures/aggregated-taker-buy-sell-volume/history?interval=5m`                                          |
| Taker Buy/Sell 5m (spot)      | `/api/spot/taker-buy-sell-volume/history?exchange=binance&interval=5m`       | Aggregated: `/api/spot/aggregated-taker-buy-sell-volume/history?interval=5m`                                             |
| Liquidations 5m (aggregated)  | `/api/futures/liquidation/aggregated-history?interval=5m`                    | Exchange-specific: `/api/futures/liquidation/history?exchange=binance&interval=5m`                                       |
| Order book (perps)            | `/api/futures/orderbook/ask-bids-history`                                    | Aggregated: `/api/futures/orderbook/aggregated-ask-bids-history`                                                         |
| Order book (spot)             | `/api/spot/orderbook/ask-bids-history`                                       | Aggregated: `/api/spot/orderbook/aggregated-ask-bids-history`                                                            |
| On‑chain exchange balances    | `/api/exchange/balance/list`                                                 | v3: `/api/exchange/balance/list?interval=1d`                                                                             |
| ETF flows (BTC/ETH)           | `/api/etf/bitcoin/flow-history` & `/api/etf/ethereum/flow-history`           | HK ETFs: `/api/hk-etf/bitcoin/flow-history`                                                                              |


**Notes:**
- Persist raw responses; **no resampling** here. Funding 8h is forward-filled later on the 5-minute grid.  
- Use `symbol_to_coin` mapping for aggregated OI/liquidations.

---

## 3) Module B — Feature Engineering

**Goal:** Build a clean **5-minute** feature table per symbol; align to **UTC bar close**. Missing inputs → **NaN**; set `data_ok=false`. **No deltas/slopes**.

**Columns:**
- **Core OHLCV:** `ts` (close ms), `symbol`, `open`, `high`, `low`, `close`, `volume`.
- **Basis proxy (if spot present):** `basis_now`, `basis_TWAP_60m`, `basis_TWAP_120m`.
- **Funding:** `funding_now` (ffilled from 8h settlements), `funding_pctile_30d`, optional `funding_pred_twap_60m` (from 5m predicted).
- **Open interest:** `oi_now`, `oi_pctile_30d` (aggregated coin mapped via `symbol_to_coin`).
- **Flow & share:** Δ=`takerBuyVol − takerSellVol` per bar; `cvd_perp_5m`=cumsum(Δ); `cvd_perp_15m`=rolling 3 bars; optional `perp_share_60m`.
- **Liquidity:** `spread_bps` (and optional `depth_ratio` within ±`orderbook_range_bp` bps).
- **Liquidations:** `liq_notional_5m` (USDT), `liq_count_5m`; optional `liq_notional_60m`.
- **Volatility:** `rv_15m` (realized variance of log-returns over last 3 bars).
- **Context flags:** `exch_reserve_flag`, `etf_flow_flag`.
- **Meta:** `data_ok`.

**Rules:**
1) Build canonical **UTC 5-minute close** index for the 30-day window.  
2) **Forward-fill only `funding_8h`** to 5 m; do not ffill other series (except last OB snapshot per bar).  
3) Order book snapshot must be ≤ **60 s** old; else NaN & `data_ok=false`.  
4) Units: taker volume = **base**; liquidation notional = **USDT**; open interest = **contracts**.

---

## 4) Module C — Backtest (walk-forward, 5-minute)

- Train window **30 days**; **retrain every 8 hours**.  
- Use features with coverage ≥ **0.95** in the window.  
- Robust scale + clip (train p1–p99).  
- Model: **IsolationForest** with **fixed per-tier params**; score = `-decision_function`.  
- Threshold = `quantile(train_scores, q=threshold_q)` (e.g., 0.97).  
- Alerts: `pre_alert` if ≥ threshold for `persist_k_5m` bars; `storm` when it persists for `storm_confirm_k_5m`; `cooldown_bars` to suppress re-fires.  
- Labels: explosion if max(|move|) ≥ `pct_move` within horizons {30,60,90,120} minutes from alert bar.  
- Metrics: precision (primary); optional recall/F1; counts; average lead time; hit-rate by tier/symbol.

Outputs:
```
ARTIFACTS_ROOT/<RUN_ID>/
  alerts/<SYM>.csv
  scores/<SYM>.csv
  metrics/metrics.json
```

---

## 5) Module D — Report (Lightweight Charts)

- Per-symbol **self-contained HTML** using TradingView’s **Lightweight Charts**.  
- **Panels:**  
  - top: **candles** + markers for `pre_alert` / `storm` (+ optional overlays: `basis_now`, `funding_now`),  
  - middle: **IF score** line + **threshold** dotted line,  
  - bottom: **OI** (area) + **Liquidation notional** (histogram).  
- Embed JSON arrays for `price`, `score`, `threshold`, `oi`, `liq`, and `alerts`.  
- Cross-panel time sync; offline-openable HTML.

---

## 6) Acceptance Tests

- Funding events snap to **{00:00, 08:00, 16:00 UTC}**; plateaus after forward-fill.  
- `symbol_to_coin` mapping works (e.g., ETHUSDT→ETH); unresolved mapping → NaNs & `data_ok=false`.  
- Order book snapshot age ≤ **60 s**; otherwise OB features NaN & `data_ok=false`.  
- Deterministic scores/alerts with fixed `random_state`.  
- Report renders offline; markers align with scores; crosshair sync works.

---

## 7) Manifest (reproducibility)

Include: `run_id`, git SHA, dependency versions, **exchange**, per-series modes, intervals, `orderbook_range_bp`, `symbol_to_coin` map, **unit conventions**, model params, scaler stats, seed, and train/eval windows.
