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
