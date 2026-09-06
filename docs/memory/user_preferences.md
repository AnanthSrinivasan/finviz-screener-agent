# User preferences

## Autonomy — the standing instruction
- **Do not hand back manual steps.** The user does not have time to run
  git commands, click merge buttons, or shepherd a change. Finish the job.
  (2026-09-05, stated directly: "we cant operate on manual thing over time
  scaling and i dont have time all the times.")
- Merging approved work to `main` is part of finishing it, not a separate ask.
- Ask only when the answer changes what gets built. Otherwise pick the sane
  default, state it, proceed.
- **Confirm scope before building something NEW.** The standing merge
  authorisation covers shipping work he has already approved — it is NOT a
  licence to start new modules or features off a passing remark. (2026-09-06:
  "dont go sooner to build a change before confirm".) Answer the question
  first; propose the build in one line; wait.
- **Never end a response by assigning the user work.** No "you should review
  this", no "have your other session do X", no "remember to Y". If it needs
  doing, either do it or write it into `docs/memory/next_session.md` as an
  instruction to yourself. (2026-09-05: "dont instruct me u instruct yourself
  how will you understand for next chat. dont ask me anything man.")
- **Do not re-raise a question the user has already passed on.** If it can be
  resolved by choosing the conservative option, choose it, record the choice in
  memory, and move on. The public-repo/memory-privacy question was asked twice
  and is now settled by policy: memory stays operational, redaction is the
  agent's job.

## Process
- Follow the spec → review → tasks → execute → push → docs → memory loop in
  `CLAUDE.md`. The user relies on the review step as the decision point.
- Skip spec/review for typo/doc-only/one-line-no-semantic-change work, or when
  told to just do it.
- Each task = one committable change.

## How to communicate
- Direct answers first, reasoning after. No hedging, no padding.
- The user **will** interrogate a number he doesn't believe, and he is usually
  right to. Show the arithmetic and name the assumption behind any estimate.
- Say plainly when something is an estimate vs measured.
- Never present cached data as live. See the XBI incident in `CLAUDE.md`.
- Correct your own errors in one line and move on.

## Answer the question that was asked
- A short factual question gets a short factual answer. Lead with it, in the
  first line. (2026-09-06: asked "top 3 holdings in EEM" — the answer was there
  but sat below a caveat paragraph, a repo correction and a rotation read. The
  user later said he simply hadn't scrolled, so this is a readability
  preference, NOT a recorded failure to answer.)
- Side findings are worth surfacing, but AFTER the answer and only if they
  change a decision. Never in front of it.
- Length is not thoroughness. Match the size of the reply to the size of the ask.

## Jurisdiction — do NOT infer rules from location
**He is a US trader.** Lived in the US ~10 years, now living in Luxembourg for
work (stated 2026-09-06). His accounts and trading identity are US — Robinhood/
SnapTrade, Alpaca paper and live.

Corrected same day: an earlier entry read this as "EU resident" and applied
PRIIPs/MiFID retail restrictions and Luxembourg speculative-gains tax to him.
**That was wrong and he pushed back.** Physically living in the EU does not
make his brokerage, instrument access, or tax treatment EU.

Rule: never derive instrument availability or tax consequences from where he
lives. If it actually matters to an answer, ask one direct question. Otherwise
assume the US setup that the repo's accounts reflect.

His interest in European markets is **technical**: chart/TA methods are
price-based and transfer to any market, so he wants to look at EU charts. It is
not a question about EU residency, access or tax.

## Ticker suggestions
- Only name tickers the system has actually scored, from a screener CSV or a
  rotation snapshot. If the universe does not cover the market being asked
  about, say so and stop — no from-memory lists, no "unverified" hedge, because
  the user charts them anyway.
- Markets the system does NOT cover: everything outside US exchanges. The seven
  Finviz screens are US-only; there is no non-US exchange handling in the repo.
  **This is a real gap given the user is EU-resident.**

## Trading posture
- The system is a signal layer. The human decides. Never auto-execute against
  SnapTrade/Robinhood — alert only, forever.
- Capital preservation over activity. "Cash is a position."
