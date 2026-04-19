# Finviz Screener Agent

## MANDATORY — Read before every action

1. Read all memory files in `/Users/sananth/.claude/projects/-Users-sananth-Documents-Mac-Backup-Languages-Python-finviz-screener-agent-new/memory/` before doing anything
2. Always `git pull --rebase origin main` before `git push` — Actions commits data files back constantly
3. After any screener/agent logic change: run the relevant GH Actions workflow and verify the logs, not just unit tests
4. Run `python -m unittest discover -s tests -t .` before every push

## Workflow for every non-trivial task — spec → review → tasks → execute

For any task beyond a one-line fix (logic change, dashboard change, new feature, multi-file edit), follow this loop:

1. **Spec** — write what you understand the user wants: the problem, the concrete change, the files/functions involved, and the tradeoffs. Point to exact `file:line` anchors.
2. **Review** — surface the spec to the user *before* editing. Call out open questions and decisions the user needs to make (e.g. sizing caps, UI placement). Do not proceed without a yes.
3. **Tasks** — break the approved spec into a numbered task list. Short. Each task is a single committable change.
4. **Execute** — work the tasks in order, mark each done, then run tests + workflow verification per rules 3–4 above.

**Skip spec/review only when:** typo fix, doc-only edit, user explicitly says "just do it", or the change is fully contained to one line and has no semantic impact. When in doubt, spec first.

**Why:** prevents wasted work on wrong-shaped solutions and gives the user a decision point before any irreversible change.

Automated stock screening + position monitoring system. Scrapes Finviz daily, scores tickers using Weinstein Stage Analysis + quality metrics, monitors open positions via SnapTrade, and sends alerts to Slack. Runs entirely on GitHub Actions.

**Repo:** `AnanthSrinivasan/finviz-screener-agent` (branch: `main`)
**Stack:** Python 3.11 (GitHub Actions), Finviz scraping, Alpaca API, SnapTrade API, Claude API, Slack webhooks, AWS S3 (archival), AWS EventBridge + Lambda (X/Twitter publishing). yfinance used only in `finviz_weekly_agent.py` for quarterly EPS/revenue history (character change check).
**Live reports:** https://ananthsrinivasan.github.io/finviz-screener-agent/

## Architecture — 10 Agents + Test Suite

| Agent | File | Schedule | Slack Channel |
|-------|------|----------|---------------|
| Daily Screener | `finviz_agent.py` | 20:30 UTC Mon-Fri | `#daily-alerts` |
| Weekly Review | `finviz_weekly_agent.py` | 10:00 UTC Saturday | `#weekly-alerts` |
| Market Monitor | `market_monitor.py` | 21:00 UTC Mon-Fri | `#market-alerts` (state changes), `#market-daily` (daily) |
| Position Monitor | `position_monitor.py` | Hourly 14:00-21:00 UTC + 12:00 + 22:00 UTC Mon-Fri | `#positions` |
| Alerts | `alerts_agent.py` | 21:00 UTC Mon-Fri | `#general-alerts` |
| Earnings Alert | `earnings_alert.py` | 21:30 UTC Mon-Fri | `#general-alerts` |
| Market Pulse | `market_pulse.py` | 4x daily (10am, 12:10pm, 2:20pm, 4pm ET) | `#daily-alerts` |
| Winners Watchlist | `winners_watchlist.py` | Monday evenings | `#weekly-alerts` |
| **Paper Executor** | `alpaca_executor.py` | After Daily Screener (workflow_run) + manual | `#daily-alerts` (BUY placements + summary only) |
| **Paper Monitor** | `alpaca_monitor.py` | Runs inside position-monitor.yml | `#positions` (prefixed `[PAPER]`) |

**Note on naming:** `finviz_` prefix kept only where Finviz is the primary data source (`finviz_agent.py`, `finviz_weekly_agent.py`). All other agents renamed to reflect their actual data source (Alpaca, SnapTrade, etc.).

**Supporting files:**
- `utils/generate_index.py` — Generates GitHub Pages index
- `utils/calibrate_peel.py` — Per-ticker peel threshold calibration. Formula: `(close-SMA50)*close/(SMA50*ATR14)` matching TradingView "ATR% Multiple". Finds historical run peaks (continuous periods above 50MA), computes p75 as signal threshold (floor 10x), p75×0.75 as warn (floor 7.5x). CLI: `--mode positions|watchlist|all`. Runs daily (positions) and weekly (watchlist). Output: `data/peel_calibration.json`.
- `utils/analyze_mae.py` — MAE/MFE analysis from 1099-B CSV + Alpaca OHLCV. Run ad-hoc: `python utils/analyze_mae.py`. Output: `data/mae_analysis.html` + `data/mae_analysis.json`.
- `utils/archive_data.py` — Archives dated data files older than 70 days to S3 (`screener-data-repository`, `eu-central-1`). Runs in `daily-finviz.yml` before git commit. Upload → verify (`head_object`) → delete local. Never archives state files.
- `test_finviz_agent.py` — Unit tests (mocked, no API keys)
- `test_integration.py` — Integration tests for signal merge pipeline
- `test_archive.py` — Unit tests for `utils/archive_data.py` (mocked S3, no credentials needed)

**Publishing layer (`agents/publishing/`):**
- `agents/publishing/event_publisher.py` — Non-fatal EventBridge wrapper. Three functions:
  - `publish_market_daily_summary()` — fired by `market_monitor.py` at 5pm ET. No-op on X today; reserved for future Slack/Discord publisher.
  - `publish_screener_completed()` — fired by `premarket_alert.py` at 9am ET. Triggers SetupOfDay tweet with Finviz chart (reads yesterday's screener CSV, picks top Quality Score ticker).
  - `publish_persistence_pick()` — fired by `finviz_agent.py` at ~4:30pm ET (only if `persistence_days >= 3`). Triggers PersistencePick tweet with Finviz chart.
- All publish calls are wrapped in try/except — a failed EventBridge call never blocks the screener.

**Infra (CDK):**
- `infra/` — AWS CDK Python stacks deployed to `eu-central-1`
- `ScreenerInfraStack` — S3 bucket `screener-data-repository`, IAM user `finviz-screener-bot` (S3 + SSM + EventBridge permissions)
- `PublisherStack` — EventBridge custom bus `finviz-events`, XPublisher Lambda (`infra/lambdas/x_publisher/x_publisher.py`), 3 EventBridge rules (MarketDailySummary / ScreenerCompleted / PersistencePick)
- SSM namespace: `/anva-trade/` — stores X API credentials (X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET) as SecureString. Lambda reads at runtime via boto3 `get_parameters(WithDecryption=True)`.
- Lambda deps pre-installed into asset dir: `pip install -r infra/lambdas/x_publisher/requirements.txt -t infra/lambdas/x_publisher/` (packages gitignored)
- Deploy: `pip install -r infra/requirements.txt && cdk deploy --all --profile personal-090960193599`
- Admin AWS profile: `personal-090960193599` (account `090960193599`, IAM user `admin_user`)
- Account: `090960193599`

## Workflows

| Workflow | File | Trigger |
|----------|------|---------|
| Position Monitor | `position-monitor.yml` | Cron (hourly market hours) + workflow_dispatch (BUY/SELL) |
| Daily Screener | `daily-finviz.yml` | Cron + workflow_dispatch |
| Weekly Review | `weekly-finviz.yml` | Cron + workflow_dispatch |
| Market Pulse | `market-pulse.yml` | Cron (4x daily) + workflow_dispatch |
| Earnings Alert | `earnings-alert.yml` | Cron + workflow_dispatch |
| Finviz Alerts | `alerts-finviz.yml` | Cron + workflow_dispatch |
| Market Monitor | `market_monitor.yml` | Cron + workflow_dispatch |
| Pre-Market Alert | `premarket-alert.yml` | 9:00 AM ET (13:00 UTC) Mon-Fri + workflow_dispatch |
| Test Suite | `test.yml` | On push to main / PRs |

## Position Monitor — Rules Engine

The position monitor has two layers:

**Layer 1 — ATR-based (runs on every position from SnapTrade):**
- Hard stop: $-4,500 per position (SLV Feb 2026 rule)
- ATR exit: ATR multiple from SMA50 <= -1.5
- Dynamic stop: 5% base + (ATR% × 0.5). Tightens to 3% base in RED/DANGER market state.
- Peel warn/signal: per-ticker calibrated from `data/peel_calibration.json` (p75 as signal, floor 10x; p75×0.75 as warn, floor 7.5x). Falls back to ATR% tier table if ticker not calibrated: low(≤4%): 3/4x · mid(≤7%): 5/6x · high(≤10%): 6.5/8x · extreme: 8.5/10x
- AI commentary via Claude API

**Layer 2 — Minervini 6-rules engine (via `positions.json` state):**
- Rule 1: Stop loss check (positions.json stop price)
- Rule 4: No averaging down (blocks BUY if price < entry). Averaging UP merges shares + recomputes weighted avg cost, recalculates T1/T2.
- Rule 5: Gain protection — ATR trail (price − 2×ATR) from entry, silent; breakeven stop at +20%; trailing stop at +30% (10% trail)
- Rule 6: Market state gate — no entries in RED/BLACKOUT
- Target alerts: Target 1 (+20%) → sell half; Target 2 (+40%) → trail tight
- Gain fading warning: was +20%+, now <+5%

**Sizing modes** (in `trading_state.json`):
- `suspended` — 3+ consecutive losses → paper trade only
- `reduced` — 2 consecutive losses → max 5% position size
- `aggressive` — 2+ consecutive wins + GREEN/THRUST market
- `normal` — default

**Manual trade input** via workflow_dispatch: ticker, shares, price, side (BUY/SELL).

## SnapTrade Auth

- Uses SDK's signature scheme: HMAC-SHA256 over JSON `{"content": null, "path": "/api/v1/...", "query": "..."}`, base64-encoded
- Signature goes in `Signature` header; `clientId`, `timestamp`, `userId`, `userSecret` go in query params
- `SNAPTRADE_USER_ID` is a GitHub **variable** (`vars.*`), NOT a secret — all others are secrets
- SDK package `snaptrade` (v1.1.0) is deprecated but installed for reference; auth is hand-rolled matching the SDK pattern

## Secrets & Variables

| Name | Type | Used By |
|------|------|---------|
| `SLACK_WEBHOOK_URL` | secret | Daily screener, market pulse |
| `SLACK_WEBHOOK_WEEKLY` | secret | Weekly agent, winners watchlist |
| `SLACK_WEBHOOK_ALERTS` | secret | Alerts agent, earnings, failure notifications |
| `SLACK_WEBHOOK_POSITIONS` | secret | Position monitor |
| `SLACK_WEBHOOK_MARKET_ALERTS` | secret | Market monitor (state changes) |
| `SLACK_WEBHOOK_MARKET_DAILY` | secret | Market monitor (daily summary) |
| `ANTHROPIC_API_KEY` | secret | Daily agent, weekly agent, position monitor |
| `PAGES_BASE_URL` | secret | All agents (gallery links in Slack) |
| `SNAPTRADE_CLIENT_ID` | secret | Position monitor |
| `SNAPTRADE_CONSUMER_KEY` | secret | Position monitor |
| `SNAPTRADE_USER_ID` | **variable** | Position monitor |
| `SNAPTRADE_USER_SECRET` | secret | Position monitor |
| `ALPACA_API_KEY` | secret | Paper executor, paper monitor, premarket alert, market pulse |
| `ALPACA_SECRET_KEY` | secret | Paper executor, paper monitor, premarket alert, market pulse |
| `ALPACA_BASE_URL` | secret | Paper executor, paper monitor (`https://paper-api.alpaca.markets/v2`) |
| `AWS_ACCESS_KEY_ID` | secret | `archive_data.py` + `event_publisher.py` — bot key for `finviz-screener-bot` IAM user |
| `AWS_SECRET_ACCESS_KEY` | secret | `archive_data.py` + `event_publisher.py` |
| `AWS_BUCKET_NAME` | secret | `archive_data.py` (`screener-data-repository`) |
| `AWS_REGION` | secret | `archive_data.py` + `event_publisher.py` (`eu-central-1`) |

**SSM Parameters (stored in AWS, not GitHub secrets — read by XPublisher Lambda at runtime):**

| Parameter | SSM Path | Description |
|-----------|----------|-------------|
| X API Key | `/anva-trade/X_API_KEY` | X developer app consumer key |
| X API Secret | `/anva-trade/X_API_SECRET` | X developer app consumer secret |
| X Access Token | `/anva-trade/X_ACCESS_TOKEN` | X OAuth 1.0 access token |
| X Access Secret | `/anva-trade/X_ACCESS_SECRET` | X OAuth 1.0 access token secret |

## Data Files

```
data/
  positions.json                          # Rules engine state (open/closed positions, stops, targets)
  trading_state.json                      # Win/loss streaks, sizing mode, recent trades
  watchlist.json                          # Market pulse watchlist (auto-populated by screener + manual entries)
  paper_stops.json                        # Paper trade stop-loss references {ticker: {stop_price, entry_price, atr_pct, entry_date}}
  alerts_state.json                       # Breadth/F&G alert state (rolling 15-day)
  market_monitor_history.json             # Rolling 30-day breadth history
  market_monitor_YYYY-MM-DD.json          # Daily market breadth snapshot
  daily_quality_YYYY-MM-DD.json           # Q-rank, stage, section per ticker
  finviz_screeners_YYYY-MM-DD.csv         # Enriched daily screener data
  finviz_screeners_YYYY-MM-DD.html        # HTML table
  finviz_chart_grid_YYYY-MM-DD.html       # Chart gallery
  finviz_weekly_YYYY-MM-DD.html           # Weekly report
  finviz_weekly_persistence_YYYY-MM-DD.csv # Weekly signal scores
  positions_YYYY-MM-DD.json               # Position snapshots
```

## Trading Rules Encoded

| Rule | Implementation |
|------|---------------|
| Weinstein Stage 2 required | `compute_stage()` — 50MA above 200MA (sma200 > sma50), price not deeply below 50MA (sma50 > -10). Perfect alignment = also above 20MA. RVol/dist-from-high handled by Quality Score, not stage gate |
| No Stage 3/4 entries | Quality Score penalizes (-25/-40), 10% gate excludes |
| Market state conditioning | RED/BLACKOUT = no entries, CAUTION = half size, GREEN/THRUST = full |
| Dynamic stop loss | `5% + ATR% * 0.5` (3% base in RED/DANGER) — position monitor enforces |
| Hard position cap | $-4,500 per position (SLV incident Feb 2026) |
| ATR exit signal | ATR multiple from 50MA <= -1.5 (structural breakdown, not just pullback) |
| Peel (scale out) | ATR multiple from 50MA tiers: low/mid/high/extreme (warn at ~75% of signal) |
| No averaging down | Rule 4 — BUY blocked if price < existing entry |
| Averaging up | BUY on existing position when price > entry → merges shares, recomputes weighted avg, recalculates T1/T2 |
| ATR incremental trail | From entry onwards: stop = max(stop, price − 2×ATR). Silent. Disabled at +20% (breakeven takes over) |
| Breakeven stop | At +20% gain, stop moves to entry + 0.5% |
| Trailing stop | At +30% gain, 10% trail from highest price seen |
| Sizing suspension | 3 consecutive losses → paper trade only |

## Quality Score (0-100+)

- Market Cap: 0-30 pts
- Rel Volume: 0-25 pts
- EPS: 0-20 pts — uses `max(EPS Y/Y TTM, EPS Q/Q)`. Q/Q rescues spin-offs and IPOs with distorted trailing EPS (e.g. SNDK: -328% TTM vs +618% Q/Q → full 20 pts). Slack shows `EPS Q/Q*` when quarterly overrides annual.
- Inst Trans: 0-8 pts — institutional transaction change from Finviz. +8 (>10%), +5 (>3%), +2 (≥0%). Flags institutional accumulation. Shows as `Inst +X.X%` in Slack top-10.
- Multi-screener: 0-15 pts (3+ screens = 15)
- Stage 2: +25 (+10 perfect alignment), Stage 3: -25, Stage 4: -40
- VCP: +15
- Distance from high: 0-10 pts

## Market State Classification

The cycle flows directionally: RED → THRUST → CAUTION → GREEN → COOLING → CAUTION/RED → DANGER → RED

| State | Condition | Priority | Direction | Trading Action |
|-------|-----------|----------|-----------|---------------|
| BLACKOUT | Feb 1–end of Feb · Sep 1–Sep 30 | 1 | — | No new trades in Feb or Sep — both flagged as seasonally unreliable months |
| DANGER | 500+ stocks down 4%+ today AND 5d ratio < 0.5 | 2 | ↓ hard | No entries, raise stops immediately |
| COOLING | prev_state == GREEN AND GREEN conditions no longer met | 3 | ↓ fading | Trim positions, tighten stops, no new entries |
| THRUST | 500+ stocks up 4%+ today (Bonde "Very High" buying pressure) | 4 | ↑ signal | Start building watchlist NOW |
| GREEN | 5d ratio >= 2.0, 10d >= 1.5, F&G >= 35, SPY above 200d MA | 5 | ↑ bull | Full size entries |
| CAUTION | 5d ratio >= 1.5, F&G >= 25, SPY above 200d MA | 6 | ↑ recovering | Half size, build watchlist, get ready |
| RED | Everything else (SPY below 200d MA or 5d ratio < 1.0) | 7 | ↓ bear | No new trades |

## Trading Philosophy — The Rules Behind the Rules

> "Market is the ultimate master. We are not bigger than the market."

**On psychology:**
- 10% is trading mechanics. 90% is psychology — discipline, conviction, patience.
- The system is a signal layer, not a decision maker. It surfaces what the data says. The human decides.
- No system beats a trader who loses discipline. EC (equity curve) only grows with conviction trades, not over-positioning on weak setups.

**On humility:**
- Never force a trade. The market will always give another setup.
- Qullamaggie: $9K → $1M in 3.5 years. Discipline + conviction on fewer, better names. Not chasing everything.
- When the system says COOLING or CAUTION — respect it. The big losses come from ignoring the signal.
- Rules encode what we *know*. The gray zones (market stalling, direction unclear) require human judgment. The system flags them; it does not decide them.

**On what the system cannot encode:**
- V-shaped recovery vs dead-cat bounce — requires price action reading (support/resistance)
- Market character shifts — requires candlestick context, not just breadth numbers
- Stall direction — watch whether SPY breaks support or reclaims resistance. The system will show breadth; you read the tape.
- When in doubt, do nothing. Cash is a position.

**On position sizing:**
- Conviction must match position size. A half-conviction trade deserves half size or no trade.
- Averaging down is forbidden (Rule 4). Averaging up on a winner is how size grows.
- Suspension mode (3 losses) exists for a reason — respect it.

## Roadmap

- **TradingView MCP integration** — Connect Claude to TradingView desktop app via MCP when Mac Mini arrives. Goal: Claude reads charts directly, adds pattern recognition (support/resistance, VCP confirmation) to the signal layer that breadth alone cannot capture.
- **F&G zone-aware state machine** — Incorporate greed/extreme greed thresholds into COOLING and CAUTION logic (in progress, Apr 2026).

## Development Notes

- **Market breadth source:** Up/Down 4% counts come from Alpaca snapshots API (`fetch_breadth_alpaca`). Universe: NYSE+NASDAQ active equities, filtered to price > $3 and dollar vol > $250k OR volume > 100k (Bonde's filter). THRUST=500, DANGER=500 (Bonde "Very High pressure" calibration). A/D totals (`^NYADV ^NYDEC ^NAADV ^NADEC`) were removed — all four symbols are dead on Yahoo Finance as of April 2026. `breadth_source` field in daily JSON shows which source ran (`alpaca_4pct`).
- **Python version:** 3.11 on GitHub Actions, may be 3.12+ locally. Avoid f-string backslashes inside `{}` expressions (breaks on 3.11).
- **Testing:** Run `python -m unittest discover -s tests -t .` locally (227 tests, no API keys needed). Also `python -c "import agents.<module>"` to catch runtime errors. SnapTrade/Alpaca integration tests still require `gh workflow run <workflow>` + `gh run watch <id>`.
- **Finviz scraping:** Rotating user agents, exponential backoff, no proxy. Rate-limit-friendly delays between requests.
- **Weekly agent:** Uses Claude API with `web_search` tool for catalyst research (~$0.10-0.20/run).
