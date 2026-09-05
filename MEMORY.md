# MEMORY — index

**Read this file and every file it links BEFORE the first non-read action of a session.**
These live in the repo (not on any one machine) so a laptop session and a
Claude Code cloud session load identical memory.

| File | Holds |
|---|---|
| [docs/memory/user_preferences.md](docs/memory/user_preferences.md) | How the user wants to be worked with — process, tone, autonomy |
| [docs/memory/project_state.md](docs/memory/project_state.md) | Where things stand: accounts, open work, known-broken |
| [docs/memory/feedback_and_corrections.md](docs/memory/feedback_and_corrections.md) | Corrections already given. Re-making these is the failure mode this file exists to prevent |
| [docs/memory/environment_notes.md](docs/memory/environment_notes.md) | Machine/session differences — what works where |

## Writing to memory

Per `CLAUDE.md` rule 5: when you learn something new about the user's
preferences or the project — **especially after a correction** — write it here
as the FIRST step of your response, not the last. One line, dated, factual.

**This repository is PUBLIC.** Anything written here is world-readable. Keep
memory operational: process, decisions, technical state. Trading numbers that
already appear on the published dashboards are fine. Personal reflection is not.
