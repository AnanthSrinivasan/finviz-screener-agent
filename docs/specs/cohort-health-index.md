# Cohort Health Index — read the tape the way the trader reads it

**Status:** SPEC — awaiting user approval
**Date:** 2026-07-15
**Depends on:** soft dependency on `theme_map.json` (money-flow-dashboard spec) — degrades gracefully without it
**Conflicts:** touches `market_monitor.py` and cockpit generator; no overlap with scorecard/earnings agents

## 1. Problem

`market_state` is 100% index-level (up4/down4 across the whole market, SPY MAs, VIX, F&G). The user judges the tape by his growth cohort ("all memory names taken to the shed, AAOI down 50% — that's not normal") and on 2026-07-09 the cohort read beat the index read. Index-calm + leader-group distribution is the divergence that hits his book first, and the system is structurally blind to it.

## 2. Design

### 2.1 Cohort universe (built fresh daily, deduped, ~80–150 names)

Union of: open positions (all 3 books: `positions.json`, `paper_stops.json`, `live_alpaca_stops.json`) + active watchlist rows (`watching`/`focus`/`entry-ready`, not archived) + `theme_map.json` constituent tickers when the file exists.

### 2.2 Metrics — new step in `market_monitor.py`

One batched Alpaca daily-bars fetch (~150 symbols × 60 days, explicit `start` per 2026-07-10 rule):

- `pct_down4_today`, `pct_up4_today` — the user's carnage read
- `pct_above_20ma`, `pct_above_50ma`
- `pct_within_10_of_52wk_high` (60d window proxy: within 10% of 60d high)
- `cohort_score` 0–100 = weighted blend (proposal: 35% above-20ma, 25% above-50ma, 25% (1 − down4 share), 15% near-high share)
- `label`: `HEALTHY` (≥65) / `MIXED` (40–64) / `STRESS` (25–39) / `CARNAGE` (<25 OR pct_down4 ≥ 25%)

Persisted in the daily market-monitor record + rolling history (`cohort` block inside `market_monitor_history.json` records). Non-fatal: any failure → record written without cohort block, monitor unchanged.

### 2.3 Divergence signal (Phase 1 — informational only)

Fire when index and cohort disagree hard:

- `market_state ∈ {GREEN, THRUST, TREND-FOLLOW, STEADY-UPTREND}` AND `label ∈ {STRESS, CARNAGE}` → `⚠ COHORT DIVERGENCE — index says {state}, your cohort says {label}` to `#market-alerts`, with the 3 worst cohort names ($-moves) cited.
- Inverse (index RED, cohort HEALTHY) → one-line `cohort resilient` note inside the regular post, no separate alert.
- Dedup: alert once per label change, not per run.

### 2.4 Surfaces

- **Cockpit Gate block** (`gate_decision` context line): `Gate: FULL · index GREEN · cohort MIXED (58)` — cohort shown always, colored by label.
- **Market monitor Slack:** one added line `Cohort: 61 MIXED · 12% down-4 · 48% above 20MA`.
- **Phase 2 (observation-gated ≥4 weeks):** CARNAGE while index-GREEN tightens the cockpit gate one notch (FULL→HALF). Not in this spec.

## 3. Tests (`tests/test_cohort_health.py`)

- Universe build: 3-book union, dedup, archived-watchlist exclusion, missing theme_map.
- Score/label math on synthetic bars incl. CARNAGE down4 override.
- Divergence: fires on GREEN+STRESS, dedup on repeat label, inverse path.
- Non-fatal on bars failure.

## 4. Non-goals

Changing `market_state` transitions or executor sizing (Phase 2 decision after observation). Intraday cohort reads. Per-sector sub-cohorts (theme layer already gives that view).
