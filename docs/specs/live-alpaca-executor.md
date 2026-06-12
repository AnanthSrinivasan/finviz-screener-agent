# Spec — Live Alpaca Executor (agent-traded real-money account)

**Status:** DRAFT — awaiting user approval. Do not implement until approved.
**Date:** 2026-06-12
**Owner request:** 2026-06-11 — "i am going to ask you to start agent trade on alpaca live account … i am giving alpaca live which has 5k or so … i want for live also no distractions with multi tickers"

## 1. Goal

The agent auto-trades the user's **live Alpaca brokerage account (~$5k)** with the
exact same signal pipeline and rules engine as the paper account. This is a
**discipline experiment, not a scale-up**: the paper book (+22% since April,
23 closed trades, 56% win, payoff 2.4) proves the process; the live $5k account
proves it survives real fills. Success = 2–3 months of clean mechanical
execution; scaling afterwards is a funding decision, not a code change.

## 2. Hard boundaries (non-negotiable)

1. **Robinhood / SnapTrade stays alert-only forever.** This spec is a *scoped
   amendment* to the "system never places live orders" rule: live execution is
   authorized **only** for the dedicated Alpaca live account. No code path may
   ever submit an order via SnapTrade.
2. **No manual orders in the agent's account.** One discretionary trade
   contaminates the experiment. The monitor flags any position it didn't open
   (`[LIVE] FOREIGN POSITION` Slack alert) and refuses to manage it.
3. **Longs only, screener-qualified only.** No shorts, no options, no leveraged
   ETFs, no tickers outside the day's qualified list. The mandate is Stage-2
   longs from the daily screener — same as paper.

## 3. Architecture — profile, not fork

Parameterize `alpaca_executor.py` + `alpaca_monitor.py` with a
`TRADING_PROFILE=live` env profile rather than copying files:

| Concern | Paper (today) | Live profile |
|---|---|---|
| Credentials | `ALPACA_API_KEY/SECRET_KEY` | `ALPACA_LIVE_API_KEY/SECRET_KEY` (new GH secrets) |
| Base URL | `paper-api.alpaca.markets/v2` | `api.alpaca.markets/v2` (`ALPACA_LIVE_BASE_URL`) |
| Stops state | `data/paper_stops.json` | `data/live_alpaca_stops.json` |
| Streaks/sizing | `data/paper_trading_state.json` | `data/live_alpaca_trading_state.json` |
| Slack prefix | `[PAPER]` | `[LIVE 🔴]` |

The live profile has **its own streak/sizing state** — independent of both the
paper book and the user's manual-live `trading_state.json` (currently
`suspended`, 8 consecutive losses; the agent's record starts clean and earns
its own modes). Market-state gate, ETF-regime overlay, rules-engine exits
(`apply_position_rules`), no-averaging-down — all identical to paper.

**Workflows:** no new crons. The executor's live pass runs as a second step in
the existing `workflow_run` job after the daily screener; the monitor's live
pass runs inside the existing position-monitor schedule.

## 4. Deltas from paper (the actual work)

1. **Notional (fractional) orders.** $5k cannot buy whole shares of SNDK-class
   names. Buys submit as `notional` dollars; sells submit fractional `qty`.
   Floor: skip any order < $10; skip T1/T2 peel legs worth < $25.
2. **Position cap 2** (user: "no distractions with multi tickers"). Base size =
   `equity / cap × size_mul`; the market-state and sizing-mode multipliers
   apply unchanged.
3. **Marketable-limit instead of market orders.** Limit = `last × 1.005`, TIF
   day. Unfilled at EOD → cancel + Slack log line. Protects against thin-open
   slippage that paper never sees.
4. **Circuit breakers (code, not intentions):**
   - **Daily halt:** live equity −3% intraday → no further new entries today + Slack.
   - **Drawdown suspend:** equity < 85% of high-water mark → live profile
     suspends itself (flag in `live_alpaca_trading_state.json`) + Slack;
     re-enable requires a manual workflow_dispatch.
   - **Order sanity:** reject any single order > 60% of equity; reject any
     symbol not on today's qualified list.
5. **Idempotent orders.** `client_order_id = "live-{YYYYMMDD}-{ticker}"` so a
   retried workflow can never double-buy.
6. **Dry-run phase.** `LIVE_DRY_RUN=1`: full pipeline runs and Slack-logs every
   order it *would* place, submits nothing. Ships ON.

Note: PDT is no longer a constraint — FINRA removed the $25k pattern-day-trader
rule effective 2026-06-04 (Reg Notice 26-10).

## 5. Rollout

- **Phase 0 (week 1):** dry-run on. Verify order logs against paper's actual
  entries — they should match modulo sizing.
- **Phase 1 (months 1–2):** live. Cap 2, breakers armed, notional sizing.
- **Phase 2:** compare live vs paper stats (fill slippage, same-signal P&L
  delta). Scaling = user funds the account; no code change.

## 6. Out of scope

- Any SnapTrade/Robinhood execution (never).
- A live twin of `claude_portfolio.html` (phase 2 if wanted).
- Changing paper behavior in any way.

## 7. Open questions for user

1. **Position cap — 2 or 3?** (Spec assumes 2 for a $5k account.)
2. **Dry-run week — keep, or go straight live?** (Strong recommend: keep.)
3. **Breaker numbers OK?** (−3% daily halt · −15% drawdown suspend.)
4. **T1/T2 peels at this size?** Halving a ~$2.5k position leaves fragments;
   alternative is full-position exits only (simpler, less churn). Spec assumes
   peels stay ON with the $25 floor.
