# Feedback and corrections

Corrections already given. Re-making one of these is the specific failure this
file exists to prevent.

## 2026-09-06 — do not infer jurisdiction rules from where he lives
He said he lives in Luxembourg; I recorded "EU resident" and applied PRIIPs/
MiFID ETF restrictions and Luxembourg 6-month speculative-gains tax to him. He
corrected it: **he is a US trader** who lived in the US ~10 years and moved to
Lux for work. Location ≠ jurisdiction for brokerage, instruments or tax.
His EU interest is purely technical — TA transfers across markets.
Also told: "dont go sooner to build a change before confirm" — do not start new
work off a passing remark, even under the standing merge authorisation.

## SUPERSEDED — 2026-09-06 — "lost the user's jurisdiction" (this read was wrong)
Advised a whole thread as if the user were US-based: "EU-domiciled UCITS ETFs
are effectively unavailable to you", "use US-listed EUAD", "ADRs cover the 80%",
"adding a second market". He lives in **Luxembourg**. Every one of those is
backwards — UCITS is his home market, US-domiciled ETFs are the restricted ones.

Root cause: jurisdiction lived only in the laptop memory that has never been
absorbed into the repo (still queued, next_session.md §0). Nothing in the repo
stated where he lives, so a cloud session could not know.
Now recorded in `user_preferences.md` — read it before any instrument, access
or tax statement.

## 2026-09-06 — never hand the user tickers the system has not scored
Escalating in one thread: listed European names from model knowledge; called
them "Stage 2" with nothing behind it; then measured ITA at RS 2, rank 44/45,
BROKEN and still wrote "European defense could be diverging" to keep the claim
alive. The user charted Rheinmetall — price 1,034 under a declining 200 MA at
1,396.5, −48% from its high, below every MA — textbook **Stage 4**.
Rule: no ticker suggestions the screener has not scored, and when measured data
contradicts something already said, drop it rather than hedging it forward.

## 2026-09-06 — do not attach system vocabulary to unverified names
Told the user to "start with the defense block — that's where the Stage 2
structure has been" about European names, with no data behind it. "Stage 2" is a
defined gate in this system (`compute_stage`), so using it casually reads as a
screened result. It was also wrong for the tape: ITA sat at RS 2, rank 44/45,
BROKEN, −10% over 20d. Never borrow the system's terms — Stage 2, peel-safe,
Ready-to-Enter, Q, RS — for anything the system has not actually scored.

## 2026-09-06 — lead with the answer (softened same day)
Asked for EEM's top 3 holdings. The names WERE given, but below a caveat
paragraph plus a doc correction and a rotation read. The user asked whether the
question had landed — then said he had just not scrolled up. So: keep the
habit of putting a short answer on the first line, but do not treat this as a
missed answer. Don't over-correct into terseness on questions that genuinely
need context.

## 2026-09-06 — EEM is not an ex-China ETF (repo doc was wrong)
`CLAUDE.md` recorded EEM as "emerging markets ex-China ... since KWEB is
China-only". Wrong: EEM tracks MSCI EM, ~25-30% China, with Tencent and
Alibaba top-5 — it overlaps KWEB. The ex-China ticker is EMXC, and the INDA/EWZ
exposure the note cites was never actually added to the universe. Corrected in
place. Lesson: an ETF's stated *rationale* in the universe notes is not
evidence of what it holds — the repo stores price/RS metrics only, never
constituents, so holdings claims must be labelled as unverified.

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
