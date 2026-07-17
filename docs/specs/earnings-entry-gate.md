# Earnings Entry Gate — never open a position into a report blind

**Status:** APPROVED 2026-07-15 (user) — blackout = 3 trading days
**Date:** 2026-07-15
**Depends on:** nothing (fully independent)
**Conflicts:** touches `finviz_agent.py` (also touched by signal-scorecard spec) and `alpaca_executor.py` — coordinate or run sequentially with scorecard agent.

## 1. Problem (verified 2026-07-15)

`grep earnings alpaca_executor.py agents/trading/*.py` → zero hits. `earnings_alert.py` warns about **held** positions only. The paper/live executor will open a full-size position two days before a report; Ready-to-Enter will promote one; the cockpit Qualified card doesn't show the date. One bad gap-down through a fresh entry erases weeks of edge — and live trades real dollars now.

## 2. Design

### 2.1 Source + parser

Finviz snapshot field `Earnings` (e.g. `"Jul 24 AMC"`, `"Aug 05 BMO"`, `"-"`). `earnings_alert.py` already parses this — **extract its parser into `agents/utils/earnings_date.py`** as `parse_earnings_field(s, today) -> Optional[date]` (handles year rollover: a Jan date seen in Dec is next year) + `trading_days_until(d, today) -> int`. `earnings_alert.py` refactored to import it (behavior unchanged).

### 2.2 Constant

`agents/trading/rules.py`: `EARNINGS_BLACKOUT_DAYS = 3` (trading days). Single source for all consumers.

### 2.3 Wiring — block for robots, badge for humans

| Surface | Behavior |
|---|---|
| `alpaca_executor.py` (paper AND live) | **Hard skip** when earnings within blackout. Slack line: `⏸ CRWD skipped — earnings Jul 24 (2td)`. Logged so the skip is visible, not silent. |
| `finviz_agent.py` `_is_ready_to_enter` | Candidate within blackout is **not promoted** to entry-ready; stays focus with `hold_reason="earnings Jul 24"`. Ready-to-Enter Slack line for near-miss names shows `⚠ ER Jul 24`. |
| Daily Cockpit qualified cards (`generate_daily_cockpit.py`) | Every card always shows `ER: Jul 24 (2td)`; inside blackout the card is greyed with the reason. |
| Week-Ahead Shortlist (`agents/utils/week_ahead_shortlist.py`) | Trade-plan card gains an `Earnings:` line; a name reporting in the coming week is kept but flagged `⚠ reports this week — plan the hold or skip`. |

Human surfaces never hide a name — they inform. Only the auto-executor hard-blocks.

### 2.4 Fail-open rule

Earnings field missing/unparseable → treat as no earnings known, do NOT block (log once). A data gap must not freeze entries; the executor already has enough hard gates.

## 3. Tests (`tests/test_earnings_gate.py`)

- Parser: AMC/BMO, year rollover, `-`, garbage input.
- `trading_days_until`: weekends, same-day.
- Executor skip at exactly N days, pass at N+1, fail-open on missing field.
- `_is_ready_to_enter` demotion + reason string.

## 4. Decision

Blackout **N=3 trading days — CONFIRMED by user 2026-07-15.**

## 5. Non-goals

Exiting held positions before earnings (existing earnings_alert covers awareness; exit stays human). Post-earnings re-entry timing. Options/hedging.
