# Spec — HTF Base Coverage + Screener Gate Tightening

**Date:** 2026-05-09
**Status:** Draft, awaiting execution
**Trigger:** RKLB (+34% earnings gap May 8) wasn't surfaced. Retro analysis (`docs/research/missed_bursts_2026-05-09.md`) found 4 genuine in-mandate misses across 60 days — gates are sound but leak narrowly, and HTF base reclaims (RKLB-class) need a dedicated path.

## Read first
- `docs/research/missed_bursts_2026-05-09.md` — empirical baseline (98 +30% bursts, 87% out-of-mandate by design, 4 fixable)
- `docs/research/README.md` — analysis playbooks
- Memory: `project_screener_coverage_2026-05-09.md`, `reference_research_log.md`
- `CLAUDE.md` → "Daily Screener Signals" section — current Ready-to-Enter / Fresh Breakout / Base Building / RS Leader predicates

## Goals
1. Catch HTF base reclaims where dist-from-52w-high disqualifies but dist-from-recent-swing-high is tight (RKLB Apr 16 reference: -16.7% from ATH but -11% from Jan/Feb swing high).
2. Plug 4 narrow leaks (MTSI, RMBS, TWLO, IHRT) without inviting low-Q / high-ATR / extended noise.
3. Improve visibility for Base Building (currently top-5 Slack cap likely ranks out HTF candidates on busy days).

## Non-goals
- Loosening Q < 70 to catch penny pumps. By design.
- Loosening ATR > 8 to catch volatile gap-and-go names. By design.
- Catching dist > 0 already-extended names. By design.
- Predicting earnings gaps. Not catchable by daily setup screener.

## Changes

### 1. RTE dist gate softening
**File:** `finviz_agent.py` → `_is_ready_to_enter()` predicate (or whichever function gates the 🎯 Ready to Enter block — grep `Ready to Enter`).
**Change:** `dist <= -10` → `dist <= -12`.
**Why:** MTSI (-10.02%), RMBS (-10.33%) — clean Q≥80 / Stage-2-perfect names killed by 0.02-0.33pp. Both ran +30% / +54%.
**Risk:** widens RTE band slightly into Base Building zone. Acceptable — RTE is "ready now" with VCP/Q gates that BB doesn't have.

### 2. Fresh Breakout RVol exception for tight quality names
**File:** `finviz_agent.py` → `_is_fresh_breakout()` predicate.
**Change:** keep `RVol >= 1.2` as default, but add: **OR** `(Q ≥ 80 AND ATR ≤ 6 AND RVol ≥ 1.0)`.
**Why:** TWLO (Q73, RVol 0.90) and RMBS (Q82, RVol 0.85, ATR 5.3) had quiet pre-break days. Quality + tightness justifies the exception.
**Risk:** small — Q≥80 + ATR≤6 + Stage-2-perfect is a narrow set, RVol≥1.0 is still "active not dead."
**Note:** TWLO's Q=73 still wouldn't qualify even with exception. Accept that — Q73 is borderline; don't relax further.

### 3. Swing-high pivot detector → 🌀 HTF Base Reclaim block
**New module:** `agents/utils/swing_pivot.py`
**Function signature:**
```python
def compute_swing_pivot(ticker: str, days: int = 90) -> dict | None:
    """Returns {swing_high, swing_high_date, dist_from_swing_high_pct, last_close} or None on no data."""
```
- Fetches `days` daily bars from Alpaca (`feed=iex`, `adjustment=split` — see retro caveats).
- `swing_high` = max high over the last `days` bars EXCLUDING last 5 days (avoid using today's high as its own pivot).
- `dist_from_swing_high_pct = (last_close - swing_high) / swing_high * 100`.

**New predicate** in `finviz_agent.py` → `_is_htf_base_reclaim()`:
- Stage 2 perfect ✓
- Q ≥ 75 ✓
- ATR% ≤ 7 ✓
- `dist_from_52w_high_pct < -12` (would be excluded by RTE/FB)
- `dist_from_swing_high_pct >= -10` (close to recent swing pivot — the actionable level)
- Rising MA stack: SMA20% > 0, SMA50% > 0, SMA200% > 0
- Peel-warn safe (`SMA50% / ATR% <= peel_warn`)
- RVol ≥ 1.0
- Not held
- Not already in RTE / Fresh Breakout / Base Building / RS Leader / Power Play

**Output:**
- New Slack block: 🌀 **HTF Base Reclaim** — top 5 by Q. Format: `<ticker> · Q{q} · 52w {dist52}% · swing {dist_swing}% · RVol {rvol}`.
- HTML gallery: collapsible `<details>` section, all qualifiers (uncapped).
- Persisted in `daily_quality_YYYY-MM-DD.json` with `htf_base_reclaim: true` flag.

**Why dedicated block, not just widening BB:** different signal. BB = "name is consolidating in a wider base, watch." HTF Base Reclaim = "name has reclaimed its recent swing pivot from a deeper drawdown and is set up for the next leg." Mixing dilutes both.

**Cost:** 1 Alpaca call per candidate (~150-300 tickers per daily run after pre-filter on Stage 2 perfect + Q≥75). Use the multi-symbol bars endpoint, batch 100 at a time → 2-3 API calls, ~5-10s added runtime. Acceptable.

**Pre-filter:** only call Alpaca for rows where `dist_from_52w_high < -12 AND Stage 2 perfect AND Q >= 75 AND ATR <= 7 AND SMA20% > 0 AND SMA50% > 0 AND SMA200% > 0`. Cuts API load to <50 tickers/day in practice.

### 4. Base Building visibility
**File:** `finviz_agent.py` → wherever the BB Slack block is built (grep `Base Building` or `🏗`).
- Slack: top 5 → top 10.
- HTML gallery: uncap (currently top 5 in `<details>`).
**Why:** Q=78 (RKLB-class) gets ranked out of top 5 on busy market days. Top 10 + full HTML gallery costs nothing and the user can scroll.

## Watchlist integration
- 🌀 HTF Base Reclaim hits auto-add to watchlist at `priority=focus` with `source=htf_base_reclaim_auto` (parallel to `screener_auto`, `breakout_auto`, `hidden_growth_auto`, `rs_leader_auto`).
- Reactivate aged-out entries.

## Tests (per CLAUDE.md "Write tests for new helpers" rule)
- `tests/test_swing_pivot.py`:
  - Returns `None` when Alpaca returns empty bars.
  - Computes correct swing high excluding last 5 days.
  - Handles hyphen-tickers (BF-B) — skip cleanly, no exception.
- `tests/test_htf_base_reclaim_predicate.py`:
  - RKLB Apr 16 fixture row passes (mock swing_pivot returning -11%).
  - Same row with `dist_from_swing_high = -15%` fails.
  - Row with Q=70 fails Q gate.
  - Row already in `Fresh Breakout` set is excluded.
- Update existing RTE / FB tests for the gate softening.

## Verification
1. `python -m unittest discover -s tests -t .` — all pass.
2. `python3 finviz_agent.py --dry-run` (or workflow_dispatch) — verify RKLB-class candidates surface.
3. Re-run retro: `python3 utils/retro_missed_bursts.py` — extend `classify_exclusion()` with `htf_base_reclaim` predicate, verify it catches RKLB Apr 16 + at least 1 of MTSI/RMBS/IHRT.
4. Run `daily-finviz.yml` workflow once after merge, watch logs and Slack output.

## Out of scope (deferred)
- Earnings-gap prediction (RKLB May 8 +34% gap). Not a daily-setup pattern.
- Lowering Q<70 threshold. By design — see retro.
- Lowering ATR>8 threshold. By design — see retro.
- TradingView MCP for chart pattern reading (per project roadmap, pending Mac Mini).

## Files touched
- `finviz_agent.py` — predicates + Slack/HTML rendering
- `agents/utils/swing_pivot.py` — new
- `tests/test_swing_pivot.py` — new
- `tests/test_htf_base_reclaim_predicate.py` — new
- `tests/test_finviz_agent.py` — update existing RTE/FB cases for softened gates
- `utils/retro_missed_bursts.py` — add `htf_base_reclaim` to classify_exclusion
- `CLAUDE.md` — document new block + watchlist source
- `SYSTEM_DOCS.md` — same

## Decisions needed before execution
1. Confirm RTE `dist <= -12` (not `-15`).
2. Confirm FB exception clause `Q ≥ 80 AND ATR ≤ 6 AND RVol ≥ 1.0`.
3. Confirm 🌀 HTF Base Reclaim swing window = 90d, exclude-last = 5d. (Alternatives: 60d/3d, 120d/10d.)
4. Confirm `dist_from_swing_high >= -10` is the right tightness gate. (Alternatives: -8 stricter, -12 looser.)
5. Confirm BB cap top 10 (not top 8 or uncapped in Slack too).
