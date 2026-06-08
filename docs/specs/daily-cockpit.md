# Daily Cockpit (`daily.html`) — Spec

**Status:** APPROVED 2026-06-06 — building. Decisions: (1) live SnapTrade w/ positions.json fallback; (2) all 6 blocks in v1; (3) HTML-only now, Slack overhaul deferred to its own spec (daily Slack is also a firehose — same decision-first fix to follow).
**Date:** 2026-06-06
**Author:** Claude (fund-manager framing, per user request)

## Problem

The current daily output is a chart *firehose* — `finviz_chart_grid_*.html` with N
signal blocks (Ready-to-Enter, Fresh Breakout, HTF-BR, Episodic Pivot, …). It shows
what *exists*, not what to *do*. For this user specifically — whose documented failure
modes are (1) round-tripping winners, (2) hold-in-hope on losers, (3) over-trading weak
tapes — more ideas = more temptation. The firehose actively works against him.

Live state at spec time (2026-06-06): sizing mode `reduced` (2 consecutive losses),
record 16W/26L (38% hit rate — EC only works if winners are big), ETF regime
`blow-off-risk`, market EXTENDED, user just went flat in fear. positions.json still
shows IREN open (stale rules-state vs reality).

## Design principle

One morning pane. Decision-first, **discipline-enforcing**. Every block answers one
question and forces one action. Must make "do nothing today" a *visible, validated win*
— not a blank to fill. Light theme + plain-English (standing user rules:
[[feedback_light_theme]], [[feedback_plain_english_dashboards]]).

Differentiator vs generic mindset content (e.g. Julian Komar infographics, used only as
structural validation): every abstract principle is **bound to a live number and a
specific leak of this user**. "Manage risk" → his IREN verdict + $ leak. "Be patient" →
his `reduced` size + today's 0-qualified count. "Kill ego" → his 38% win-rate shown
honestly.

## Blocks (top→bottom = morning order)

| # | Block | Question | Data source | Forces / Guards |
|---|-------|----------|-------------|------|
| 0 | Discipline banner (sticky) | What headspace am I in? | trading_state.json | sizing mode · streak · drawdown vs $100k/$150k goal. Guards ego/over-trading. |
| 1 | 🚦 THE GATE | Can I add risk today? | market_monitor_*.json + etf_rotation.json | state + max action (Full/Half/NO NEW ENTRIES) + position cap. Guards over-trading. |
| 2 | 📓 THE BOOK | What do I do with what I hold? | positions.json OR live SnapTrade | per-position verdict (CUT/SCALE½/TRAIL/HOLD) via `verdict_for`. Scale-due + cut-due made loud with $ at stake. Guards round-trip + hold-in-hope. **Heart of the page.** |
| 3 | 🎯 QUALIFIED TODAY | Which names clear every gate? | daily_quality / screener CSV | ≤3 fully-qualified RTE, trade-plan cards (trigger · −8% stop · de-levered size · T1/T2). "0 qualify" = a win. Guards over-trading. |
| 4 | 👀 ON DECK | What am I stalking? | watchlist.json | tiers + promote-trigger. Empty today (flushed). |
| 5 | 🗺 LEADERSHIP | Where's money flowing? | etf_rotation.json | strong/weak sectors + regime. |
| 6 | 📊 THE RECORD | Am I improving? | trading_state.json + position_history | win-rate, streak, avg win vs avg loss. Guards self-improvement. |

## Build plan (after approval)

- New `utils/generators/generate_daily_cockpit.py` — pure loaders + `render_*` fns +
  `COCKPIT_CSS`. Reuse `verdict_for` (generate_live_portfolio), market state loader,
  `etf_rotation_summary`. Writes `data/daily.html`.
- Tile on `index.html` ("☀️ Daily Cockpit").
- Wire into `daily-finviz.yml` after screener step.
- Tests: `tests/test_daily_cockpit.py` (per [[feedback_write_tests_for_new_code]]).
- Graceful empty-states: flat book, flushed watchlist, stale market data (recompute
  fresh or show "as of <date>").

## Decisions needed (3)

1. **Book data source** — live SnapTrade pull per render (truthful, slower, needs keys)
   vs positions.json (instant, but stale — shows IREN open while user is flat).
   Recommendation: live SnapTrade with positions.json fallback.
2. **v1 scope** — all 6 blocks now, vs ship discipline core (Banner + Gate + Book) first,
   add rest after. Recommendation: discipline core first.
3. **Slack** — also post decision-pane as daily Slack text, vs HTML-only for now.
   Recommendation: HTML-only v1, Slack later.
