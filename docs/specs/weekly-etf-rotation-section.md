# Weekly Review — ETF Sector Setup Section

**Status:** Approved 2026-05-17 — ready to implement
**Predecessor:** [etf-rotation-dashboard.md](etf-rotation-dashboard.md) (daily `data/etf_rotation.html` + `data/etf_rotation.json` shipped 2026-05-17)

## Why

Daily ETF rotation refresh is overkill for human attention — sector rotation moves on weeks, not days. Daily noise will produce bad reactions. The right cadence for *consuming* sector setup state is **weekly**, when the user is already in planning mode (Saturday weekly review).

Weekly review currently has macro snapshot, F&G, crypto, top 5 ranked tickers, watchlist tiers, and 21 EMA pullback re-entry lane. Adds: sector setup state translated into actionable English.

## What goes on the weekly page

New `📊 Sector Setup This Week` section in `finviz_weekly_*.html`, placed **between the macro snapshot and the Top 5 cards**.

### Block 1 — Regime paragraph (templated, deterministic)

Pulled from `data/etf_rotation.json` (`regime` field) + the existing `REGIME_ACTIONS` dict in `agents/sector_rotation.py`. One paragraph of plain English explaining what the regime means and what to do this week.

Template:

> Regime: **{regime}** — {headline}. {sizing} · {entries} · {held}. **What this means for you this week:** {regime_advice_template[regime]}.

Where `regime_advice_template` is a static dict keyed by regime tag:

```python
REGIME_ADVICE = {
    "correlation_phase": "trade the index, not stocks. Single-name edge is fragile this tape — wait for dispersion to widen.",
    "early-rotation":    "leadership is forming. Build watchlist now, take only confirmed RS leaders. Patience pays.",
    "mid-rotation":      "this is the best-entry tape. Trade the leaders, not the laggards. The market is paying you to be selective, not to fish in broken groups.",
    "late-rotation":     "leadership is narrowing. Trim ≥+25% positions, skip extended names, take only fresh RS-rising leaders with peel-warn room.",
    "blow-off-risk":     "parabolic tape. No new entries. Tighten stops, trim aggressively, cash is a position. The next clean trade is on the other side.",
    "bootstrapping":     "regime tag not yet calibrated — use market_state for sizing decisions this week.",
}
```

No Claude API call — fully deterministic.

### Block 2 — Actionable interpretation (top 5 per bucket)

Generated from `data/etf_rotation.json`. Top 5 ETFs per actionable bucket, plain English:

> - 🎯 **XLRE, KRE, XBI, FCG, XLE** in BASE → tight, ready to break out. Start screening constituents this weekend — that's where next leadership likely shows up.
> - 🟦 **UFO, LIT, XOP** pre-breakout → approaching highs with room. Watchlist trigger if these clear pivot on volume.
> - 🚀 **XLK, SMH, TAN** extended — don't chase. Wait for 21 EMA pullback on the names you want in these themes.
> - ❌ **HACK, ITA, GDX, IGV, XLF** broken → if a name from these groups screens well this week, the group is wrong. Skip or wait.

Top 5 per bucket. If bucket has < 5, list what's there. Skip a bucket entirely if empty.

### Block 3 — Compact metrics table

Sortable table, only the **actionable** buckets (BASE / PRE-BREAKOUT / EXTENDED). Columns: Ticker, Name, Bucket, mult50, dist52, range20, ret20, RVol. Reuses the same row HTML pattern as the daily dashboard.

Footer link: `View full daily dashboard → data/etf_rotation.html`

## Slack — `send_weekly_slack`

One new block, **after** the macro/breadth summary and **before** the Top Picks block:

```
📊 *Sector Setup This Week* — regime: {regime}
{regime_paragraph — one sentence, the "what this means for you" line only}

🎯 BASE: `XLRE` `KRE` `XBI` `FCG` `XLE` — screen constituents
🟦 PRE-BREAKOUT: `UFO` `LIT` `XOP` — watch for pivot break
🚀 EXTENDED: `XLK` `SMH` `TAN` — wait for PB, don't chase
❌ BROKEN: `HACK` `ITA` `GDX` — skip names from these groups
```

Each bucket: top 5 tickers max, single line. Skip bucket if empty.

## Files

- New: `agents/utils/etf_rotation_summary.py`
  - `load_etf_rotation(data_dir: str) -> dict | None`
  - `summarize_etf_rotation(rotation: dict, top_n: int = 5) -> dict` — returns `{regime, regime_paragraph, regime_advice, buckets: {BASE: [...], PRE-BREAKOUT: [...], EXTENDED: [...], BROKEN: [...]}, table_rows: [...]}`
  - `REGIME_ADVICE` dict
- Modified: `agents/screener/finviz_weekly_agent.py`
  - `generate_weekly_html()` — accept optional `etf_rotation_summary` param, build `sector_setup_html` block, inject after macro and before Top 5
  - `send_weekly_slack()` — accept same param, build sector-setup Slack block
  - `main()` — call `load_etf_rotation()` + `summarize_etf_rotation()` once, pass to both render and Slack
- New CSS classes in the existing weekly template: `.sector-setup-section`, `.bucket-row`, `.bucket-icon`, `.bucket-tickers`
- Tests: `tests/test_etf_rotation_summary.py` — pure function tests for `summarize_etf_rotation`

## Tests

- `summarize_etf_rotation` with synthetic rotation dict — top 5 per bucket, regime paragraph composed correctly
- Empty/missing rotation file → returns None, weekly render handles gracefully (skips block)
- Each regime tag produces non-empty advice line
- Bucket with < 5 tickers — surface what exists
- Bucket missing entirely (e.g. zero BROKEN ETFs today) — omitted from output

## Schedule + outputs

No new workflow. Weekly agent (`weekly-finviz.yml`, Sat 10:00 UTC) reads the most recent `data/etf_rotation.json` (written daily by `sector-rotation.yml`). Always Friday's snapshot when weekly runs Saturday.

## Out of scope

- Daily Slack alert for ETF bucket transitions (deferred — only if Mon/Thu signal proves noisy)
- Historical ETF setup-state tracking (already in `sector_rotation_history.json`)
- Constituent drill-down per leading ETF (deferred to v2)

## Success criteria

- Weekly HTML renders sector-setup section between macro and Top 5
- Weekly Slack message includes the 5-line sector setup block
- Falls through gracefully when `etf_rotation.json` is missing (skip section, log warning, weekly review still ships)
- All new tests green; full suite green
- Manual `weekly-finviz.yml` run produces Saturday's review with the new section visible
