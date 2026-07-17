# Trader Mirror — monthly You-vs-System scorecard, dollar-quantified

**Status:** SPEC — awaiting user approval
**Date:** 2026-07-15
**Depends on:** nothing (reads existing state + bars)
**Conflicts:** none (new module + weekly-agent hook; no file overlap with other spec agents)

## 1. Problem

The user's documented leaks are round-tripping winners and hold-in-hope. The rules engine computes the "correct" exit continuously, and we already shadow-log system-vs-system counterfactuals (`trail_mode_ab.json`) — but nothing ever compares the USER's actual manual exits against the rules engine. The philosophy section says 90% of trading is psychology; this is the only planned feature that addresses the 90%.

## 2. Design

### 2.1 Counterfactual replay — `agents/utils/trader_mirror.py`

For each **manual-book** closed trade in the window (from `positions.json` closed positions + real fills in `data/position_history.json`):

1. Fetch daily bars entry→close+10 sessions (Alpaca, explicit `start`).
2. Replay `agents/trading/rules.py.apply_position_rules()` day-by-day from actual entry price/date (precedent: `scripts/replay_flush_suppress.py`) → the system's exit date + price.
3. `delta_usd = (system_exit − actual_exit) × shares` — positive means the system would have kept more.

### 2.2 Leak classification (per trade, mutually exclusive, first match wins)

| Leak | Condition | Message tone (per directional-guidance memory) |
|---|---|---|
| `hold_in_hope` | actual exit below the rules stop that was active ≥2 sessions earlier | "stop said out at $X on {date}; actual exit $Y — held {n} extra days, cost ${...}" |
| `round_trip` | peak ≥ +15% during hold, exited < +5% | "peaked +{p}%, exited +{e}% — gave back ${...} from the peak" |
| `early_exit` | exited while rules engine still long AND price went ≥ +5% higher within 10 sessions | "sold ${...} early — system exit was {date} at $Z" |
| `disciplined` | none of the above (|delta| small or user beat the system) | credit it explicitly — the mirror must show wins too |

### 2.3 Output — monthly, first Saturday (hook in `finviz_weekly_agent.py` when `today.day <= 7`)

- `data/trader_mirror_YYYY-MM.html` (linked from the weekly report + index History area): per-trade table (ticker, entry, your exit, system exit, delta $, leak tag) + totals per leak bucket + 3-month trend of each bucket.
- **Slack (`#weekly-alerts`), 5 lines max, verdict-first:**
  `🪞 Trader Mirror — June: you left $2,340 on the table. hold-in-hope $1,800 (VIK, TNA) · round-trip $540 (Z) · early-exit $0 · disciplined: 4 of 7 trades — best month since April.`
- Neutral months ("system and you within $200") say so plainly — no manufactured drama.

### 2.4 Honesty constraints

- Counterfactual uses only information available at the time (the replay is causal by construction — same engine, same bars).
- System exit assumes fill at the day's close after the signal (conservative, no look-ahead).
- Trades without recoverable fills (no `position_history` record) are listed as `unscored`, never guessed.

## 3. Tests (`tests/test_trader_mirror.py`)

- Replay parity: synthetic bars where the rules exit is known.
- Each leak classifier + precedence order; `disciplined` credit path.
- Delta math with partial sells (FIFO share matching via `utils/pnl_walk` semantics — the proven P&L source per memory).
- Month window selection; unscored path; render import-safe.

## 4. Non-goals

Scoring paper/live books (they already follow the rules — nothing to mirror). Real-time nagging (monthly cadence only — a mirror, not a backseat driver). Auto-adjusting any rule from mirror results.
