# CLAUDE_CONTEXT.md — Finviz Screener Agent

Paste this at the start of any new Claude conversation to restore full project context.

## What This Is

Automated stock screening + position monitoring system. Scrapes Finviz daily, scores tickers using Weinstein Stage Analysis + quality metrics, monitors open positions via SnapTrade, and sends alerts to Slack. Runs entirely on GitHub Actions.

**Repo:** `AnanthSrinivasan/finviz-screener-agent` (branch: `main`)
**Stack:** Python 3.11, GitHub Actions, Finviz scraping, yfinance, SnapTrade API, Claude API, Slack webhooks
**Dependencies:** `requests`, `beautifulsoup4`, `pandas`, `snaptrade`, `yfinance`

## Architecture — 8 Agents

### Agent 1: Daily Screener (`finviz_agent.py`)
- **Schedule:** 21:30 UTC Mon-Fri (`daily-finviz.yml`)
- **5 screeners:** 10% Change, Growth, IPO, 52 Week High, Week 20%+ Gain
- Fetches snapshot metrics (ATR%, EPS, SMA distances, Rel Vol, 52w high dist)
- Computes: Weinstein Stage (0-4), VCP detection, Quality Score (0-100)
- **10% Change momentum gate:** tickers from this screener must pass 4 checks (above 50d MA, RVol >= 1.5x, within 30% of 52w high, not Stage 3/4) or get excluded
- Classifies into sections: `stage2`, `ipo`, `momentum`, `watch`, `excluded`
- Generates chart grid HTML + AI summary via Claude API
- **Outputs:** `data/finviz_screeners_*.csv`, `data/finviz_chart_grid_*.html`, `data/daily_quality_*.json`

### Agent 2: Weekly Review (`finviz_weekly_agent.py`)
- **Schedule:** 18:00 UTC Sunday (`weekly-finviz.yml`)
- 3-agent pipeline: Signal Score Calculator → Catalyst Research (Claude + web_search) → Synthesiser
- Persistence scoring: days_seen, screener diversity, EP/IPO signals, character change detection
- Character change = 3+ quarters improving EPS + accelerating sales (checked via yfinance)
- Watch/excluded tickers filtered from top 5, catalyst research, and AI brief
- **Outputs:** `data/finviz_weekly_*.html`, `data/finviz_weekly_persistence_*.csv`

### Agent 3: Market Monitor (`finviz_market_monitor.py`)
- **Schedule:** 22:00 UTC Mon-Fri (`market_monitor.yml`)
- 6 breadth screens: up/down 4% today, up/down 25% quarter, above 40d MA, total universe
- Classifies market state: `THRUST` / `GREEN` / `CAUTION` / `DANGER` / `RED` / `BLACKOUT`
- Fires Slack alert on state change only; daily summary always
- **Outputs:** `data/market_monitor_*.json`, `data/market_monitor_history.json` (rolling 30-day)

### Agent 4: Position Monitor (`finviz_position_monitor.py`)
- **Schedule:** Hourly 14:00-21:00 UTC + pre-market 12:00 + after-hours 22:00 UTC Mon-Fri (`position-monitor.yml`)
- Pulls live positions from SnapTrade, fetches Finviz metrics
- **Alert hierarchy:** Hard stop ($-4,500) → ATR exit (-1.5x) → Stop loss (dynamic %) → Warnings → Peel signals
- **Dynamic stop formula:** `5% base + ATR% * 0.5`
- AI commentary via Claude API on position health

### Agent 5: Alerts (`finviz_alerts_agent.py`)
- **Schedule:** 22:00 UTC Mon-Fri (`alerts-finviz.yml`)
- Tracks: Fear & Greed extremes, NYSE/Nasdaq breadth, ATR compression, commodity breakouts
- Stateful via `data/alerts_state.json` (rolling 15-day windows)

### Agent 6: Earnings Alert (`finviz_earnings_alert.py`)
- **Schedule:** 22:30 UTC Mon-Fri (`earnings-alert.yml`)
- Filters enriched daily CSV: Quality Score > 50, core sectors, 7-day earnings window

### Agent 7: Market Pulse (`finviz_market_pulse.py`)
- **Schedule:** 4x daily during market hours (`market-pulse.yml`) — 10am, 12:10pm, 2:20pm, 4pm ET
- Reads `data/watchlist.json`, fetches price + 10/21 EMA via yfinance
- Alerts on: 2% EMA proximity, stop hits, new highs vs prev close
- Silent when nothing actionable — no Slack message at all

### Agent 8: Winners Watchlist (`finviz_winners_watchlist.py`)
- **Schedule:** Monday evenings (needs workflow or manual trigger)
- Reads weekly persistence CSV top 5, filters out existing watchlist tickers, appends survivors
- No AI calls, under 60 lines

## Trading Rules Encoded

| Rule | Implementation |
|------|---------------|
| Weinstein Stage 2 required | `compute_stage()` — price above all 3 MAs, stacked, RVol >= 1.0, within 25% of high |
| No Stage 3/4 entries | Quality Score penalizes (-25/-40), 10% gate excludes |
| Market state conditioning | Weekly agent sizes recs: RED/BLACKOUT = watch only, CAUTION = half size, GREEN/THRUST = full |
| Dynamic stop loss | `5% + ATR% * 0.5` — position monitor enforces |
| Hard position cap | $-4,500 per position (SLV incident Feb 2026) |
| ATR exit signal | ATR multiple from MA <= -1.5 |
| Peel (scale out) | ATR multiple tiers: low/mid/high/extreme |
| 10% Change filter | Must pass: above 50d MA, RVol >= 1.5x, within 30% of 52w high, not Stage 3/4 |
| Character change | 3+ quarters improving EPS + accelerating sales |
| VCP detection | Stage 2 + tight ATR + volume dry-up + pullback from high |

## Current Watchlist (`data/watchlist.json`)

| Ticker | Status | Entry | Stop | Thesis |
|--------|--------|-------|------|--------|
| CRCL | watching | — | $100 | CLARITY Act Apr 3, stablecoin, IPO lifecycle |
| CORZ | watching | — | $14 | CoreWeave merger, AI infra pivot, crypto |
| **FIGS** | **entered** | **$15.60** | dynamic | Character change, 400% EPS beat, healthcare |
| SEDG | watching | — | $38 | Character change, European solar, Iran energy |
| **PLAB** | **entered** | **$42.94** | dynamic | Photomask leader, semiconductor capex |
| SNDK | watching | — | — | IPO lifecycle, 81% EPS beat, NAND cycle |

## Market State (as of 2026-03-24)
- **State: RED** — "No new trades"
- F&G: 16.1 (Extreme Fear)
- SPY: $655.38, below 200d MA

## Slack Channels (6 webhooks)

| Secret | Used By |
|--------|---------|
| `SLACK_WEBHOOK_URL` | Daily screener, market pulse, winners watchlist |
| `SLACK_WEBHOOK_WEEKLY` | Weekly agent |
| `SLACK_WEBHOOK_ALERTS` | Alerts agent, earnings, failure notifications |
| `SLACK_WEBHOOK_MARKET_ALERTS` | Market monitor state changes |
| `SLACK_WEBHOOK_MARKET_DAILY` | Market monitor daily summary |
| `SLACK_WEBHOOK_POSITIONS` | Position monitor |

## Key Data Files

| File | Written By | Read By |
|------|-----------|---------|
| `daily_quality_*.json` | Daily agent | Weekly agent, market pulse |
| `market_monitor_*.json` | Market monitor | Market pulse, weekly agent |
| `market_monitor_history.json` | Market monitor | Market monitor (rolling 30d) |
| `alerts_state.json` | Alerts agent | Alerts agent (stateful) |
| `finviz_weekly_persistence_*.csv` | Weekly agent | Winners watchlist |
| `watchlist.json` | Manual / winners watchlist | Market pulse |

## Quality Score Breakdown (0-100)
- Market Cap: 0-30 pts
- Rel Volume: 0-25 pts
- EPS Y/Y TTM: 0-20 pts
- Multi-screener: 0-15 pts (3+ screens = 15)
- Stage 2: +25 (+10 perfect alignment), Stage 3: -25, Stage 4: -40
- VCP: +15
- Distance from high: 0-10 pts

## Do NOT Touch
- Existing daily/weekly/monitor/alerts agents' core logic without explicit request
- HTML templates and CSS in `generate_finviz_gallery()`
- GitHub Pages index generation (`generate_index.py`)
- SnapTrade auth flow in position monitor

## Known Issues / Gaps
- `finviz_winners_watchlist.py` has no GitHub Actions workflow yet (was removed in `33020e9`)
- Market state is RED — all new entries are against the system's own rules (user is aware)
- Finviz scraping uses rotating user agents but no proxy; rate limiting handled with exponential backoff
- Weekly agent uses Claude API with `web_search` tool for catalyst research — costs ~$0.10-0.20/run
