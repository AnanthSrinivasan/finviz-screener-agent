# Environment notes — laptop vs cloud session

Claude Code runs in two places against this repo. They are NOT equivalent.

## Local (user's Mac)
- Works directly on `main`; the `PostToolUse` hook in `.claude/settings.json`
  auto-pulls and pushes after each commit.
- Full network: Finviz, Alpaca, Yahoo, SnapTrade all reachable.
- API keys present in the environment.

## Cloud (claude.ai/code, GitHub Actions runners)
- Session is pinned to a feature branch at launch. Pushing to `main` requires
  merging the branch — the session cannot be re-pointed mid-flight.
- **Outbound network is restricted by an egress policy.** Confirmed blocked
  2026-09-05: `query2.finance.yahoo.com` (yfinance), `finviz.com`,
  `data.alpaca.markets`, `stooq.com`, and the project's own
  `ananthsrinivasan.github.io` Pages site. All return 403 on CONNECT.
  Do not try to route around it — report the blocked host.
- **No API keys** — `ALPACA_*`, `SNAPTRADE_*`, `ANTHROPIC_API_KEY` are unset.
- Consequence: any skill needing live quotes (`/peel-status`, `/pos-review`,
  `/fills`, `utils/live_check.py`) cannot run in a cloud session. Say so
  rather than substituting cached files and calling it current.
- What DOES work: the whole repo, git, the test suite, and every `data/*.json`
  / `data/*.html` snapshot committed by Actions.

## Test-suite quirk
- `tests/test_archive.py` errors (6) without boto3 installed. This is the
  baseline, not a regression — confirm by stashing before blaming a change.
- Running the suite writes to `data/recent_events.json`. Revert it before
  committing; it is a test artifact, not a change.
