# Project state

_Last updated 2026-09-05._

## The two books — keep them separate
Conflating these produces wrong conclusions. Measured 2026-09-05:

| Book | Source | 2026 result |
|---|---|---|
| **Paper (Alpaca)** | `claude_portfolio.html`, system-driven entries | 58 closed · 55% win · **+$31,535** · expectancy +$544/trade · dollar payoff 1.51× |
| **Manual (Robinhood/SnapTrade)** | `positions.json` + `trading_state.json` | 42W/57L · equity $94,993 · **−$20,579 YTD** |

**The system's own trades are profitable; the discretionary book is not.**
The cockpit "Record" block reads the manual book, so its 2.1× payoff is
describing the losing account — and it is measured in percent, which ignores
position size.

## Open positions (manual book, 2026-09-05)
CVX · MU · ARKK — all green, none near stop. Sizing mode `suspended` (3 losses).
Market state BLACKOUT (September seasonal rule), so gate reads PAPER ONLY.

## System stability — measured 2026-09-05

Evidence base: 473 completed Actions runs with a conclusion, 2026-04-28 → 2026-09-05.

| | |
|---|---|
| Overall success | **463/473 = 97.9%** (plus 6 skipped, 2 cancelled) |
| Runs needing a retry (`run_attempt` > 1) | **0** — no flakiness being masked |
| Daily Finviz Screener | 97/100 (2026-05-12 →) |
| Market Monitor | 96/100 (2026-04-28 →) |
| Position Book | 98/100 (2026-07-22 →) |
| Alpaca Executor | 62/62 = 100% (2026-06-15 →) |
| Test suite | 1420 tests, 6 pre-existing `test_archive` errors (boto3) |

**All 10 failures map to a known, diagnosed incident** — none unexplained:
Market Monitor ×4 on 06-29→07-02 (the Finviz `snapshot-td2` parsing break),
Daily Screener 07-17 (the logo-cell ticker doubling — `assert_scrape_healthy`
caught it same-day, which is the guard working), Daily Screener ×2 06-09
(dollar-volume gate / universe expansion), Alpaca Executor 06-04 (off-cycle run
before the screener; fixed by `_resolve_screener_csv`), Position Book ×2 08-06.

**Scheduled triggers are best-effort — this is the real reliability limit, not
code.** GitHub delays or drops cron runs under load:
- Delay past midnight UTC re-dates the output file. Daily Screener started
  23:54 on 08-26 → wrote `finviz_screeners_2026-08-27.csv`; started 23:57 on
  08-31 → wrote the 09-01 file. The "missing" 08-26 / 08-31 CSVs are this, not
  lost runs.
- 2026-08-06: the Daily Screener cron never fired at all, and Position Book
  failed twice. That day has no screener/quality/rotation output.

## Repo visibility — going private

The user offered (2026-09-05) to take the repo private once stability is shown.
Measured constraints:
- **Actions minutes fit easily.** Observed load ~25 runs / ~26 minutes per
  weekday → **~543 min/month** against a 2,000-min free private allowance.
  Public is unlimited, so this is the only cost that appears.
- **GitHub Pages is the blocker.** `has_pages: true` and the whole reporting
  surface (cockpit, portfolios, watchlist, chart grid, weekly) is served from
  `ananthsrinivasan.github.io/finviz-screener-agent/`, with `PAGES_BASE_URL`
  linking into it from every Slack message. Pages from a **private** repo
  requires GitHub Pro or higher; on Free, flipping visibility takes every
  dashboard and every Slack link dark.

**Agent rule:** do not assume visibility. Check it at session start
(`private` on the repo API) and set the memory-redaction bar from the answer —
strict while public, relaxed once private. Do not ask the user to confirm.

## Known broken / unfinished
- **`/peel-status` skill has no calibration tier-cap.** `.claude/commands/peel-status.md`
  reads `calib[ticker]["warn"]/["signal"]` raw. `CLAUDE.md` requires
  `min(calibrated, tier)` — calibration may only tighten, never loosen. Same bug
  already fixed three times elsewhere (screener 2026-05-29, executor 2026-06-12,
  `position_monitor.get_peel_thresholds` 2026-08-19). Live instance: CVX on
  2026-09-05 read `OK` at 4.85× against a raw warn of 7.5 when its ATR 1.9% tier
  warn is 3.0 / signal 4.0 — it was past the signal. **Not yet fixed.**
- **Paper stop leakage.** 4 of 26 losses breached the −8% floor, for −$11,310
  (31% of all loss dollars): DDOG −11.0%/6d, BTSG −10.7%/**0d**, GEV −9.1%/19d,
  ALGM −8.7%/4d. BTSG losing 10.7% in zero days is a gap — an entry-filter
  problem, not a stop problem. `docs/specs/earnings-entry-gate.md` exists;
  unverified whether it is wired. **Not yet investigated.**
- **Batch-entry days underperform.** 3+ same-day entries: 45 trades, +$11,921.
  1–2 entries: 13 trades, +$19,614. The 2026-07-31 batch of six went −$7,843.
  Small sample; a same-day entry cap is untested.
- **Watchlist is 68% dead weight** — 574 rows, 391 archived.

## Round-tripping, quantified (paper book)
Open unrealized profit peaked at **+$14,117** end of May and is **+$1,734** now.
About **$12,400 of unrealized gain drained away over Jun–Aug without being
booked**, while closed trades over those months were net +$11,131. This is the
documented "round-tripping winners" leak, measured for the first time.

## Recently shipped
- 2026-09-05 — paper portfolio month table now shows **Realized** (FIFO, shared
  `portfolio_common.monthly_realized`) beside **Equity Δ**. It previously showed
  only the equity series, so August read −$389 while 16 closed trades booked
  +$4,567, and the page contradicted its own trade log.
