# ⚡ Episodic Pivot Block — Spec

**Status:** Draft for review (2026-05-22)
**Reference cases:** QBTS 2026-05-21 (gap +33%, preceded by SB on 5/20), QBTS 2026-04-15 (gap +22%, preceded by SB on 4/13)

## Problem

Every actionable block in the daily screener gates on the moving-average stack (Stage 2 strict, or Stage Transition's early Stage 2, or Recovery Leader's SMA50/200% ≥ +15). High-momentum recovery names like QBTS, QUBT, RGTI gap on catalyst news while structurally still below the 200MA — every existing block rejects them.

Pradeep Bonde's framework keys off the **Setup Bar (SB)**: the quiet consolidation day BEFORE the explosive day. The trade is buying the SB; by the EP (Episodic Pivot) day itself the move is already gone.

## What's locked

After three rounds of back-and-forth + two backtest runs, these decisions are final:

1. **Pattern B only** (pullback-reversal SB). Pattern A (single-bar high-tight drift) is dead in all forms — backtest showed 0% hit at +15%/5d when tightened. Deferred indefinitely.
2. **Sector context is a TAG, not a GATE** — high-momentum leaders move standalone first; the ETF rotates later. Filtering on sector confirmation would lose the leaders.
3. **Output: `#momentum-alerts` (new channel, full detail) + `#daily-alerts` (1-line teaser) + chart grid HTML + ETF rotation HTML cross-link.**
4. **Production universe:** any ticker that appeared in `finviz_screeners_*.csv` in the last 20 trading days (~300-500 tickers). Catches QBTS via its 5/06 Power Move appearance.
5. **Sector rotation Slack (Mon/Thu)** adds a `🔥 X fired EP in SECTOR` line only when ≥1 🔥-tier (sector + peers confirmed) EP fired since the last post.

## Predicate (Pattern B — pullback-reversal SB)

### Bar-shape gate (Alpaca 90d daily bars per candidate)

```
RVol_today          ≤ 1.0      # drying volume
range_contract      ≤ 0.80     # today's range vs prior 10d avg range
prior_3d_cum_return ≤ -8%      # close[t-1] / close[t-4] - 1 ≤ -8%
chg_pct_today       ≥ +3%      # green reversal bar
no_expansion_in_last_7d        # no day where RVol≥3 OR chg≥+10 in last 7 trading days
```

### Pre-filter (cheap, Finviz snapshot)

```
SMA50%           ≥ +10         # price 10% above 50d SMA — current uptrend confirmed
Perf Quarter     ≥ +15         # 3-month RS, base-effect-free (NOT our RS Rating)
ATR%             ≤ 12          # filters biotech binaries / SPAC noise
Avg Volume       ≥ 1M          # liquidity floor
Market Cap       ≥ $500M       # excludes micro-cap pumps
Price            ≥ $5          # penny-stock floor
sector ∉ {Utilities, Energy, Real Estate, Basic Materials, Consumer Defensive}
industry NOT containing {Biotech, Drug Manufacturers - Specialty & Generic}
ticker appeared in finviz_screeners_*.csv ≥ 1× in last 20 trading days
ticker NOT in open positions.json
ticker NOT already surfaced in another block this run (RTE/FB/HG/HTF-BR/RSL/Stage Transition/Recovery Leader/21 EMA PB)
```

### Per-ticker dedup

Persistent tracker at `data/episodic_pivots.json`:
```json
{
  "QBTS": {
    "last_fire_date": "2026-05-20",
    "last_fire_tags": ["SECTOR", "PEERS"],
    "fire_count": 1
  }
}
```
Cooldown: **20 trading days per ticker**. Pradeep doesn't double-EP — if the setup played out, fine; if it failed, the name needs 4 weeks before re-qualifying.

## Context tags (computed on fire day)

Four mutually-exhaustive tags, computed from existing data:

| Tag | Computed from | When true |
|---|---|---|
| **SECTOR ↑** | `data/etf_rotation.json` + `data/sector_rotation_YYYY-MM-DD.json` via `agents/utils/sector_lookup.py` | Parent ETF in `BASE` or `PRE-BREAKOUT` bucket **OR** `rank_delta_5d ≤ -5` |
| **PEERS** | scan `data/episodic_pivots.json` history for same industry | ≥1 ticker in same Finviz Industry fired EP in last 5 trading days |
| **STANDALONE** | neither | auto-applied when both above false |
| **LEADER** | SECTOR ↑ but no PEERS | first-mover in a rotating sector |

Emoji mapping for Slack:
- 🔥 = SECTOR ↑ + PEERS — sector confirmed and peers co-firing (the QBTS class)
- 🌊 = PEERS only — early thematic move, ETF hasn't caught up yet
- 📈 = LEADER (SECTOR ↑ only) — first-mover in rotating sector
- ⚡ = STANDALONE — one-off setup, no sector edge

## Outputs

### 1. `#momentum-alerts` Slack post (full detail)

Posts once per run, only when ≥1 fire.

```
⚡ Episodic Pivot — Pullback Reversal Setups (3 today, 1 🔥)
Pradeep SB lane: drying volume + 3d pullback + reversal up
Sizing guide: 🔥/🌊 full · 📈 half · ⚡ quarter

🔥 AMKR · $68.49 · +4.5% · RVol 0.58 · ATR 5%
   [SECTOR ↑ SMH] [PEERS: AXTI, COHU]
   3d pullback -9.1% → reversal · dist 52w-hi -10%
   /stock-research AMKR

🌊 AXTI · $112.88 · +6.6% · RVol 0.83 · ATR 9%
   [PEERS: AMKR, COHU]
   3d pullback -13.2% → reversal · dist 52w-hi -16%
   /stock-research AXTI

⚡ BW · $14.69 · +11.2% · RVol 0.34 · ATR 7%
   [STANDALONE]
   3d pullback -15.5% → reversal · dist 52w-hi -8%
   /stock-research BW
```

Sort order: 🔥 → 🌊 → 📈 → ⚡. Within tier, rank by `chg_pct × (1 - rvol)` (rewards strong reversal on drying volume).

### 2. `#daily-alerts` Slack teaser (1 line)

Posted at end of daily screener message, only when ≥1 EP fire:
```
⚡ 3 EP setups today (1 🔥 sector-confirmed) — see #momentum-alerts
```

### 3. `finviz_chart_grid_YYYY-MM-DD.html`

New collapsible `<details open>` section `⚡ Episodic Pivots — Pullback Reversal`. Cards show:
- Finviz chart image
- Context-tag badges (color-coded: 🔥 red, 🌊 blue, 📈 amber, ⚡ gray)
- Metrics row: Close · RVol · Chg% · ATR% · dist-from-52w-hi · prior-3d-cum
- `/stock-research <ticker>` link

### 4. `data/etf_rotation.html` cross-link

On each ETF card, when ≥1 EP fired today on a ticker mapping to that ETF, append a line under the metrics:
```
EP setups in this sector: AMKR (🔥), AXTI (🌊), COHU (🌊)
```

### 5. Sector Rotation Slack (Mon/Thu only)

Only when ≥1 🔥 EP fired since the last sector rotation post, append one line per 🔥:
```
🔥 AMKR fired EP in SMH (rotating in) — see #momentum-alerts
```

## Watchlist integration

Auto-add on EP fire:
- `priority=watching` (not `focus` — EPs are pre-confirmation, high-vol, need human review before promotion)
- `source=episodic_pivot_auto` (8th entry path)
- If ticker already in watchlist: no-op (preserve existing priority/source); add metadata `last_ep_fire_date` to entry

## Universe & runtime

**Universe build (cheap):**
```python
from glob import glob
from datetime import date, timedelta
import pandas as pd

cutoff = (date.today() - timedelta(days=30)).isoformat()  # 30 cal days ≈ 20 trading
recent_csvs = [f for f in glob('data/finviz_screeners_*.csv') if f.split('_')[-1][:10] >= cutoff]
universe = set()
for f in recent_csvs:
    universe.update(pd.read_csv(f)['Ticker'].tolist())
# Filter to today's Finviz snapshot rows
candidates = today_snapshot_df[today_snapshot_df['Ticker'].isin(universe)]
```

Expected universe: 300-500 tickers/day. Pre-filter (Finviz snapshot, no network) drops most to 20-50 candidates. Bar-shape gate fetches 90d Alpaca bars for those 20-50 (parallel, ~5 sec). Production fire rate: **2-5/week** projected from backtest extrapolation.

## Files to touch

1. **`agents/screener/finviz_agent.py`** — add `_is_episodic_pivot(row, bars_df, history)` predicate, universe build, fire detection loop, Slack post for `#momentum-alerts`, teaser line for `#daily-alerts`, HTML gallery section, watchlist auto-add hook.
2. **`agents/utils/episodic_pivot.py`** (new) — pure functions: `passes_pre_filter`, `passes_bar_shape`, `compute_context_tags`, `compute_emoji`, `format_slack_card`, `format_html_card`. Easy to unit test.
3. **`data/episodic_pivots.json`** — new tracker file (per-ticker last_fire_date + tags + count).
4. **`agents/sector_rotation.py`** — extend `etf_rotation.html` ETF card renderer to optionally show "EP setups in this sector" line. Read fires from `data/episodic_pivots.json` filtered to today.
5. **`agents/screener/watchlist_lifecycle.py`** (or wherever screener_auto / htf_base_reclaim_auto sources are written) — add `episodic_pivot_auto` source.
6. **`.github/workflows/daily-finviz.yml`** — add `SLACK_WEBHOOK_MOMENTUM: ${{ secrets.SLACK_WEBHOOK_MOMENTUM }}` to env.
7. **`tests/test_episodic_pivot.py`** (new) — unit tests for pre-filter, bar-shape gate, context tags, dedup, watchlist add idempotency.
8. **`CLAUDE.md`** — document under "Daily Screener Signals" + add `SLACK_WEBHOOK_MOMENTUM` to secrets table.
9. **`SYSTEM_DOCS.md`** — same.
10. **`MEMORY.md`** — add `project_episodic_pivot_block.md` after merge (separate from the existing `project_episodic_pivot_research.md` which captures the research findings).

## Backtest evidence

Source: `scripts/backtest_sb_detector_v2.py`, results in `data/sb_backtest_results.csv`.
Universe: 152 active watchlist tickers, 90 trading days ending 2026-05-22.

| Variant | Fires | Per day | Hit +15%/5d | Hit +10%/5d | Worst |
|---|---|---|---|---|---|
| Pattern A (loose) | 628 | 6.98 | 8% | 21% | -7.8% |
| Pattern A (tight + consecutive + cooldown) | 28 | 0.31 | **0%** | 18% | -0.2% |
| **Pattern B (shipping)** | **13** | **0.14** | **15%** | **23%** | **+0.1%** |

Pattern B hits: BW +25%, CIEN +29%, ICHR +10%, DK +9%, VIR +10% (note: VIR is biotech — would be excluded in production by the biotech industry filter).

Production universe (~300-500 tickers via "screener appearance ≥1 in 20d") extrapolates to ~2-5 fires/week. Manageable for #momentum-alerts.

## Reference data (DO NOT DELETE — used for regression validation)

QBTS daily bars 2026-04-13 (SB) → 2026-04-15 (EP +22%) and 2026-05-20 (SB) → 2026-05-21 (EP +33%) are the canonical Pattern B reference cases. The implementation MUST fire on both setup days (4/13 and 5/20) when fed the historical Finviz snapshot + Alpaca bars. Add a fixture-based unit test for this in `tests/test_episodic_pivot.py`.

Note: QBTS 4/13 has `dist_hi5 = -4.6%` (high-tight subspace of B) — it actually satisfies `cum3_prev ≤ -8%` only marginally. **The 4/13 case may not fire under the locked Pattern B gates** — its `cum3_prev` is around -1% (no 3-day pullback). This is a known gap; the 4/13 SB is closer to Pattern A high-tight than Pattern B pullback-reversal. We ship the 5/20 lane and accept the 4/13 miss as the explicit cost of not shipping Pattern A.

## Open questions for review

None — all 3 final questions answered:
- Q1: `#daily-alerts` teaser = YES (1 line)
- Q2: Sector rotation Mon/Thu cross-mention = YES (only 🔥 tier)
- Q3: Production universe = "appeared in screener in last 20d" (~300-500 tickers)

## Approval gate

**This spec needs explicit "approved" from user before any code is written.** Per CLAUDE.md workflow: spec → review → tasks → execute.
