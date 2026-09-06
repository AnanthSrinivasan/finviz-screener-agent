# User preferences

## Autonomy — the standing instruction
- **Do not hand back manual steps.** The user does not have time to run
  git commands, click merge buttons, or shepherd a change. Finish the job.
  (2026-09-05, stated directly: "we cant operate on manual thing over time
  scaling and i dont have time all the times.")
- Merging approved work to `main` is part of finishing it, not a separate ask.
- Ask only when the answer changes what gets built. Otherwise pick the sane
  default, state it, proceed.
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

## Jurisdiction — READ THIS BEFORE ANY INSTRUMENT OR TAX STATEMENT
**The user is resident in Luxembourg (EU), not the US** (stated 2026-09-06).
- **UCITS / EU-listed ETFs are available to him** — his native market. Never
  describe them as inaccessible.
- **US-domiciled ETFs are the restricted ones.** Under PRIIPs/MiFID II an EU
  retail investor generally cannot buy SPY, QQQ, ITA, ARKK, IBIT, XBI through
  an EU broker. The 45-ETF rotation universe is a SIGNAL layer for him, not a
  shopping list — the "ETF play" line in Rotation Catalyst may be unactionable.
- **European exchanges are his HOME market.** Do not frame trading EU as
  "adding a second market"; the US is the remote one.
- Existing US brokerage (Robinhood/SnapTrade, Alpaca) likely predates the move;
  do not assume it generalises to new US-domiciled purchases.
- **Tax shape affects strategy:** Luxembourg treats securities gains held
  **under 6 months** as speculative (ordinary income); over 6 months generally
  exempt for non-substantial stakes. His swing style (winners ~17 days) sits
  entirely in the speculative bucket. Raise it when hold period or strategy is
  discussed — always as general information to confirm with a local advisor.

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
