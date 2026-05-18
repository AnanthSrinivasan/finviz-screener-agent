# ATR-Tiered T1 Trim Discipline

**Status:** Approved 2026-05-10. Ready to execute.
**Author:** session-driven spec.

## Problem

T1 alert (+20% gain) already fires in the system and is in `CRITICAL_EVENT_KINDS`,
but executor (the human) often does not trim half on receiving the alert.
Backtest on last 12 closed positions shows **+19.3pp savings** if half had been
trimmed at T1, but **the savings cluster entirely on high-vol names**
(FLY ATR 8.8%, PL ATR 9.4%). Low-vol names (AMD ATR 4.3%) held the trend
cleanly post-T1 and trimming would have clipped upside.

## Diagnosis

The leak is not the T1 threshold. The leak is:

1. The alert says "consider selling half" — too soft, sounds optional
2. No nag if T1 fires and shares stay unchanged
3. No visibility on how often this leak happens (no counter)
4. Paper book doesn't auto-execute the rule, so the discipline isn't
   even visible in paper P&L
5. The same alert fires whether ATR is 3% (smooth trend) or 10% (whipsaw),
   when the underlying behavioral need is very different

## Solution

Five changes, no threshold change:

### 1. Capture `atr_pct_at_entry` on every position

- Source: existing Finviz snapshot `ATR (14)` already pulled at BUY time
- Live: `position_monitor.py` BUY paths (~lines 873, 1183, 1199)
- Paper: `alpaca_executor.py` `paper_stops.json` schema (~line 776)
- Backfill: one-time script for existing 9 open positions via Finviz snapshot

### 2. ATR tier helper (single source of truth)

In `agents/trading/rules.py`:

```python
def atr_tier(atr_pct: float) -> str:
    if atr_pct <= 5.0:
        return "low"
    if atr_pct <= 8.0:
        return "mid"
    return "high"
```

Boundaries match existing trail-tier code (5/8 cutoffs).

### 3. ATR-tiered T1 alert wording

In `agents/trading/rules.py` T1 event emission:

| Tier | Message |
|---|---|
| low (≤5%)  | `🎯 TICKER T1 HIT $X — informational (low-vol, trail in effect)` |
| mid (5–8%) | `🎯 TICKER T1 HIT $X — TRIM HALF NOW (mid-vol)` |
| high (>8%) | `🚨 TICKER T1 HIT $X — TRIM HALF MANDATORY (high-vol, round-trip risk)` |

Falls back to mid-vol wording if `atr_pct_at_entry` missing on a position
(safe default — fail toward action).

### 4. T1 nag in book post

For mid/high-vol positions where `target1_hit=True` AND shares unchanged
≥3 trading days post-T1:

```
🔁 T1 ROUND-TRIP RISK: TICKER hit T1 N days ago, no trim recorded,
   peak +X%, current +Y% (giving back Zpp)
```

- New field: `target1_hit_date` (set when T1 first fires)
- Stops nagging when shares drop or position closes
- Low-vol skipped — user's discretionary hold is the correct move
- Flat-amber every day (no escalation — escalation desensitizes)

### 5. Paper auto-trim on T1

In `agents/trading/alpaca_executor.py` paper monitor path:

- When T1 fires AND `atr_tier != "low"`:
  - Market-sell `shares // 2`
  - Update `paper_stops.json` (`shares`, `target1_hit=True`, `target1_hit_date`)
  - Raise stop on remaining shares to breakeven (`max(entry, current_stop)`)
- Slack: `[PAPER] 🎯 TICKER T1 — auto-trimmed N shares @ $X, stop -> breakeven`
- Low-vol skipped — paper trails just like live

This lets the user *see the discipline working* in paper P&L without
touching live trades. If paper outperforms over 3 months of base hits,
trust transfers to live.

## What does NOT change

- T1 threshold stays +20%
- T2 logic untouched
- Trail engine (ATR-tiered) untouched
- Stop-loss / breakeven floor logic untouched
- Live monitor never auto-executes — alerts only
- `t1_no_trim_count` rolling counter — deferred (low value vs nag; revisit later)

## Backtest evidence (closed positions, 2026-04 through 2026-05)

| Ticker | ATR% | Tier | Peak% | Actual% | ATR-aware trim outcome | Delta |
|---|---|---|---|---|---|---|
| FLY  | 8.8% | high | +32% | 0.0%   | half trimmed @ +20% | **+10.0pp** ✅ |
| PL   | 9.4% | high | +20% | +2.3%  | half trimmed @ +20% | **+8.8pp** ✅ |
| AMD  | 4.3% | low  | +20% | +19.1% | held to trail        | 0.0pp (correct) |
| CORZ | 6.2% | mid  | +13% | +7.2%  | no T1, no trim       | 0.0pp |
| 8 others | — | — | <20% peak | — | no T1, no trim | 0.0pp |

**Sum delta across all 12 trades: +18.9pp.** Same as universal trim-at-T1
(+19.3pp) minus the 0.5pp AMD clip. ATR-aware captures 98% of edge while
respecting the low-vol hold pattern.

## Task list (execution order)

1. Add `atr_pct_at_entry` capture in live BUY paths
   (`position_monitor.py` lines ~873, 1183, 1199) and paper
   (`alpaca_executor.py` `paper_stops.json` schema ~line 776)
2. Add `atr_tier()` helper in `agents/trading/rules.py`
3. Update T1 event message in `rules.py` to route by tier; safe fallback
   to mid-vol if field missing
4. Add tier badge column in `agents/trading/book_table.py`
5. Add `check_t1_nag()` logic; track `target1_hit_date`; render in book post
6. Add paper auto-trim on T1 in `alpaca_executor.py` paper path with
   breakeven stop raise
7. Unit tests for: `atr_tier()`, message routing, nag day-count
   (low-vol skip), paper auto-trim (mid+high fires, low skips)
8. One-time backfill script: read Finviz snapshots for existing 9 open
   positions, write `atr_pct_at_entry`
9. Update `CLAUDE.md` and `SYSTEM_DOCS.md` — new field schema, ATR-tier
   T1 policy, paper auto-trim behavior

## Open decisions resolved

- ATR boundaries: **5 / 8** (matches existing trail-tier code)
- ATR window: **ATR14** (Finviz snapshot, already in BUY-time scope)
- Nag escalation: **flat amber every day** (no escalation)
- Paper auto-trim breakeven stop raise: **yes** (fully simulate rule as written)
- `t1_no_trim_count` weekly counter: **deferred** (nag covers the behavioral
  need; counter is low marginal value)

## Success criteria (re-check at 30 days post-deploy)

- Paper book has ≥3 T1 auto-trims executed cleanly
- Live `target1_hit` positions show `target1_hit_date` populated
- Book post shows tier badges + nag entries when applicable
- No false fires on low-vol positions (e.g. INDV, AMD-class)
