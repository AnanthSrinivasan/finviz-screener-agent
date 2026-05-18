# Spec — Consolidated Position Book Slack (3x daily)

**Date:** 2026-05-09
**Status:** Draft, awaiting execution
**Trigger:** Position-monitor Slack is per-event spam (stop / T1 / T2 / MA-trail / BE / peel / fade / sector × 9 positions × hourly). Strategy is buried in the noise. User can't see what to do at a glance.

## Read first
- `CLAUDE.md` → "Position Monitor — Rules Engine" (Layers 1, 1b, 2)
- `agents/trading/rules.py` — `apply_position_rules()` returns `events: list[dict]` with `kind` field
- `agents/trading/position_monitor.py` — current Slack posting flow
- `.github/workflows/position-monitor.yml` — current cron `0 14-21 * * 1-5` + manual triggers

## Goal
Replace ~6 alert types posted hourly per position with **one consolidated table message at 3 fixed times per day.** Strategy-readable in 10 seconds. **Critical alerts that cannot wait for the next scheduled book post are emitted as their own separate Slack messages immediately.**

## Schedule

**Book post (consolidated table):**
| Time UTC | Time ET | Purpose |
|---|---|---|
| **13:15 UTC** | 9:15 AM ET | Pre-open book — overnight moves, stops vs gap, plan |
| **14:30 UTC** | 10:30 AM ET | Post-open settle — first hour digested |
| **17:30 UTC** | 1:30 PM ET | Mid-day check — afternoon positioning |

**Critical-only check (separate cron, separate message):**
- Every **30 min** during market hours (14:00–21:00 UTC).
- Posts ONLY when one of: `stop_hit`, `auto_closed`, `share_drift_avg_up`, `share_drift_partial_sell`, `t1_hit_first_time`, `t2_hit_first_time`.
- Never posts the table. Each event = its own short Slack message.
- **A critical event also gets recorded into `events_since_last` so the next book post acknowledges it as a footnote** (so user has both the immediate ping and the consolidated context).

## Book post format

```
📊 POSITION BOOK — 14:30 UTC (post-open)
Market: GREEN · Sizing: SUSPENDED (6L) · Open P/L: +$6,252

TK    Avg      Now     Move    Peak%   Stop      $P/L     STATE
AAOI  124.75   148.99  +19.4%  +53.6%  174.36    +$2,424  ⚠ TRIM (gave back 34pp)
GLW   165.09   186.91  +13.2%  +20.1%  186.92    +$1,963  🚨 STOP NEAR ($0.01)
INDV   34.15    39.52  +15.7%  +19.6%   38.45    +$1,341  ✓ HOLD
CRWV  115.71   113.99   -1.5%  +19.5%  125.40      -$172  🚨 ROUND-TRIP — cut half
TNA    63.04    65.11   +3.3%   +7.2%   61.90       +$207  ✓ HOLD
NVDA  204.32   215.21   +5.3%   +6.6%  204.52       +$545  ✓ HOLD
IREN   61.40    61.55   +0.2%   +6.9%   59.56        +$15  ✓ HOLD
ZVRA   11.42    11.12   -2.6%   +1.5%   10.55        -$60  ✓ HOLD
APLD   41.50    41.27   -0.6%   +1.7%   36.46        -$11  ✓ HOLD

🚨 ACTIONS TODAY
  • AAOI: trim 33sh @ market — locks ~$800, peak give-back 34pp
  • GLW: stop ≈ current ($186.92 vs $186.91) — likely fires today
  • CRWV: cut 50sh — round-tripped, FIGS pattern shape

📋 EVENTS SINCE LAST POST
  • CRWV: tagged ROUND-TRIP at 13:45 UTC (peak $138.23 → -1.5%)
  • GLW: hit T1 ($198.11) at 13:22 UTC — alert posted separately

🔗 dashboard · positions.json
```

### Design rules
1. **One table per book post.** Per-position status reduces to one word: `✓ HOLD` · `⚠ TRIM` · `🚨 ROUND-TRIP` · `🚨 STOP NEAR` · `🔻 STOPPED`.
2. **Peak% next to Move.** Give-back from peak is the metric current Slack hides — make it visible.
3. **Actions block separate from data.** Top-3 things to do today. Plain-language commands.
4. **Events digest, not events spam.** Inter-post events compressed to bullets, not their own posts (except critical — see above).
5. **Market state + sizing mode in header.** Constant context, one line.

## Critical alert message format (separate posts)

Critical events fire their own short Slack message, formatted minimally:

```
🚨 STOP HIT — CRWV
Stop $125.40 triggered at 14:23 UTC (price $125.18)
100sh @ avg $115.71 → est. close $125.18 (+8.2%)
[no auto-sell — alert only, system signals; you decide]
```

```
🎯 T1 HIT — GLW
Target $198.11 reached at 13:22 UTC
Suggest: trim 30sh (1/3) → locks $988
Stop ratchets to $186.92 (BE)
```

```
🟡 SHARES INCREASED — AAOI
SnapTrade reports 150sh (was 100). Avg-up detected.
New avg cost $128.40 · T1 $154.08 · T2 $179.76
breakeven_activated cleared, target1_hit cleared
```

```
🔻 AUTO-CLOSED — FIGS
Position no longer in SnapTrade. Closing at $10.92 (live_quote).
Result: -34.4% · -$1,719 · 13 days held · close_source=live_quote
total_losses 6→7 · sizing_mode → suspended
```

Each is a one-shot: posts when event detected, never re-posts (dedup via existing event-state files).

## State map (book post `STATE` column)

```
🔻 STOPPED      stop_hit OR auto_closed THIS RUN          (rare in book — usually emitted as separate)
🚨 STOP NEAR    abs(current - stop) / current < 0.5%
🚨 ROUND-TRIP   peak_gain >= 15% AND current_pct < (peak_gain - 18)   # gave back >18pp of peak
⚠ TRIM          peak_gain >= 25% AND current_pct < (peak_gain - 10)
                AND target1_hit AND shares_unchanged_since_t1
✓ HOLD         default
```

`⚠ TRIM` and `🚨 ROUND-TRIP` rows feed the ACTIONS block at the top.

## What gets eliminated (current → new)

| Current message | New behavior |
|---|---|
| Per-position hourly status with full metrics block | Folded into book table row |
| BE crossed alert | Footnote in next book post |
| MA-trail close-below alert | Footnote bullet (post-close-only, fires after 17:30 book — show in next pre-open book) |
| Peel-warn alert | Footnote bullet |
| Gain-fading alert (1×ATR fade) | Folded into peak% column visibility + STATE |
| Sector rotating-out alert | Footnote bullet |
| **Stop-hit** | **STAYS as immediate separate message** |
| **Auto-close (full exit)** | **STAYS as immediate separate message** |
| **Share-drift reconcile (avg-up / partial-sell)** | **STAYS as immediate separate message** |
| **T1 / T2 first-time hit** | **STAYS as immediate separate message** (these are decision points, not noise) |

## Implementation

### Files
- `agents/trading/position_monitor.py` — main refactor. Existing per-event Slack calls replaced with append to two queues:
  - `book_events` (digest in next book post)
  - `critical_events` (post immediately as separate Slack)
- `agents/trading/book_table.py` — **new**. Pure functions:
  - `build_book_table(positions, live_prices, market_state, sizing_mode) -> str`
  - `compute_state(position, live_price) -> str` (returns one of the 5 STATE strings)
  - `build_action_block(positions_with_state) -> str`
  - `build_events_digest(events_since_last) -> str`
- `data/book_last_post.json` — **new state file**. Schema: `{last_book_post_ts, events_since_last: [event_dict, ...]}`. Cleared each book post.
- `.github/workflows/position-monitor.yml` — cron change:
  - **Book cron:** `15 13,14 * * 1-5` for 13:15 + 14:30, plus `30 17 * * 1-5` for 17:30.
    Actually three rows is cleaner: `15 13 * * 1-5`, `30 14 * * 1-5`, `30 17 * * 1-5`.
  - **Critical cron:** `*/30 14-21 * * 1-5` — runs every 30 min, posts only on critical events.
  - Same workflow file, two job names with different `if:` matching the cron schedule. Or two workflows. Decision in §"Open questions".

### Critical vs digest routing in `apply_position_rules()`

`rules.py` already returns events with `kind`. Add a constant:

```python
CRITICAL_EVENT_KINDS = {
    "stop_hit",
    "auto_closed",
    "share_drift_avg_up",
    "share_drift_partial_sell",
    "t1_first_hit",
    "t2_first_hit",
}
```

Caller (`position_monitor.py`) splits:
```python
critical = [e for e in events if e["kind"] in CRITICAL_EVENT_KINDS]
digest   = [e for e in events if e["kind"] not in CRITICAL_EVENT_KINDS]

for e in critical:
    post_slack_critical(e)         # immediate, own message
    append_to_digest_log(e)        # also remembered for next book

for e in digest:
    append_to_digest_log(e)        # only shows in next book post

if is_book_run():
    post_book(positions, prices, digest_log)
    clear_digest_log()
```

### Live price fetch
Alpaca `/v2/stocks/trades/latest` for all open tickers in one call (proven this session). Falls back to last daily close if IEX returns empty for a ticker. Cache in run scope so `compute_state()` doesn't re-fetch.

## Tests

- `tests/test_book_table.py`:
  - 9-position fixture from real `data/positions.json` snapshot renders deterministically (snapshot test)
  - `compute_state()` cases: ROUND-TRIP (CRWV-style), STOP-NEAR (GLW-style), TRIM (AAOI-style), HOLD (TNA-style)
  - `build_action_block()` returns top 3 actions, sorted by severity
  - Events digest groups by ticker, shows time + kind + key value
- `tests/test_critical_routing.py`:
  - `stop_hit` event routes to critical queue AND digest log
  - `peel_warn` event routes to digest only
  - `t1_first_hit` routes to critical AND digest
  - Empty critical queue produces no Slack post
- `tests/test_book_state_persistence.py`:
  - Events accumulate in `book_last_post.json` between book runs
  - Book run consumes + clears

## Verification

1. `python -m unittest discover -s tests -t .` — all pass, new tests included.
2. `gh workflow run position-monitor.yml` × 2 (book run + critical run) — verify single Slack post per book run, separate posts only for critical events.
3. Track messages in `#positions` for 1 trading day after merge — should drop from ~30+/day to 3 book posts + ≤5 critical pings.

## Open questions (decide before exec)

1. **Book schedule:** 13:15 / 14:30 / 17:30 UTC — confirm. (Or add a 4th at 20:30 UTC for close-prep?)
2. **Critical cron interval:** 30 min during market hours. Confirm. Faster = more compute, slower = lag on stop-hits. Current is hourly, so 30min is already a tightening.
3. **One workflow file with two jobs, or two workflow files?** Two files is clearer separation; one file is fewer files in `.github/workflows/`. Recommend: **two files** (`position-book.yml` + `position-critical.yml`).
4. **Round-trip threshold:** 18pp give-back from peak. Lower = more flags. Confirm or adjust.
5. **TRIM threshold:** peak ≥ 25% AND give-back ≥ 10pp AND T1 hit AND shares unchanged. Adjust?
6. **DM channel:** book posts to `#positions` (existing) or to a personal DM channel for higher signal? Critical alerts always to `#positions`.

## Out of scope (deferred)
- Auto-execute trim suggestions. ALERT-ONLY remains the rule. The book recommends, you click.
- Charts in book posts. Slack file uploads add complexity; keep first version text-only.
- Mobile push prioritization (Slack-side config, not code).

## Files touched
- `agents/trading/position_monitor.py`
- `agents/trading/book_table.py` (new)
- `agents/trading/rules.py` (add `CRITICAL_EVENT_KINDS`)
- `data/book_last_post.json` (new state)
- `.github/workflows/position-book.yml` (new) + `.github/workflows/position-critical.yml` (rename/replace `position-monitor.yml`)
- `tests/test_book_table.py` (new)
- `tests/test_critical_routing.py` (new)
- `tests/test_book_state_persistence.py` (new)
- `CLAUDE.md` — update Position Monitor section to document book schedule + critical-routing
- `SYSTEM_DOCS.md` — same
