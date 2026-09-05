# Next-session work queue — read this, then act. Do not ask.

This file is the standing instruction to the next Claude session. Everything
here is already authorised. Work it top-down. Update it as you go: tick items
off, add what you find. Do not hand any of it back to the user as a task.

## 0. One-time: absorb the legacy laptop memory (LOCAL sessions only)

If `/Users/sananth/.claude/projects/-Users-sananth-Documents-Mac-Backup-Languages-Python-finviz-screener-agent-new/memory/`
exists on this machine — you are on the Mac and this has not been done yet:

1. Read every file in it.
2. Merge its content into the matching file under `docs/memory/`. Deduplicate
   against what is already there; keep the older dated entries, they are the
   record.
3. **Redact while merging — do not ask the user to do it.** The repo is public.
   Keep: process, preferences, decisions, technical state, corrections,
   trading figures already published on the dashboards. Drop: personal
   reflection, anything about the user's psychology, health, finances outside
   the published books, or third parties.
4. Commit, push, and delete this section from this file.

In a cloud session that path does not exist. Skip silently — it is not an error
and not worth mentioning.

## 1. Open work, highest value first

- **B-08 — `/peel-status` calibration tier-cap.** `.claude/commands/peel-status.md`
  reads `calib[t]["warn"]/["signal"]` raw. Add `min(calibrated, tier)`, mirroring
  `position_monitor.get_peel_thresholds`. Same bug already fixed three times
  elsewhere. Reference failure: CVX 2026-09-05 read `OK` at ATR mult 4.85 when
  its ATR 1.9% tier signal is 4.0. Small, contained — spec not required, just
  fix it, test, merge.
- **B-09 — paper stop leakage past −8%.** 4 of 26 losses breached the floor for
  −$11,310 (31% of all loss dollars): DDOG −11.0%/6d, BTSG −10.7%/**0d**,
  GEV −9.1%/19d, ALGM −8.7%/4d. BTSG at 0 days is a gap, so it is an entry
  filter problem, not a stop problem — start by checking whether
  `docs/specs/earnings-entry-gate.md` is actually wired into
  `alpaca_executor.py`. Needs live data, so run it from a local session.
  This one warrants a spec before changing execution behaviour.
- **B-10 — same-day entry cap.** Batch days (3+ entries): 45 trades, +$11,921.
  1–2 entry days: 13 trades, +$19,614. The 2026-07-31 batch of six went
  −$7,843. Sample is small — measure across more history before enforcing a cap.
- **Watchlist hygiene.** 574 rows, 391 archived (68% dead weight). Prune.

## 2. Standing habits for this project

- Verify before asserting a pattern: control for equity growth and check the
  full sample, not one month.
- State whether a payoff figure is percent-based or dollar-based. They disagree
  here, and the dollar one is the true one.
- In a cloud session, live market data is blocked and there are no API keys.
  Say so instead of quoting cached snapshots as current.
- Run `python -m unittest discover -s tests -t .` before every push. Baseline is
  6 `test_archive` errors (boto3 absent). Revert `data/recent_events.json`
  afterwards — the suite writes to it.
