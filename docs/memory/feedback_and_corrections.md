# Feedback and corrections

Corrections already given. Re-making one of these is the specific failure this
file exists to prevent.

## 2026-09-05 — instruct yourself, not the user
Ended a response telling the user to have a laptop session merge the old memory
files and to review them for personal content. That is agent work. Anything the
next session must do belongs in `docs/memory/next_session.md`, written as an
instruction to yourself — never as homework for the user, and never as another
question.

## 2026-09-05 — don't hand back manual work
"i dont want to do manual action … we cant operate on manual thing over time
scaling and i dont have time all the times." Approved work gets finished and
merged. Do not stop at "here's a branch, you merge it."

## 2026-09-05 — don't state facts you haven't read back
Quoted a commit hash (`a7b96d5`) that was never in any tool output; the real
hash was `6ff8552`. Branch and content were right, the hash was invented.
Read identifiers back from output before quoting them.

## 2026-09-05 — measure before asserting a pattern
Claimed "you size losers bigger than winners" from a single month. Controlling
for equity growth, it holds in 4 of 6 months at a 1.20× median — a tendency,
not a rule, and July (1.94×) drives most of it. Check the full sample before
naming something systemic.

## 2026-09-05 — percent payoff vs dollar payoff
Avg-win% / avg-loss% ignores position size. The manual book shows 2.05× in
percent while the account falls. Always state which basis a payoff figure uses.

## Standing rules from CLAUDE.md worth repeating here
- **Never quote a cached number as current.** `data/` is a snapshot from the
  last workflow run. The 2026-08-19 XBI miss (reported RS 51/rank 22 from cache
  while it was actually RS 86/rank 6) cost a real trade. Use `utils/live_check.py`
  — and in a cloud session, where live data is blocked, say so instead.
- **Calibration may only tighten a tier, never loosen it.** This bug has been
  fixed three times in three files and is still live in `/peel-status`.
- **Always pass an explicit `start` to Alpaca bars fetches** or they silently
  return `[]`.
- **Never read the Finviz ticker cell with `.text`** — the logo span doubles the
  first letter. Use `agents/utils/finviz_table.py::extract_ticker`.
