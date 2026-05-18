# Phase 2 Spec — RS Rating (computed metric, IBD-style)

**Status:** spec approved 2026-05-06 — execution deferred until Phase 1 (RS Leader signal) ships and is verified
**Goal:** quantify "is this stock holding up vs the market" with a percentile-ranked 0-100 score per ticker, computed daily.

## Why

Phase 1 RS Leader uses categorical filters (Stage 2 perfect, Q ≥ 75, peel-safe). It does not actually measure stock-vs-market return. We currently eyeball "DOCN was up while SPY was down in March" — Phase 2 replaces eyeballing with a number.

Concrete payoffs:
- **Tie-breaker** when 2 tickers tie at Q=92 — higher RS wins
- **Display column** in every Slack signal + chart card so user sees relative strength at a glance (`RS 87`)
- **Filter loosener for RS Leader Phase 1** — `(Q ≥ 75 OR RS ≥ 70)` would catch DOCN Apr 2-3 (Q=69 but probably high RS at that point) without lowering Q for everyone
- **Watchlist tier promotion criterion** — `RS ≥ 80` becomes one of the focus → entry-ready bumps
- **Optional Quality Score bonus** — small `+5/+10` for top RS percentile

## Formula

```
rs_raw = 0.50 × (stock_perf_month   - spy_perf_month)
       + 0.50 × (stock_perf_quarter - spy_perf_quarter)

rs_rating = percentile_rank(rs_raw, universe) → integer 0-100
```

**Why this formula:** simplest market-neutral RS — outperformance over 1m and 3m windows, equally weighted. IBD uses 4 windows (3/6/9/12mo) but our Finviz scrape only captures Perf Month + Perf Quarter natively; longer windows would need extra fetches per ticker. Two windows is enough signal for daily decisions.

**Worked example:**
- Stock with PerfMonth +5% during SPY -10%: `rs_raw = 0.5*(5-(-10)) + 0.5*(perf_q delta) = 7.5 + ...`
- Stock with PerfMonth +5% during SPY +5%: `rs_raw = 0.5*(0) + 0.5*(perf_q delta) = ...`
- The first stock ranks higher — exactly the desired behavior.

## Universe choice

| Option | Pros | Cons |
|---|---|---|
| **(A) Filtered screener universe (~130 rows/day)** | Free — uses existing scrape | Biased: everyone in filter is already strong, RS=70 in this set isn't truly "top 30% of market" — RS becomes a Q look-alike |
| **(B) Broad Finviz scrape (~5000-7000 stocks), Ticker + Perf Month + Perf Quarter only** | True market-relative RS | +1 broader scrape per day (~30s added to `daily-finviz.yml`) |

**Recommendation: (B).** The bias problem in (A) is the dealbreaker. Small payload (3 columns, ~100KB) makes the broad scrape cheap.

## Implementation — 6 touch points

| # | Location | Change |
|---|---|---|
| 1 | New file `agents/screener/rs_metric.py` | Pure functions: `fetch_spy_perfs() -> tuple[float, float]`, `fetch_universe_perfs() -> dict[ticker, (perf_m, perf_q)]`, `compute_rs_raw(perf_m, perf_q, spy_pm, spy_pq) -> float`, `percentile_rank(scores: dict[ticker, float]) -> dict[ticker, int]` |
| 2 | `finviz_agent.py` after `compute_quality_score` | Fetch SPY perfs · fetch broad universe perfs · compute `rs_raw` per row · percentile-rank · attach `RS Raw` + `RS Rating` columns to enriched_df |
| 3 | `daily_quality_YYYY-MM-DD.json` writer | Persist `rs_raw` and `rs_rating` per ticker (new fields) |
| 4 | Slack `_format_message` blocks | Append `RS XX` to ticker lines in Ready-to-Enter, Fresh Breakout, RS Leader, Top Picks |
| 5 | `_build_card` chart card builder | Add `RS XX` badge below Q badge |
| 6 | `tests/test_rs_metric.py` (new file) | Cover percentile edge cases, SPY fetch failure (graceful degrade — `rs_rating = None`, no crash), `rs_raw` arithmetic, rank stability |

## Edge cases

- **SPY fetch fails:** `rs_rating = None` for that day; signals fall back to Q-only filtering. Logged at WARNING, not raised. Daily screener still ships.
- **Broad universe fetch fails:** same as SPY — degrade to None across the board.
- **Stock Perf Month/Quarter missing** (older snapshot or new IPO): `rs_rating = None` for that ticker, doesn't break sort. UI shows `RS —`.
- **First-day deployment:** No historical RS. Forward-only metric. Acceptable — RS Rating builds up daily from go-live.
- **Tied rs_raw values:** percentile rank using `scipy.stats.rankdata(method='average')` to handle ties cleanly.

## Approved decisions (locked 2026-05-06)

1. **Universe: (B) broad ~6000-stock scrape.** User: "without failing if we can run on breadth i am fine." Run-time cost not a concern; graceful degradation when broad fetch fails (per edge cases above) is the safety net — `rs_rating = None`, signals fall back to Q-only, no crash.
2. **Quality Score bonus: YES.** Add `+5 if rs_rating ≥ 80, +10 if rs_rating ≥ 90` to `compute_quality_score`. Small enough not to reshape Q distribution; rewards true RS leaders.
3. **RS Leader Phase 1 integration: keep Q ≥ 75 unchanged.** Do NOT add `OR RS ≥ 70` clause. Keep Phase 1 predicate strict and stable. Phase 2 RS Rating will display alongside Q in Slack/cards but won't gate the Phase 1 trigger.

## Phasing

- **This window (Phase 1):** RS Leader Part A + B ships standalone, no Phase 2 dependency. The signal's existing categorical filters are sufficient to catch DOCN at Apr 6.
- **Next window (Phase 2):** RS metric module + broad scrape + integration into all signals + revisit Phase 1 thresholds with RS as additional gate.

## Out of scope for Phase 2

- Multi-window IBD-exact formula (4 windows). Two-window simplification is enough.
- Historical backfill of RS. Forward-only.
- Sector-relative RS (stock vs sector ETF). Possible Phase 3.
- RS-weighted portfolio sizing. Possible Phase 3.

## Execution checklist (when Phase 2 starts)

- [x] Decisions 1-3 resolved (see "Approved decisions" above)
- [ ] `agents/screener/rs_metric.py` with 4 pure functions (`fetch_spy_perfs`, `fetch_universe_perfs`, `compute_rs_raw`, `percentile_rank`)
- [ ] Broad universe fetch added to `daily-finviz.yml` pipeline
- [ ] `enriched_df` carries `RS Raw` + `RS Rating` columns
- [ ] `compute_quality_score` adds RS bonus: `+5 if rs_rating ≥ 80, +10 if rs_rating ≥ 90`
- [ ] `daily_quality_YYYY-MM-DD.json` schema bumped (additive — backwards compatible)
- [ ] Slack blocks display `RS XX` next to ticker (Ready-to-Enter, Fresh Breakout, RS Leader, Top Picks)
- [ ] Chart cards show `RS XX` badge
- [ ] Unit tests covering all edge cases (SPY fail, universe fail, missing perfs, ties, percentile correctness, Q bonus arithmetic)
- [ ] Update CLAUDE.md and SYSTEM_DOCS.md (new metric, new files, new schema fields, Q bonus change)
- [ ] Run full unittest suite
- [ ] Run `daily-finviz.yml` and verify RS column populates + graceful degrade test
- [ ] Commit + push

**Phase 1 predicate is NOT modified by Phase 2** — keeps `Q ≥ 75` only, per decision 3.
