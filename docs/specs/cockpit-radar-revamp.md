# Cockpit Radar Revamp — §4 Top-5s + §5 real flows + entry-ready cap

**User decisions (2026-07-12):** watching tier is distracting in the cockpit — show "top 5 focus and top 5 entry ready"; §5 verified data-correct but design-misleading; entry-ready cap 5 approved ("top 5 entry ready"). Mandate: the system's job is putting the RIGHT names on radar and reducing risk toward the $150k goal — execution discipline is the user's half.

## 1. `_update_watchlist` — entry-ready hard cap 5 (audit F6)

- After the focus→entry-ready promotion pass, if active (status != archived) entry-ready rows > 5: keep top 5 by (proximity-to-trigger asc, Q desc) where proximity = `abs(SMA20%)` from today's screener row (unavailable → +99); demote the rest to focus with `demote_reason="entry-ready cap — outranked"`. Never re-bloats (36 actives the day after a manual 48→22 cut proved the need).
- One-time migration not needed — the cap enforces itself on first run.
- Tests: cap enforced · ranking order · archived rows excluded from the count.

## 2. Cockpit §4 "On Deck" → "🎯 Radar" (`utils/generators/generate_daily_cockpit.py`)

Replace the tier dump with two compact tables, **watching tier removed from the cockpit entirely** (it stays in the gallery watchlist section):
- **Top 5 Entry-Ready** and **Top 5 Focus** — both filtered `status != "archived"` (zombie defense, audit F1), ranked by (proximity-to-trigger, Q).
- Row: `TICKER · Q · dist-to-21EMA (SMA20%) · dist-from-high · one-line trigger` (trigger text: "at 21 EMA — buy the hold" when |SMA20%| ≤ 1.5; "pullback to ~$X" when extended; "pivot ~$X" when dist-from-high ≥ −3).
- Respect the gate: when market state blocks entries, grey the tables with the existing watch-only styling.

## 3. Cockpit §5 Leadership — show flow, then structure

Current §5 renders the BASE bucket under a "where money is flowing" headline — structure ≠ flow (2026-07-10 file: XLP RS 22 / fell 21 ranks and XLRE RS 24 / fell 19 were both listed as "next leadership"). Rebuild, all from `etf_rotation.json` (no new data):
1. **💸 Money flowing IN (5d):** top 5 by `rank_delta_5d` (most negative = climbing) with `rs_score ≥ 50` — show `TKR · up N · RS x · bucket`.
2. **💨 Flowing OUT:** worst 3 by rank_delta — one line.
3. **🎯 Bases worth screening:** BASE ∩ (`rs_score ≥ 50` OR rank improving). Annotate each with `RS x · up/down N`. BASE names failing the filter are dropped, not shown.
4. **Regime line + spread sanity:** keep the regime advice, but when (max RS − min RS) ≥ 60 across the universe, append: `…but spread is wide — leaders exist: {top 2 by RS}`. (2026-07-10: advice said "no single-name edge" in the week DAVE ran +41% — the 180d dispersion percentile under-reads spread in a high-rotation year.)

## 4. Tests

`tests/test_daily_cockpit.py` additions: radar tables ranking + archived exclusion + trigger text branches; §5 flow-vs-structure separation (XLP/XLRE-class laggard excluded from "screen these"); spread-sanity line threshold. Cap tests in `tests/test_watchlist_lifecycle.py`.

## 5. Verify

Full suite → push → dispatch `daily-finviz.yml` on a TRADING day (not weekend/holiday — audit F3) → check cockpit HTML renders §4/§5 correctly and the entry-ready count ≤ 5 in logs.
