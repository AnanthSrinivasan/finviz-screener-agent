# Spec: Shield Alerts — Position Psychology Guards

**Date:** 2026-05-03  
**Target file:** `position_monitor.py`  
**Schema change:** 2 new fields per position in `positions.json`  
**Also fix:** post-THRUST floor bug in `market_monitor.py`

---

## Context — why these exist

Trades analysis showed a recurring pattern: positions with valid trailing stops were
exited manually due to market-state noise (RED), not because the stop fired.

- **MU:** peak +7.9%, trail never hit, sold at +1.15% during 6-day RED streak
- **PL:** peak +20.3%, trail never hit, sold at +2.31% during RED
- **FLY:** peak +32.3%, trail floor ~$41, manually sold at $35.13 (through the floor) → +32% → 0%

AAOI is the counter-example: peak +53%, gave back ~6% from peak, held above stop at $172, 
currently +47%. Correct behavior — let the trail work.

RED state = entry gate closed. It is NOT an exit trigger. Stops are the exit trigger.

---

## Shield 2 — "Trail intact, RED is noise"

### Trigger conditions (every position monitor run)
- `market_state ∈ {RED, DANGER}`
- `position.status == "active"`
- `position.peak_gain_pct >= 5.0`
- `position.current_price > position.stop_price`  ← trail NOT hit

### New schema field
```json
"red_trail_reminded_date": null   // ISO date string, e.g. "2026-05-03"
```

### Dedup logic
Fire once per RED *episode*:
- If `red_trail_reminded_date` is set AND the market was RED on that date AND is still RED today → skip (already reminded this episode)
- If `red_trail_reminded_date` is null OR market recovered (CAUTION/GREEN/THRUST) since last reminder → fire, set `red_trail_reminded_date = today`
- Reset `red_trail_reminded_date = null` whenever market_state exits RED/DANGER

Simplest implementation: in position monitor, after resolving market_state, loop open
positions and clear `red_trail_reminded_date` when state is not RED/DANGER. Then check
and set on RED/DANGER runs.

### Slack message (one block per qualifying position, sent after main position summary)
```
🛡 TRAIL INTACT — {ticker} at ${current:.2f} (+{current_gain:.1f}%, peak +{peak:.1f}%)
Stop: ${stop:.2f}  |  RED = no new entries, NOT an exit signal.
Trail is your only trigger. Respect it.
```

---

## Shield 3 — "Don't sell in the middle of a winner"

### Trigger conditions (every position monitor run)
- `position.target1_hit == True`
- `position.current_price > position.stop_price`  ← still above trail
- `position.current_price < position.highest_price_seen * 0.92`  ← >8% off peak (decay zone)

### New schema field
```json
"last_t1_protection_pct": null   // float, peak drawdown % at last fire, e.g. 9.5
```

### Dedup logic
- Compute `pullback_pct = (highest_price_seen - current_price) / highest_price_seen * 100`
- Fire if `last_t1_protection_pct` is null (first time)
- Re-fire if `pullback_pct > last_t1_protection_pct + 5.0` (deepened by 5pp since last alert)
- Update `last_t1_protection_pct = pullback_pct` on each fire
- Reset `last_t1_protection_pct = null` when `pullback_pct < 5.0` (position recovered)

### AAOI validation
- Peak $191.63, current ~$183, pullback = 4.5% → below 8% threshold → **does NOT fire** ✓
- Would fire if AAOI pulls back to ~$176 (8% from peak), stop is at $172 → 4pt warning zone ✓

### FLY retro-validation
- Peak $45.56, 8% threshold = $41.92, stop ~$41 → fires at ~$42 with message ✓
- Would have fired before the $35.13 panic exit ✓

### Slack message
```
🔔 T1 LOCKED — {ticker} at ${current:.2f} (–{pullback:.1f}% from peak ${peak:.2f})
Trail floor: ${stop:.2f}  |  You're above the stop — let it work.
Selling between T1 and trail = selling in the middle of a winner.
```

---

## Shield 4 — Vol regime context (know which signal to watch)

### Problem
Layer 1b already picks the right exit signal per vol tier (21 EMA / 8 EMA / 10% trail).
But the alert only fires **after** the breach, post-close at 22:00 UTC. Nothing tells
you at entry — or in the daily summary — which signal applies to each position.

FLY root cause: ATR 10.38% → correct signal was 10% trail from peak (~$41).
User was watching 21 EMA (~$35). Six dollars of difference. No alert warned of the mismatch.

### Vol tiers (existing, from `_ma_trail_signal_for_atr`)
```
ATR% ≤ 5%   → LOW-VOL   → 21 EMA (regime-adaptive: 1 or 2 closes)
ATR% 5–8%   → MID-VOL   → 8 EMA (1 close)
ATR% > 8%   → HIGH-VOL  → 10% trail from highest_price_seen (MA can't keep up)
```

### Fix 1 — Vol tier label in daily position summary

In the per-position Slack block, add one line showing the tier and active signal level:

```
LOW-VOL  (ATR 3.2%) | exit signal: 2 closes below 21 EMA
MID-VOL  (ATR 6.1%) | exit signal: 1 close below 8 EMA
HIGH-VOL (ATR 10.4%) | exit signal: 10% trail from peak = ${trail_price:.2f}
```

For HIGH-VOL, compute and show the dollar level: `highest_price_seen × 0.90`.
This means the number is live — it updates every run as `highest_price_seen` ratchets up.

### Fix 2 — One-time entry context alert

When position monitor detects a **new** ticker in SnapTrade (not previously in positions.json),
fire a one-time Slack alert:

```
📋 NEW POSITION — {ticker} @ ${entry:.2f}
ATR: {atr:.1f}% → {tier} tier
Exit signal: {signal_description}
{if HIGH-VOL: "10% trail fires at ${highest×0.90:.2f} once peak established — NOT 21 EMA"}
{if MID-VOL:  "Watch 8 EMA — 1 close below is the signal"}
{if LOW-VOL:  "Watch 21 EMA — {1 or 2} closes below in {regime} market"}
```

Dedup: fire only once. Track via `vol_regime_alerted: true` field on the position.

### New schema field
```json
"vol_regime_alerted": false   // bool, set true after one-time entry alert fires
```

### FLY retro-validation
- Entry detected → one-time alert fires: "HIGH-VOL (ATR 10.4%) — exit signal is 10% trail, NOT 21 EMA"
- Daily summary shows: "HIGH-VOL | 10% trail from peak = $41.40" (live, ratcheting)
- User knows at all times: watch $41, not the EMA line

---

## Bug fix: post-THRUST floor not activating

### Observed failure
- Apr 30: THRUST fired (534 stocks up 4%+)
- May 1: state flipped to RED (F&G=66.6, normal range)
- `post_thrust_floor_active` never written to `trading_state.json` on Apr 30
- Result: THRUST → RED flip in 1 day, exactly what the confidence layer was supposed to prevent

### Expected behavior (from CLAUDE.md)
After any THRUST day, minimum state = CAUTION for 3 calendar days.
DANGER still bypasses. `post_thrust_floor_active` written to daily record + `trading_state.json`.

### Fix location
`market_monitor.py` — the THRUST branch must:
1. Write `post_thrust_floor_active = true` + `post_thrust_floor_until = <date+3days>` to `trading_state.json`
2. On subsequent runs, check `post_thrust_floor_until` before allowing state < CAUTION
3. Log: `INFO Post-THRUST floor active until {date} — state floored at CAUTION`

Check: `trading_state.json` after next THRUST day should contain `post_thrust_floor_active: true`.
Verify: run market monitor workflow day after THRUST and confirm log shows floor.

---

## Implementation order

1. Fix post-THRUST floor bug in `market_monitor.py`
2. Add 3 new fields to positions.json schema (backward-compatible: null/false default)
3. Implement Shield 4 vol tier label in daily position summary (`position_monitor.py`)
4. Implement Shield 4 one-time entry context alert (`position_monitor.py`)
5. Implement Shield 2 (`position_monitor.py`)
6. Implement Shield 3 (`position_monitor.py`)
7. Run `python -m unittest discover -s tests -t .` — add tests for all shield trigger logic
8. Trigger `position-monitor.yml` workflow_dispatch and verify Slack output

Shield 4 first — it's the lowest risk change (additive only) and gives immediate value
on existing open positions (AAOI, GLW, FIGS, INDV, CRWV).

## New schema fields (all backward-compatible)
```json
"red_trail_reminded_date": null    // Shield 2 — ISO date, reset on market recovery
"last_t1_protection_pct": null     // Shield 3 — float, pullback % at last alert
"vol_regime_alerted": false        // Shield 4 — bool, one-time entry context fired
```

## Files changed
- `market_monitor.py` — post-THRUST floor fix
- `agents/trading/position_monitor.py` — Shield 2 + 3 + 4
- `data/positions.json` — 3 new fields per position
- `tests/` — unit tests for shield trigger conditions
- `CLAUDE.md` + `SYSTEM_DOCS.md` — document shields + bug fix
