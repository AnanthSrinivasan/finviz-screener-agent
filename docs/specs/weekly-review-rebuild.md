# Spec — Weekly Review Rebuild (Feature D)

**Approved 2026-06-01.** Decisions: Full rebuild (all 6 sections) · Full trade-plan cards · Kill the 21 EMA lane (fold into shortlist).

## Problem
Current weekly (`finviz_weekly_agent.py`) is backward-looking — it answers "what happened?" (which the daily already does better) instead of "what do I do next week?" (the weekly's actual job). User verdict: "useless / crap / waste." Only INGM (an Emerging-candidate) ever landed.

Root issues found in code:
- **Top 5** ranks by *signal score* = appearance frequency = already-moved/extended names (rear-view).
- **AI Brief** (`generate_weekly_ai_brief`, ~line 1696) = prose essay, no decision in it.
- **21 EMA lane** (`agents/utils/pullback_setup.py` + `_render_pullback_section` ~line 916) is structurally empty ~90% of weeks: scans only the ≤35-name recurring leaderboard, then ANDs Q≥80 · RS≥60 · ATR∈[3,6] · dist∈[-12,0] · peel-safe · price within ±1.5% of 21 EMA on Fri close. 6-way AND on 35 names → empty. THIS is "never proper setups."
- **Emerging candidates** (`select_emerging_candidates` ~line 588) = the ONE forward-looking idea, surfaced INGM, but buried.
- No mention of the user's OWN book/positioning anywhere.

## Design principles (fund-manager + retail swing-trader lens)
A Saturday weekly for real capital has 4 jobs: (1) am I positioned right? regime + MY book risk; (2) what's my shortlist for next week, each with a plan; (3) what do I do with what I hold; (4) what's leadership doing. Current weekly does ~none.

## New section order (decision-density first)
1. **🎛️ Positioning & Book Risk (NEW)** — opens with USER state: market_state + ETF rotation regime + "N positions vs cap M" (🚨 if over). Realized P&L this week (FIFO from `data/position_history.json` — the proven-correct source, NOT trading_state.json), W/L count, biggest leak ("3 losers held past stop cost $X"). Book health counts: green / underwater / past-stop-held.
2. **🎯 Week-Ahead Shortlist (REPLACES Top 5)** — 5–8 names as FULL TRADE-PLAN CARDS: Setup / Trigger / Stop / Size / Invalidation. Sourced from FORWARD funnel (emerging candidates + entry-ready watchlist + new RS leaders), NOT persistence leaderboard. Stop default −8% (from MAE work — winners' median MAE; run MAE first to confirm). Size from regime + ATR. AI-assisted to write Setup/Invalidation prose per name (terse).
3. **📋 Book Weekend Review (NEW)** — per open position: cur% / peak% / dist-to-stop / verdict (✅ hold · 🟢 trail tighter · ⚠️ trim ⅓ · 🚨 cut). Reuse `/pos-review` verdict logic (utils/generators/generate_live_portfolio.py `verdict_for`).
4. **📊 Leadership Map (KEEP+tighten)** — existing ETF rotation Sector Setup block + promote Emerging candidates up here.
5. **🧠 Strategist's Note (DEMOTE AI brief)** — kill essay, replace with MAX 3 bullets: (a) regime insight, (b) best setup + why, (c) the one risk. Caps tokens.
6. **Kill 21 EMA lane** — remove `_render_pullback_section` + the pullback bucket build in main() + Slack pullback block. Fold pullback detection into §2 shortlist. Keep `agents/utils/pullback_setup.py::bucket_one` logic available if shortlist reuses the gap-to-EMA calc, else delete. Update tests.

## Source-of-truth notes
- Realized P&L: FIFO from position_history.json (schema {updated, history:{TKR:[{date,action BUY/SELL,shares,price}]}}). See feedback_pnl_source_of_truth memory.
- Book/positions: fetch_positions() from SnapTrade (.env has keys) + data/positions.json.
- Forward funnel already exists: select_emerging_candidates(), watchlist.json entry-ready rows, rs_leaders.json.
- pos-review verdict logic: verdict_for(gain, atr, s20, stage) in generate_live_portfolio.py.

## Build order (tasks)
1. Run MAE refresh FIRST (utils/analyze/analyze_mae.py — NOTE no `timeout` cmd on macOS) → get the data-driven stop number for shortlist cards. Inputs: data/1099b*.csv.
2. §1 Positioning & Book Risk — helper module `agents/utils/weekly_positioning.py` (pure fns + render html/slack). Tests.
3. §2 Week-Ahead Shortlist — `agents/utils/week_ahead_shortlist.py`: forward-funnel selection + trade-plan card builder (AI-assisted setup notes). Tests.
4. §3 Book Weekend Review — reuse verdict_for; render block. Tests.
5. §4 promote Emerging + keep ETF Sector Setup.
6. §5 rewrite generate_weekly_ai_brief → 3-bullet Strategist's Note (terse prompt).
7. §6 kill 21 EMA lane (remove render + build + slack + dead code) + update/remove pullback tests.
8. Wire all into generate_weekly_html + send_weekly_slack in new order. Full suite green. Run weekly workflow, verify logs + HTML. Update CLAUDE.md + SYSTEM_DOCS.md.

## Constraints
- Light theme only (white cards, #111827 text). No dark bg.
- Plain English: no Δ symbols, no HOT/STABLE/FADING categories (use up N / down N / —).
- Every new pure fn / generator / html builder gets unit tests same commit.
- Run `python -m unittest discover -s tests -t .` before push. Run weekly workflow + verify logs after.
