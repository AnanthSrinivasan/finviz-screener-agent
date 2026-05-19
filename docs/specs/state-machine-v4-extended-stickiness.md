# State Machine v4 — EXTENDED Stickiness + TREND-FOLLOW Guard + DANGER Widening

**Date:** 2026-05-19
**Status:** approved, pending implementation
**Files touched:** `agents/market/market_monitor.py`, `data/trading_state.json` schema, `tests/test_market_monitor.py`, `CLAUDE.md`, `SYSTEM_DOCS.md`

## Problem

State machine v3 (TREND-FOLLOW addition, May 2026) fixed the Apr 24–May 4 missed-rally bug but introduced three new structural bugs that surfaced 2026-05-15.

### Observed bad behavior

State history pulled from `data/market_monitor_2026-05-*.json`:

| Date  | State        | SPY    | up4 | dn4 | 5d   | SPY ATR mult | %above50 | QQQ ATR mult |
|-------|--------------|--------|-----|-----|------|--------------|----------|--------------|
| 05-11 | RED          | 739.30 | 290 | 359 | 1.31 | —            | —        | —            |
| 05-12 | RED          | 738.18 | 132 | 275 | 1.03 | —            | —        | —            |
| 05-13 | RED          | 742.31 | 235 | 278 | 0.78 | —            | —        | —            |
| 05-14 | RED          | 748.17 | 252 | 181 | 0.95 | —            | —        | —            |
| 05-15 morning | EXTENDED (Slack) | — | — | — | — | — | — | — |
| 05-15 evening | **TREND-FOLLOW** (persisted) | 739.17 | 110 | **535** | 0.89 | 6.85 | 7.12 | 8.24 |
| 05-18 | TREND-FOLLOW | 738.65 | 236 | 307 | 0.80 | 6.54 | 6.81 | 7.50 |

The April-1 to May-1 parabolic mark-up phase ran for ~4–5 weeks. The state machine should have been EXTENDED for that entire run. Instead it flipped EXTENDED → TREND-FOLLOW in a single day on a -5× breadth distribution day (110 up4, 535 dn4) and re-opened paper executor entries. User intent: no entries should have fired since approximately 05-13.

## Root causes (verified in code)

### Bug 1 — EXTENDED is stateless

`agents/market/market_monitor.py:815` — `extended = is_extended(spy_atr_mult_50, spy_sma50_pct, qqq_atr_mult_50)`. Recomputed every run with no memory. A $9 SPY pullback drops ATR mult under 7 and the EXTENDED tag evaporates. There is no `extended_since_date` analogue to the `last_thrust_date` mechanism at line 923. The April run was a multi-week parabolic move; the machine had no concept of "stay EXTENDED until the chart structure actually breaks."

### Bug 2 — TREND-FOLLOW takes no `prev_state`

`agents/market/market_monitor.py:872` — `is_trend_follow(today_data, fg)`. No guard against firing immediately after EXTENDED. Once Bug 1 evaporates the EXTENDED tag, TREND-FOLLOW's six SPY-position gates all pass (SMA50 still rising, SPY still near 20d high, VIX still calm, participation proxy ≥8%) and entries re-open.

The function also ignores today's breadth entirely. 05-15 had 110 vs 535 (4.86× distribution) — TREND-FOLLOW read it identically to a calm grind-up day.

### Bug 3 — DANGER's 5d gate filters out catastrophic single-day distribution

`agents/market/market_monitor.py:800-802` — requires `dn4 ≥ 500 AND 5d < 0.5`. 05-15 had dn4=535 and 5d=0.89 → DANGER missed. 535 stocks down 4% in one day should be DANGER regardless of the 5d ratio.

## Trading framework — how EXTENDED actually behaves on a real chart

Source: Minervini Stage 2 power-trend / mark-up phase, Qullamaggie continuation framework.

EXTENDED is not "ride one specific MA or die." A Stage 2 index in mark-up phase:

1. Climaxes — rides 5d SMA / 8 EMA, no pullbacks
2. Pulls back to 8 EMA, bounces, resumes new highs — still EXTENDED
3. Deeper pullback to 21 EMA, bounces, resumes new highs — still EXTENDED
4. Wick under 21 EMA that reclaims within 1–2 days — false breakdown, still EXTENDED
5. **3 consecutive closes below 21 EMA with no reclaim** — leadership ended, transition to COOLING
6. **Any close below 50 SMA** — stage shift / trend damage, transition straight to RED

Therefore the exit condition is structural (chart-level), not metric-level. The `is_extended()` ATR-mult check is the *entry* trigger; the *exit* is the 21 EMA / 50 SMA close-below logic.

## Spec — three changes

### Change A — EXTENDED stickiness

#### Entry (unchanged trigger)

EXTENDED trips when `is_extended()` returns True:
- SPY ATR mult from 50MA ≥ 7, OR
- SPY %above 50MA ≥ 8, OR
- QQQ ATR mult from 50MA ≥ 9

On first trip, persist `extended_since_date = today` in `trading_state.json`.

#### Stay condition

While in EXTENDED, stay EXTENDED while BOTH:
- SPY close ≥ SPY 21 EMA (allowing the 1–2 day wick-reclaim case below)
- SPY close > SPY 50 SMA

The `is_extended()` metric is **not** required to remain True during the stay phase. Pulls to 8 EMA / 21 EMA that bounce naturally drop the metric below 7 — that's healthy digestion inside Stage 2 and must not exit EXTENDED.

#### Exits

| Trigger | Next state | Reasoning |
|---|---|---|
| 3 consecutive closes below SPY 21 EMA with no reclaim | **COOLING** | Leadership phase ended; normal digestion underway; entries blocked but not at panic-stop level |
| 1 close below SPY 50 SMA | **RED** | Stage 2 → Stage 3/4; trend damage; no entries |
| 1–2 closes below SPY 21 EMA that reclaim above 21 EMA | **stay EXTENDED** | False breakdown; intraday wick or single-day shakeout; leadership intact |

State counters:
- `days_below_21ema` — increments on close < 21 EMA, **resets to 0** on close ≥ 21 EMA
- Once `days_below_21ema >= 3`, transition to COOLING and clear `extended_since_date` + `days_below_21ema`

#### Re-entry from COOLING/CAUTION

EXTENDED can re-trip from non-bearish states. If state is `COOLING` or `CAUTION` AND:
- `is_extended()` metric returns True, AND
- SPY closes at a new 20-day high

→ EXTENDED. This handles the April-style resumption after a late-March digestion.

Re-entry is NOT allowed from RED/DANGER/BLACKOUT (must come up through THRUST/CAUTION first per the existing directional cycle).

#### Code placement

Modify `classify_market_state` in [agents/market/market_monitor.py:765](agents/market/market_monitor.py#L765). The EXTENDED branch at lines 815-829 becomes:

```python
extended_since_date = trading_state.get("extended_since_date")
days_below_21ema    = trading_state.get("days_below_21ema", 0)
spy_close           = today_data.get("spy_close")
spy_21ema           = today_data.get("spy_21ema")
spy_50sma           = today_data.get("spy_sma50")  # already computed

metric_trip = is_extended(spy_atr_mult_50, spy_sma50_pct, qqq_atr_mult_50)

# Currently in EXTENDED — apply stickiness rules
if prev_state == "EXTENDED":
    below_50 = spy_close is not None and spy_50sma is not None and spy_close < spy_50sma
    if below_50:
        # Hard exit to RED — clear state
        ctx["extended_since_date"] = None
        ctx["days_below_21ema"] = 0
        return "RED", "EXTENDED → RED: SPY closed below 50 SMA (trend damage)", ctx

    below_21 = spy_close is not None and spy_21ema is not None and spy_close < spy_21ema
    new_days_below = days_below_21ema + 1 if below_21 else 0
    if new_days_below >= 3:
        ctx["extended_since_date"] = None
        ctx["days_below_21ema"] = 0
        return "COOLING", "EXTENDED → COOLING: 3 closes below 21 EMA, leadership ended", ctx

    # Stay EXTENDED
    ctx["extended_since_date"] = extended_since_date or date.isoformat()
    ctx["days_below_21ema"] = new_days_below
    msg = build_extended_message(spy_atr_mult_50, spy_sma50_pct, qqq_atr_mult_50, new_days_below)
    return "EXTENDED", msg, ctx

# Re-entry from COOLING/CAUTION on new 20d high + metric trip
if prev_state in ("COOLING", "CAUTION") and metric_trip:
    spy_20d_high = today_data.get("spy_20d_high")
    if spy_close is not None and spy_20d_high is not None and spy_close >= spy_20d_high:
        ctx["extended_since_date"] = date.isoformat()
        ctx["days_below_21ema"] = 0
        return "EXTENDED", "Re-entered EXTENDED: new 20d high + parabolic metrics", ctx

# Fresh trip from any non-EXTENDED state
if metric_trip and prev_state not in ("RED", "DANGER", "BLACKOUT"):
    ctx["extended_since_date"] = date.isoformat()
    ctx["days_below_21ema"] = 0
    return "EXTENDED", build_extended_message(...), ctx
```

### Change B — TREND-FOLLOW prev_state guard + breadth sanity

`is_trend_follow(today_data, fg, prev_state)` — add `prev_state` parameter.

**Reject when** `prev_state in {"EXTENDED", "RED", "DANGER", "BLACKOUT", "COOLING"}`. TREND-FOLLOW is a *continuation* of an existing uptrend; it must follow GREEN / THRUST / CAUTION / STEADY-UPTREND / TREND-FOLLOW itself. Path out of EXTENDED runs through COOLING → CAUTION → GREEN/THRUST → TREND-FOLLOW.

**Add breadth sanity gate inside `is_trend_follow`:** reject when `dn4 ≥ 2 × up4` (heavy distribution day). 05-15 had 535 vs 110 = 4.86× → rejected. 05-18 had 307 vs 236 = 1.30× → still allowed.

Update the call site at [market_monitor.py:872](agents/market/market_monitor.py#L872) to pass `prev_state`.

### Change C — Widen DANGER

[market_monitor.py:800-802](agents/market/market_monitor.py#L800-L802) becomes:

```python
if (today_data["down_4_today"] >= DANGER_DOWN_THRESHOLD
        and (metrics["ratio_5day"] < 0.5
             or today_data["down_4_today"] >= 3 * today_data["up_4_today"])):
    return "DANGER", "Major breadth deterioration", ctx
```

Preserves the "sustained weakness" path (5d<0.5) AND adds the "catastrophic single-day distribution" path (3× ratio). 05-15: 535 ≥ 500 AND 535 ≥ 3×110 → DANGER fires.

## Data dependencies

`fetch_index_extension()` at [market_monitor.py:401](agents/market/market_monitor.py#L401) gains:
- `spy_21ema` — exponential moving average over 21 daily closes
- `spy_close` — today's SPY close (likely already computed; surface explicitly)
- `spy_20d_high` — max(daily close) over last 20 sessions (for re-entry trigger)

`spy_sma50` is already computed.

`build_daily_record` gains these fields for downstream visibility:
- `extended_since_date` (nullable ISO date)
- `extended_days_active` (int — days since trip)
- `days_below_21ema` (int — running counter)

`trading_state.json` schema additions:
- `extended_since_date` (nullable ISO date)
- `days_below_21ema` (int, default 0)

## Regression tests

New tests in `tests/test_market_monitor.py`:

1. **`test_extended_sticky_through_metric_drop`** — prev=EXTENDED, ATR mult 6.85, %above50 7.12, QQQ 8.24, SPY close > 21 EMA → stays EXTENDED (the literal 05-15 case)
2. **`test_extended_exit_to_cooling_on_3_closes_below_21ema`** — prev=EXTENDED, 3rd consecutive close below 21 EMA, SPY > 50 SMA → COOLING
3. **`test_extended_exit_to_red_on_50sma_break`** — prev=EXTENDED, SPY close < 50 SMA → RED (skips COOLING)
4. **`test_extended_false_breakdown_reclaim`** — prev=EXTENDED, day 1 close < 21 EMA, day 2 close ≥ 21 EMA → counter resets, stays EXTENDED
5. **`test_extended_re_entry_from_cooling`** — prev=COOLING, ATR mult ≥ 7, SPY makes new 20d high → re-trips EXTENDED
6. **`test_trend_follow_blocked_after_extended`** — prev=EXTENDED, all 6 TREND-FOLLOW gates pass → does NOT fire TREND-FOLLOW (Change B guard)
7. **`test_trend_follow_blocked_on_distribution_day`** — prev=GREEN, all 6 gates pass, dn4=535 up4=110 → does NOT fire TREND-FOLLOW (Change B breadth sanity)
8. **`test_danger_catastrophic_distribution`** — dn4=535, up4=110, 5d=0.89 → fires DANGER (Change C)
9. **`test_danger_sustained_weakness`** — dn4=520, up4=300, 5d=0.45 → fires DANGER (original path preserved)

## Replay validation

Run `python scripts/replay_state_machine.py --days 60` after implementation. Expected reclassifications:

- All April dates that ran the parabolic move: should now be EXTENDED contiguously (currently a mix of states depending on day-to-day metric flicker)
- 2026-05-11 onward: depends on where SPY 21 EMA and 50 SMA sat — if SPY closed below 21 EMA on 05-11, that day starts the `days_below_21ema` counter; by 05-13 (3rd close below) EXTENDED would have exited to COOLING. Then RED/COOLING through 05-15.
- 2026-05-15: under v4, prev=COOLING/RED + dn4=535 → fires **DANGER** (Change C). Not TREND-FOLLOW.
- 2026-05-18: prev=DANGER → TREND-FOLLOW blocked (Change B). Likely RED.

## Executor / position monitor impact

No changes required to executor or position_monitor — both already gate on `state in {EXTENDED, RED, DANGER, BLACKOUT}` for blocking entries. Sticky EXTENDED simply means those gates stay engaged longer, which is the intended behavior.

## Paper trade audit (separate follow-up)

Once Change A is implemented and replayed, audit `data/paper_stops.json` for every BUY since 2026-05-13. Any entry that fired while replay says state should have been EXTENDED/DANGER is a trade that the broken state machine let through. Report as a separate analysis — not part of this spec's code changes.

## Open questions

None — all resolved.

**Resolved 2026-05-19:** 21 EMA reclaim rule = any single close ≥ 21 EMA resets the counter to 0. Exit fires only on 3 *consecutive* closes below 21 EMA. Confirmed by user.

## Out of scope

- F&G zone-aware state machine work (separate roadmap item)
- TradingView MCP integration
- Replacing `pct_above_50ma` proxy with a true universe-wide %above-50MA computation
