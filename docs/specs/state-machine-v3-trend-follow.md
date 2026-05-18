# State Machine v3 — TREND-FOLLOW (situational rally rider)

**Status:** ready to execute · YOLO mode after spec read
**Branch:** main · commit straight, push, run workflow, verify
**Date:** 2026-05-15

---

## Problem

State machine v2 (just shipped — EXTENDED + STEADY-UPTREND) fixed the *no-chase* problem at the top but did NOT fix the *missed-rally* problem on Apr 24–30 2026.

The root cause is the 5d/10d **breadth ratio** (count of stocks up 4% / down 4%). It is a **thrust-day detector** being mis-used as a **trend detector**:

- Steady grind-up tape (SPY +0.3%/day for 5 days) produces few 4% moves either way → ratio sits ~1.0 → falls through to RED.
- Result: 4 trading days of zero entries during a clearly trending up tape (Apr 24–30 + May 11–14).

5d ratio belongs in a *thrust-strength gauge*, not a state gate. We need a state that reads the actual situation: MA stack, persistence, participation, vol.

## Goal

Replace the 5d-ratio gating with a multi-factor **trend regime detector** so the system rides steady uptrends with full size, blocks chasing at parabolic levels, and only falls to RED when the trend actually breaks.

## New state — `TREND-FOLLOW`

Top-priority bullish state. Fires when ALL of:

| Gate | Definition | Source |
|---|---|---|
| MA stack | SPY price > SMA50 > SMA200 | `spy_sma50_pct > 0` AND `spy_sma200_pct > 0` (already computed) |
| 50MA slope | SMA50 today > SMA50 ten sessions ago | new — `compute_sma50_slope()` |
| Near 20d high | SPY within 3% of trailing 20d high | new — `pct_from_20d_high()` |
| Participation | ≥ 55% of NYSE+NASDAQ active equities (Bonde filter) above SMA50 | new — `pct_above_50ma()` |
| Vol calm | VIX < 25 OR VIX down on the day | new — `fetch_vix_snapshot()` |
| Not EXTENDED | `is_extended()` returns False | existing |

Action: **full size, entries allowed.** `_MARKET_GATE` entry: `(False, 1.0)`.
Behaves like GREEN but driven by trend, not by thrust.

## Updated priority ladder

```
1. BLACKOUT
2. DANGER
3. EXTENDED          (parabolic guardrail — no chase)
4. COOLING           (from GREEN/TREND-FOLLOW, fading)
5. THRUST            (breadth explosion — bonus signal)
6. GREEN             (thrust-day full bull — unchanged)
7. TREND-FOLLOW   ★  (trend-day full bull — NEW)
8. CAUTION           (recovering — unchanged)
9. STEADY-UPTREND    (kept as safety net for trend-follow gate misses)
10. RED              (default)
```

Note priorities 6 and 7 are alternative paths to "full size":
- GREEN is the *thrust-confirmation* path (after a recent THRUST, breadth ratios still strong).
- TREND-FOLLOW is the *trend-persistence* path (no thrust required, but trend structure intact).

Both → `(block=False, size_mul=1.0)` in executor. Both → 10 max positions.

## Demote 5d/10d ratio

- Keep the field in daily JSON for display.
- Remove ratio_5day / ratio_10day from any **gating** decision.
- Slack alert: show 5d ratio as a *thrust strength gauge* (`5d 1.3× · neutral`, `2.4× · strong thrust`), not as part of the state decision.

THRUST still gates entirely on `up_4_today >= 500` (Bonde Very-High signal) — that's correct.

## New data fields (per daily record)

| Field | Source |
|---|---|
| `spy_sma50_slope_10d` | new helper; `sma50_today - sma50_10_sessions_ago` in % terms |
| `spy_pct_from_20d_high` | new helper; `(close - max(highs[-20:])) / max(highs[-20:]) * 100` |
| `pct_above_50ma` | new helper; Alpaca snapshots, `close > sma50` count / universe |
| `vix_close` | new helper; fetch VIX via Alpaca (^VIX or VIXY proxy) or Yahoo |
| `vix_change_pct` | derived |
| `trend_follow_active` | bool — fires when all 6 gates pass |

`pct_above_50ma` is the expensive one (3000+ tickers × need 50d of bars). Cache strategy: use the **same snapshots batch** already fetched in `fetch_breadth_alpaca` and add a single field — for each snapshot, also pull SMA50 from a piggybacked daily bars call. **Cheap path:** approximate using already-fetched `up_25_quarter` (stocks up 25% in a quarter) as a *participation proxy* if a true %above-50MA is too expensive on first cut. **Real path:** add to a new helper that batches 1Day bars limit=50 per 1000-ticker call (3 round trips, ~10s total).

Spec says: ship the **proxy first** (use `up_25_quarter / universe_size >= 0.10` as proxy for healthy participation — Apr 30 was 360/3000 = 12% ✓), then iterate to true %above-50MA in a follow-up.

## Test the Apr 24 → May 14 window

Required backtest output (run `python scripts/replay_state_machine.py --days 60` after wiring v3 in):

| Date | v2 state | v3 expected |
|---|---|---|
| Apr 23 | COOLING | TREND-FOLLOW |
| Apr 24–29 | COOLING | TREND-FOLLOW (every day) |
| Apr 30 | COOLING | THRUST (overlay) inside TREND-FOLLOW (full size) |
| May 1–4 | COOLING | TREND-FOLLOW |
| May 5 | CAUTION | TREND-FOLLOW |
| May 6+ | EXTENDED | EXTENDED (unchanged — priority 3 wins) |

Hard requirement: v3 must NOT flip to TREND-FOLLOW at any local SPY top in the last 60d (i.e. should never co-occur with EXTENDED — priority order enforces this).

## Files to change

1. `agents/market/market_monitor.py`
   - Add `compute_sma50_slope_10d(bars)`, `pct_from_20d_high(bars)`, `fetch_vix_snapshot()`, `compute_participation_proxy(today_data)`.
   - Wire results into `today_data` and `build_daily_record`.
   - Add `is_trend_follow()` predicate.
   - Add `TREND-FOLLOW` branch in `classify_market_state` between GREEN and CAUTION (priority 7).
   - Update message + emoji (`🌊` for TREND-FOLLOW).
   - Update consecutive_weak_days reset list.

2. `agents/trading/alpaca_executor.py`
   - `_MARKET_GATE`: `"TREND-FOLLOW": (False, 1.0)`.
   - `effective_max_positions`: TREND-FOLLOW returns 10.
   - `aggressive` mode overlay: include TREND-FOLLOW in the 1.25× boost set.

3. `agents/trading/position_monitor.py`
   - `state_emoji`: add TREND-FOLLOW.
   - BUY gate: TREND-FOLLOW behaves like GREEN (no block, no sizing note).

4. `CLAUDE.md` + `SYSTEM_DOCS.md`
   - Add TREND-FOLLOW row to the state table.
   - Update the cycle diagram: `RED → THRUST → CAUTION → TREND-FOLLOW ⇌ GREEN → COOLING → EXTENDED → DANGER → RED`.
   - Document the 5d-ratio demotion (gauge only).

5. `tests/test_market_monitor.py`
   - New `TrendFollowTests` class. Required cases:
     - All 6 gates pass → TREND-FOLLOW.
     - 50MA slope negative → not TREND-FOLLOW.
     - Below 20d high by 5% → not TREND-FOLLOW.
     - Participation < 55% (or proxy fails) → not TREND-FOLLOW.
     - VIX > 25 AND VIX up → not TREND-FOLLOW.
     - EXTENDED override beats TREND-FOLLOW even when all gates pass.
     - GREEN takes precedence when thrust-day conditions also satisfied (priority 6 before 7).
     - Apr 30 2026 replay inputs → TREND-FOLLOW (or THRUST overlay if thrust=True).

6. `scripts/replay_state_machine.py`
   - Already pulls SPY/QQQ bars — extend to compute the 3 new bar-derived fields per historical date.
   - Add column for new state + a summary line: "TREND-FOLLOW days: N, would have allowed entries on M days that v2 blocked".

## Acceptance criteria (no shipping until all green)

- [ ] `python -m unittest discover -s tests -t .` — 100% pass, new TrendFollowTests included.
- [ ] `python scripts/replay_state_machine.py --days 60` — Apr 24 → May 4 shows TREND-FOLLOW; May 6 → May 14 still shows EXTENDED.
- [ ] Workflow run on `market_monitor.yml` succeeds and writes `trend_follow_active`, `spy_sma50_slope_10d`, `spy_pct_from_20d_high`, `pct_above_50ma`, `vix_close`, `vix_change_pct` to today's record.
- [ ] Today's live state is **still EXTENDED** (priority 3 wins — sanity check that v3 didn't break the parabolic guardrail).
- [ ] CLAUDE.md + SYSTEM_DOCS both updated.
- [ ] Memory note saved at `~/.claude/projects/.../memory/project_state_machine_v3.md`.

## YOLO execution order

1. Read this spec end-to-end.
2. Read existing v2 implementation (`agents/market/market_monitor.py` — fetch_index_extension, classify_market_state) for style.
3. Implement helpers (SMA50 slope, 20d high distance, participation proxy, VIX) — each gets a unit test.
4. Wire TREND-FOLLOW into classifier; add `is_trend_follow()` predicate.
5. Update executor + position_monitor.
6. Run unit tests. Iterate until green.
7. Run backtest replay. Confirm Apr 24–30 flips to TREND-FOLLOW. If not, debug *the data*, not the gates (likely participation proxy too strict).
8. Update CLAUDE.md + SYSTEM_DOCS.
9. Commit + push + run workflow.
10. Verify today's record contains all new fields and state is still EXTENDED.
11. Save memory note.
12. Final report: backtest table + acceptance criteria checked.

**No mid-task questions.** Pick the smallest sensible fix for any edge case and document in the final report. The spec is the contract.

## Out of scope

- True %above-50MA computation (use proxy first, swap in later).
- VIX 1Day timeframe vs realtime: 1Day daily bar is fine.
- Slack 5d-ratio gauge cosmetics — leave for follow-up.
- TREND-FOLLOW → COOLING transition wiring beyond what the existing COOLING-from-GREEN rule handles.

---

End of spec. Execute.
