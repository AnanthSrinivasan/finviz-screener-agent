# Board — premarket board meeting (critical days)

Premarket decision meeting for the manual/Robinhood book. Produces one verdict per position, every verdict tagged by its source, and **writes the whole meeting to a dated log that gets committed** — so "what did you advise that morning?" is always answerable from the record, never from reconstruction.

## Hard rules

1. **Never quote a stop, target, or peak from memory.** Read them from `data/positions.json` at HEAD, and cite the value in the output.
2. **Tag every line of guidance:**
   - `[RULE]` — deterministic system rule fired; cite the number and its source (e.g. "stop $57.35 (positions.json) vs last $57.10 → breached").
   - `[DATA]` — a fact with source + timestamp (premarket quote, breadth, VIX).
   - `[JUDGMENT]` — my read; label it as opinion and give the invalidation.
3. **Log before advising.** The meeting file is written and committed even if the user never replies.

## Steps

1. `git pull --rebase origin main` (Actions commits state constantly).
2. Load `data/positions.json` open positions + latest `data/market_monitor_*.json` + last 3 days of breadth from `data/market_monitor_history.json`.
3. Pull live/premarket quotes for every open ticker + SPY/QQQ/IWM + VIX via Alpaca snapshots (`.env` keys). Premarket prints can be thin — note the timestamp of the last trade used.
4. **Flush-day context block** (the playbook): classify where we are — Day 1 distribution (400+ down-4%), Day 2 test (shrinking pressure? index holding 50 SMA/21 EMA? VIX <20? F&G stable?), Day 3 bounce. State it explicitly: "shakeout profile" vs "breakdown profile" with the four checks shown.
5. **Per-position verdicts**, one line each, worst first:
   - `STOP BREACHED [RULE]` — last price ≤ stop_price. Show both numbers + peak context. Default action: exit per rule. If ATR% ≤ 5% check `price_above_sma5` (rules.py SMA5 filter) and say whether it suppresses.
   - `STOP NEAR [DATA]` — within 1.5% of stop. State the exact price that flips it to RULE.
   - `TARGET [RULE]` — T1/T2 within today's likely range; peel plan.
   - `HOLD [RULE]` — no rule fired; show distance to stop.
   - Any override of a RULE verdict must be written as `[JUDGMENT] overriding RULE because <reason> — invalidation: <price/event>`.
6. **Market gate line**: current market_state + what it permits (entries/size) per CLAUDE.md state table.
7. Write the full meeting to `docs/board/YYYY-MM-DD.md` (create dir if missing): timestamp, quotes used, every verdict with tags, and — after the user decides — append their decisions. Commit + push with message `Board meeting YYYY-MM-DD [skip ci]`.
8. Post the same content to the user in the chat, verdicts first, market context second.

## Post-mortem use

When reviewing a past day ("what did we decide?", "was that exit right?"), read `docs/board/<date>.md` FIRST, and `git show <ref>:data/positions.json` for the stop as of that morning. If neither exists, say so — do not reconstruct advice from memory.
