# Signal Scorecard — measure every callout block, prune what doesn't pay

**Status:** SPEC — awaiting user approval
**Date:** 2026-07-15
**Depends on:** nothing (fully independent)
**Conflicts:** touches `finviz_agent.py` (also touched by earnings-entry-gate spec — coordinate or run sequentially)

## 1. Problem

The screener has 9 auto-entry paths and ~12 Slack callout blocks, each added after a missed trade. No process measures fires → forward returns per block. The EP block got a one-off design-time backtest; no other block ever got one, and none get ongoing measurement. The system can only grow; it can never learn that a block doesn't pay. Simplification requires measurement.

## 2. Design

### 2.1 Fires log — `data/signal_fires.json` (NEW, append-only rolling 400 days)

New step in `finviz_agent.py` `main()`, after all callout blocks are computed, before Slack post. One record per (date, block, ticker):

```json
{"date": "2026-07-15", "block": "ready_to_enter", "ticker": "CRWD",
 "price": 412.35, "q": 84, "atr_pct": 3.2, "rank_in_block": 1}
```

Blocks logged: `ready_to_enter`, `fresh_breakout`, `hidden_growth`, `htf_base_reclaim`, `power_play`, `base_building`, `stage_transition`, `rotation_catalyst`, `recovery_leader`, `episodic_pivot`, `ema21_pullback`, `rs_leader`, `big_movers`. `price` = Finviz snapshot Price at fire. Idempotent per day (re-runs replace that date's records). Non-fatal: log failure never blocks the screener.

### 2.2 Weekly scoring — new §6 in `finviz_weekly_agent.py`

For every fire older than the horizon, fetch daily bars (Alpaca, explicit `start` — see 2026-07-10 rule) and compute:

- `ret_5d`, `ret_20d` (close-to-close from fire date)
- `hit10_20d` — touched +10% within 20 sessions
- `max_drawdown_20d`
- same-window SPY return (excess return)

Aggregate per block over trailing 90 days AND lifetime: `n_fires`, `median_ret_5d`, `median_ret_20d`, `median_excess_20d`, `win_rate_5d`, `hit10_rate`, `worst`. Persist `data/signal_scorecard.json`. Cache computed fire outcomes so each fire is scored once (append `scored: {ret_5d, ...}` back onto the fire record).

### 2.3 Output

- **Weekly HTML:** new section `📏 Signal Scorecard` — table sorted by `median_excess_20d`, red rows for underperformers, sparkline of fires/week.
- **Weekly Slack:** 3 lines max — best block, worst block, any block newly flagged REVIEW.
- **Flag rule (informational — human decides):** `n_fires ≥ 20` over ≥8 weeks AND `median_excess_20d ≤ 0` AND `hit10_rate < 15%` → status `REVIEW/RETIRE` in table + Slack. No block is auto-disabled.

### 2.4 Backfill

One-off script `scripts/backfill_signal_fires.py`: reconstruct historical fires where recoverable — watchlist `source=*_auto` + `added` dates, `rs_leaders.json`, `episodic_pivots.json`, `hidden_growth.json` snapshots in git history is out of scope; use current state files only, tagged `backfilled: true` (lower confidence, excluded from lifetime stats by default).

## 3. Tests (`tests/test_signal_scorecard.py`)

- Fires log: idempotent same-day rewrite, 400-day trim, non-fatal on IO error.
- Scoring: ret/hit10/drawdown math on synthetic bars; fire younger than horizon skipped; scored-cache respected.
- Flag rule thresholds; excess return vs SPY.
- Render import-safe, no network.

## 4. Non-goals

Auto-disabling blocks; scoring manual trades (that's trader-mirror); intraday granularity.
