# Spec — operating memory in the repo + the full workflow loop

**Status:** shipped 2026-09-05
**Trigger:** a cloud session found `CLAUDE.md` rule 1 unsatisfiable and both
`PostToolUse` hooks dead.

## Problem

1. **Memory was machine-local.** `CLAUDE.md:5` pointed at
   `/Users/sananth/.claude/projects/-Users-sananth-.../memory/`. Nothing named
   `MEMORY*` has ever been committed (`git log --all -- '*MEMORY*'` → empty).
   Cloud sessions ran with no memory at all while being told reading it was
   mandatory before the first non-read action.
2. **Half the workflow was undocumented.** `CLAUDE.md:11-18` documented
   spec → review → tasks → execute. Commit/push lived only as rule 2; the
   SYSTEM_DOCS update lived only inside a hook; updating `CLAUDE.md` itself was
   written nowhere; `BACKLOG.md` was never mentioned in `CLAUDE.md` despite
   being the tracking file.
3. **Both hooks were hardcoded to the laptop path** and failed on every Bash
   call in a cloud session. Hook 1 also pushed a hardcoded `main`, which is
   wrong on any feature branch — including the branches cloud sessions are
   pinned to.
4. **The doc reminder ran one way only:** it nagged about `SYSTEM_DOCS.md` when
   `CLAUDE.md` changed, never the reverse, so agent-logic changes could land
   with `CLAUDE.md` stale — and `CLAUDE.md` is what steers the agent.

## Change

**A. Memory into the repo.** `MEMORY.md` (index) + `docs/memory/{user_preferences,
project_state,feedback_and_corrections,environment_notes}.md`. `CLAUDE.md:5`
repointed to the relative path so laptop and cloud load identically.

Privacy: the repo is public (`"private": false` via the GitHub API). Memory is
therefore scoped to operational content — process, decisions, technical state.
Figures already published on the dashboards are fine; personal reflection is
excluded. Stated inline in `MEMORY.md` and `CLAUDE.md` rule 5 so it survives.

**B. Loop extended to 7 steps** — added 5 Ship, 6 Docs, 7 Memory + CLAUDE.md.

**C. Standing authorisations** — merge approved work to `main` without asking,
gated on the suite being at baseline. Explicit carve-outs kept for live money,
data deletion, history rewrites, and changes the user has not seen the shape of.

**D. Hooks made portable** — `cd` removed from both; hook 1 pushes
`git rev-parse --abbrev-ref HEAD`; hook 2 fires two reminders (docs, and
memory + backlog) on the appropriate path sets.

## Verification

- `jq -e` on the hook path → exit 0, valid schema.
- Hook 2 pipe-tested against real `HEAD`: fires, emits valid JSON, and was
  observed firing live in-session with the Mac-path error gone.
- Hook 1 branch resolution confirmed against the session branch.
- `grep -rn "/Users/sananth"` across `.claude/`, `CLAUDE.md`, `MEMORY.md`,
  `docs/memory/` → clean.
- Full suite at baseline (see below).

## Notes / follow-ups

- Memory is seeded from this session only. The laptop's existing memory files
  can be merged in later — review for personal content first, the repo is public.
- Still open, recorded in `docs/memory/project_state.md`: the `/peel-status`
  calibration tier-cap bug, and the paper-book stop leakage
  (4 losses past −8%, −$11,310).
