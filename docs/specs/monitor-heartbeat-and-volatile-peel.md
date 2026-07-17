# Spec: Monitor Heartbeat + Volatile Name Peel Fix

## Problem

SNDK ran +18.4%, never peeled, then round-tripped to +1.2% because:
1. **T1 at +20% is too high for 8.5% ATR names** — they swing 8% in a day, so +18% peak → reversal before T1 ever fires. The system locked in zero on a name that was up $5k.
2. **GitHub Actions silently dropped all Friday 6/27 cron jobs** — no position-book, no position-critical. Price crashed through stop with no one watching. Zero alerting that the monitor was offline.

---

## Fix 1: ATR-Tiered T1/T2 Targets

Currently T1 = +20%, T2 = +40% for ALL names regardless of volatility. A 2% ATR name and an 8% ATR name get the same targets. That's wrong — an 8% ATR name swinging +18% is the equivalent of a 2% ATR name at +5%. It should peel earlier.

### New T1/T2 by ATR tier:

| ATR% | T1 (sell half) | T2 (trail tight) | Rationale |
|------|----------------|-------------------|-----------|
| ≤ 3% | +20% | +40% | Low-vol, let it run (current behavior) |
| 3–5% | +15% | +30% | Mid-vol, peel sooner |
| 5–8% | +12% | +25% | High-vol, SNDK class — +12% is a real move |
| > 8% | +10% | +20% | Extreme vol, take what the market gives |

### Implementation

**File:** `agents/trading/alpaca_executor.py` — where T1/T2 are set at entry time.

Currently (line ~990 area):
```python
target1 = round(entry_price * 1.20, 2)
target2 = round(entry_price * 1.40, 2)
```

New:
```python
def compute_targets(entry_price: float, atr_pct: float) -> tuple[float, float]:
    """ATR-tiered T1/T2 targets. Volatile names peel sooner."""
    if atr_pct > 8:
        t1_mult, t2_mult = 1.10, 1.20
    elif atr_pct > 5:
        t1_mult, t2_mult = 1.12, 1.25
    elif atr_pct > 3:
        t1_mult, t2_mult = 1.15, 1.30
    else:
        t1_mult, t2_mult = 1.20, 1.40
    return round(entry_price * t1_mult, 2), round(entry_price * t2_mult, 2)
```

**File:** `agents/trading/alpaca_monitor.py` — where T1/T2 events fire. No change needed — it already checks `current_price >= target1`. The targets themselves just get set tighter at entry.

**Migration for existing positions:** On next monitor run, if `target1` in stops file was set at the old +20% and `atr_pct > 5`, recalculate. One-shot migration block at top of monitor loop:
```python
# Migrate legacy +20% targets for high-vol names
if atr_pct > 5 and not stop_info.get("t1_peeled"):
    new_t1, new_t2 = compute_targets(entry_price, atr_pct)
    if abs(stop_info["target1"] - entry_price * 1.20) < 1.0:  # still at legacy +20%
        stop_info["target1"] = new_t1
        stop_info["target2"] = new_t2
        log.info("%s: migrated targets to ATR-tiered T1=$%.2f T2=$%.2f (ATR %.1f%%)",
                 ticker, new_t1, new_t2, atr_pct)
```

**SNDK retroactive:** entry $1960.51, ATR 8.46% → new T1 = $1960.51 × 1.10 = **$2156.56** (would have hit at peak $2320 → peeled half → locked profit). New T2 = $2352.61 (same as old T1, still pending). The +18% peak blows past the new +10% T1 easily.

---

## Fix 2: Monitor Heartbeat Alert

When GitHub Actions silently skips scheduled runs, we need to know within 1 hour — not discover it on Sunday looking at a round-tripped position.

### Design

**New workflow: `.github/workflows/monitor-heartbeat.yml`**

Runs every 90 minutes during market hours. Checks whether `position-critical.yml` has run in the last 2 hours. If not → Slack alert to `#positions`.

```yaml
name: Monitor Heartbeat
on:
  schedule:
    - cron: '0 15,17,19,21 * * 1-5'  # 4 checks during market hours
  workflow_dispatch:

jobs:
  heartbeat:
    runs-on: ubuntu-latest
    steps:
      - name: Check last critical run
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          SLACK_WEBHOOK_POSITIONS: ${{ secrets.SLACK_WEBHOOK_POSITIONS }}
        run: |
          # Get last successful position-critical run
          last_run=$(gh run list --workflow=position-critical.yml --status=completed --limit=1 --json updatedAt --jq '.[0].updatedAt' -R AnanthSrinivasan/finviz-screener-agent)
          
          if [ -z "$last_run" ]; then
            echo "No runs found at all"
            exit 0
          fi
          
          # Compare to now (seconds since epoch)
          last_epoch=$(date -d "$last_run" +%s 2>/dev/null || date -j -f "%Y-%m-%dT%H:%M:%SZ" "$last_run" +%s)
          now_epoch=$(date +%s)
          diff_minutes=$(( (now_epoch - last_epoch) / 60 ))
          
          echo "Last critical run: $last_run ($diff_minutes min ago)"
          
          if [ $diff_minutes -gt 120 ]; then
            curl -s -X POST "$SLACK_WEBHOOK_POSITIONS" \
              -H 'Content-Type: application/json' \
              -d "{\"text\":\":rotating_light: *MONITOR OFFLINE* — position-critical hasn't run in ${diff_minutes} min. GitHub Actions may be dropping crons. Manual dispatch: https://github.com/AnanthSrinivasan/finviz-screener-agent/actions/workflows/position-critical.yml\"}"
            echo "ALERT SENT"
          fi
```

Also checks `position-book.yml` with a 3-hour threshold (it runs 3x daily, not every 30 min).

### Why not a Lambda/external monitor?

Overkill. If GitHub Actions is down enough to skip the heartbeat too, you'll notice from zero Slack messages period. The heartbeat just catches the common case: one workflow's cron gets deprioritized while others still run (exactly what happened 6/27 — the weekly ran fine, monitors didn't).

---

## Fix 3: Fallback — Position-Critical Also Runs Inside Position-Book

Currently position-critical and position-book are separate workflows. If critical's cron gets skipped, the 30-min monitoring dies but the 3x-daily book still runs (different schedule, different cron line, often survives). Add a quick stop-check inside the book workflow so that even on a catastrophic cron-skip day, the 3x book catches blown stops.

**File:** `position-book.yml` — already runs `alpaca_monitor.py` with `BOOK_RUN=1`. The monitor already checks stops during book runs. Confirm this is true:

```python
# In alpaca_monitor.py — stop check runs regardless of BOOK_RUN
# (BOOK_RUN only controls whether the consolidated table posts to Slack)
```

If the stop-check is gated behind `not BOOK_RUN` — remove that gate. Stop exits must fire on ANY run.

---

## Test Plan

1. Unit test `compute_targets()` — verify tiers produce correct multipliers at boundaries (3.0, 5.0, 8.0 ATR%).
2. Unit test migration logic — mock a stops dict with legacy +20% T1 on ATR 8% name, verify it migrates.
3. Integration: manually dispatch position-book, verify SNDK target migrates in logs.
4. Heartbeat: manually dispatch monitor-heartbeat, verify it checks and doesn't false-alarm.
5. Simulate heartbeat alert: stop position-critical for 3 hours, dispatch heartbeat, verify Slack alert fires.

---

## Summary

| Fix | What | Prevents |
|-----|------|----------|
| ATR-tiered T1/T2 | High-vol names peel at +10-12% instead of +20% | SNDK round-trip ($5k → $0) |
| Heartbeat workflow | Alerts when monitor is offline >2 hours | Silent Friday 6/27 cron skip |
| Book fallback | Stop-check runs in book workflow too | Blown stops during critical-only outage |
