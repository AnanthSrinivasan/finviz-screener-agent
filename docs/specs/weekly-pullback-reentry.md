# Spec — Weekly Pullback Re-entry Table (21 EMA lane)

**Date:** 2026-05-09
**Status:** Draft, awaiting approval
**Trigger:** User reading [`finviz_weekly_2026-05-09.html`](https://ananthsrinivasan.github.io/finviz-screener-agent/data/finviz_weekly_2026-05-09.html) — "Recurring Names — signal score > 138 (30 names)" is informative but not actionable. Top picks include FLEX-class ATR≈10 names (too volatile to act on). User's gripe: *"we need someway where we can take new risks. all these names are 10x atr value crossed. may be good to see if they retrace to 21ema then we can act."*

## Read first
- `finviz_weekly_agent.py` — current weekly report flow, recurring-names table generation, HTML template.
- `data/finviz_weekly_persistence_2026-05-09.csv` — the 30-name source list.
- `agents/trading/rules.py` — existing 21 EMA logic (used in MA-trail). Reuse the EMA helper.
- `agents/trading/position_monitor.py` — `fetch_alpaca_daily_bars` (re-use, don't duplicate).
- `data/peel_calibration.json` — per-ticker peel thresholds (re-use the same gate the executor uses).
- Memory: `feedback_always_apply_peel_filter.md` — peel-warn gate is mandatory before listing entry candidates.
- Memory: `user_chart_terminology.md` — 21 EMA, not "20 SMA". 50 SMA, not "50 EMA".

## Goal
Add ONE new actionable table to the weekly report. Filter the 30+ recurring high-signal names down to the 3–5 sitting at the 21 EMA pullback zone — same place Qullamaggie buys. Everything outside the entry zone is labeled so the user can read it as a "ready / waiting / extended" status board, not a scrollwall of names.

**Out of scope (this spec):** 50 SMA pullback lane. User explicitly deferred — *"50sma has a chance of both ways so follow up is fine."* Will add as a separate spec if the 21 EMA lane proves useful.

## Output — three sections

### 🎯 Re-entry Setup — at 21 EMA (actionable)
Top 5 from the 30 recurring names where price is at 21 EMA pullback. **These are the buy candidates.** One line per row:

```
🎯 TTMI    $48.21  21EMA $47.93   gap +0.6%   Q80  RS89  ATR 7.0   peel-safe
🎯 JBL     $164.07 21EMA $163.18  gap +0.5%   Q89  RS57  ATR 4.0   peel-safe
🎯 PWR     $311.22 21EMA $309.40  gap +0.6%   Q101 RS60  ATR 3.4   peel-safe
```

### ⏳ Watching — pulled back, not yet at 21 EMA (radar)
Names sitting between 1.5–4% above 21 EMA. Will be actionable on a deeper pullback. Display 5 rows max.

### 🚫 Extended — past peel-warn (no action)
Names that triggered the persistence list but are already past their per-ticker peel-warn calibration. **Display 5 rows max** with peel multiple shown so the user understands *why* this is informational only. Replaces the current "Top 5" weekly callout when those happen to be extended (the FLEX-ATR-10 class user complained about).

If a name from the 30 is in none of the above (e.g. above 4% from 21 EMA but below peel-warn), it goes into a fourth tail bucket: `🟡 Mid-flight` — collapsed `<details>` with up to 10 rows. Doesn't clutter the main view but is reachable.

## Filters / classifier

For each ticker in the 30-name persistence list, fetch last 30 daily bars from Alpaca and compute 21 EMA:

```python
def classify_pullback_setup(price, ema21, atr_pct, sma50_pct, peel_warn,
                            q, rs, atr_pct_v, dist_from_high) -> str:
    # Hard quality bar (matches user's "great names" definition)
    if q < 80 or rs < 70 or atr_pct_v > 6 or dist_from_high > 0:
        return "skip"  # not in any bucket — drops out of the actionable view
    if dist_from_high < -12:
        return "skip"  # too deep — that's HTF Base Reclaim's territory

    # Peel-warn check — uses the same gate as alpaca_executor
    peel_mult = sma50_pct / atr_pct_v if atr_pct_v > 0 else 0
    if peel_mult > peel_warn:
        return "extended"

    # Distance from 21 EMA
    gap_pct = (price - ema21) / ema21 * 100
    if -1.5 <= gap_pct <= 1.5:
        return "entry_zone"
    if 1.5 < gap_pct <= 4.0:
        return "watching"
    if gap_pct > 4.0:
        return "mid_flight"
    return "below_ema"  # gap_pct < -1.5 — broken below 21 EMA, not actionable
```

Tier labels in HTML: `🎯 Entry zone` (entry_zone), `⏳ Watching` (watching), `🟡 Mid-flight` (mid_flight), `🚫 Extended` (extended). `below_ema` and `skip` rows are dropped.

**Rationale for thresholds:**
- `gap ±1.5%` matches the precision of an actual 21 EMA touch (a daily candle's wick width on most Q≥80 names).
- `gap ≤4%` for "watching" is roughly where another red day puts price into the entry zone.
- ATR ≤ 6 hard cuts the FLEX/ATR-10 class user explicitly named.
- Q ≥ 80, RS ≥ 70 mirrors the bar the user accepted on the existing weekly Top Picks.

## Implementation

### Files
- `agents/utils/pullback_setup.py` (new) — pure functions:
  - `compute_21ema(closes: list) -> float | None` — wraps the existing `_ema` helper from `agents/trading/rules.py:222`. Returns None if <22 bars.
  - `classify_pullback_setup(...)` — see above.
  - `build_pullback_rows(persistence_df, fetch_bars_fn, peel_loader_fn) -> dict[str, list]` — returns `{"entry_zone": [...], "watching": [...], "mid_flight": [...], "extended": [...]}`. Each row: `{ticker, price, ema21, gap_pct, q, rs, atr_pct, peel_mult, peel_warn, sector}`.

- `finviz_weekly_agent.py` — after the existing recurring-names table, call `build_pullback_rows()` and render HTML via a new helper `_render_pullback_section(buckets) -> str`. Slack: a compact `🎯 Re-entry Setup` block (entry_zone only, top 5 lines).

- HTML — light theme palette per memory `feedback_light_theme.md` (white card, #16a34a green for `🎯`, #f59e0b amber for `⏳`/`🟡`, #dc2626 red for `🚫`).

- Tests:
  - `tests/test_pullback_setup.py` — `compute_21ema` correctness on a fixture, `classify_pullback_setup` covers each bucket boundary, peel-warn extended path, ATR>6 skip, Q<80 skip, dist>0 skip.
  - One row-snapshot test that fixed inputs produce a deterministic HTML row.

### Caveats
- **Alpaca rate limits:** 30 tickers × ≤1 req/s = ~30s. Run inside the existing weekly `requests.Session()` reuse. Cap at 35 tickers (slice persistence list), log if any error out — don't block the report.
- **Recurring list size:** the spec assumes 30 names. If the list grows seasonally, the actionable subset stays small (~5 entry_zone, by construction). The "extended" bucket is capped at 5 in render.
- **No new external deps.** Re-use Alpaca + existing `_ema`.

## Verification
1. `python -m unittest discover -s tests -t .` — new tests pass.
2. `gh workflow run weekly-finviz.yml` — verify the Saturday weekly report renders the new section. Check Slack `#weekly-alerts` for the new block.
3. Inspect [`finviz_weekly_2026-05-16.html`](https://ananthsrinivasan.github.io/finviz-screener-agent/) post-run — `entry_zone` count should be 1–5 in normal markets; 0 is acceptable (zero-action week is real signal too).

## Files touched
- `agents/utils/pullback_setup.py` (new)
- `finviz_weekly_agent.py`
- `tests/test_pullback_setup.py` (new)
- `CLAUDE.md` — Weekly Review row gets a "🎯 Re-entry Setup" sub-bullet.
- `SYSTEM_DOCS.md` — Section 3.2 Weekly Review gets the same sub-bullet.

## Open questions (decide before exec)

1. **Slack: post the 🎯 Entry zone block standalone, or only in the HTML?** Recommend standalone Slack block (top-of-message in `#weekly-alerts`). High-signal, ≤5 lines.
2. **What if `entry_zone` is empty?** Render the section with `(0 names sitting at 21 EMA this week — wait for a pullback)` message rather than hide it. Keeps the user oriented.
3. **Day-of-week sensitivity:** weekly runs Saturday. 21 EMA is computed on Friday's close. Confirm that's fine (vs trying to also factor Monday's pre-market for stale-by-Tuesday check). Recommend: ship as Friday-close, revisit if signals stale.
4. **`Q≥80` and `RS≥70` thresholds:** these are the actionable cut. Lower would surface more, higher fewer. Confirm or override.

## Won't ship (deferred)
- 50 SMA pullback lane (user deferred — "follow up is fine").
- Auto-add `entry_zone` rows to watchlist. Watch-only this iteration; user clicks if they want to act.
- Slack DM channel routing (existing `#weekly-alerts` is fine).
