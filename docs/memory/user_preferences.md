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

## Trading posture
- The system is a signal layer. The human decides. Never auto-execute against
  SnapTrade/Robinhood — alert only, forever.
- Capital preservation over activity. "Cash is a position."
