# Autonomous Trading Intelligence System — Feature Overview

> A fully automated, rules-based equity screening and position management platform built on proven institutional methodologies. Zero infrastructure cost. Zero discretionary emotion. Runs 24/7 on GitHub Actions.

---

## Performance (Verified from 1099 Tax Records)

| Year | Total Notional Traded | Net Realized P&L | Return on Deployed Capital | Account |
|------|-----------------------|------------------|---------------------------|---------|
| 2024 | **$418,892** | **+$47,636** | **~100%** *(gains exceeded net new capital deployed)* | Robinhood (live) |
| 2025 | **$1,608,125** | **+$42,481** | **~45%** *(on ~$90–100K starting balance; no external capital needed)* | Robinhood (live) |
| **Combined** | **~$2.03M** | **+$90,117** | | |

**Year 1 context:** Account was rebuilt from near-zero in January 2024. Net new capital injected throughout 2024: ~$44,792. Net realized gains: $47,636 — gains exceeded capital deployed, equivalent to 100%+ return on net new capital in the first active year.

**Year 2 context:** Account required no meaningful external capital in 2025. The operator net-withdrew $4,200 while the system generated $42,481 in realized gains — fully self-sustaining.

| Metric | Value |
|--------|-------|
| Max Drawdown Per Position | $4,500 (hard-coded rule) |
| Max Portfolio Drawdown (2024) | −4.9% from peak. Gains were concentrated in Nov–Dec 2024; limited exposure window. |
| Max Portfolio Drawdown (2025) | −23.5% from peak. July 2025 account peak ~$115K → Dec 2025 ~$88K. Driven by concentrated volatile positions Aug–Sep 2025. |
| Portfolio Drawdown & Recovery | Operator experienced significant portfolio drawdown during 2022 bear market — this system was built as the rules-based response to that experience. Every risk rule exists because it was tested against real losses. |
| Trading Mode | Rules-only; no human intervention on entries/exits |
| Execution | Limit orders at close price; fills at next open |

---

## 1. Intelligent Stock Screening

**What it does:** Scans the entire US equity universe daily using a multi-source market data pipeline, scores every ticker through a proprietary Quality Score, and surfaces only the highest-conviction setups.

**Daily output — 5 curated sections (115 tickers on a typical day):**
- **Stage 2 Leaders** — Weinstein Stage 2 confirmed names only. The primary trade universe.
- **IPO Lifecycle** — Recent IPOs evaluated on lifecycle metrics, not SMA rules (SMA is unreliable on young charts)
- **Momentum/Catalyst** — High relative volume + significant intraday move. 2–4 week plays.
- **Watch List** — Transitional or lower-conviction setups. Monitor, do not chase.
- **Excluded** — Tickers that triggered a filter but failed the momentum quality gate (shown for transparency)

**Key capabilities:**
- **Multi-signal fusion** — aggregates signals across 6+ independent screening criteria (breakouts, high momentum, VCP, strong fundamentals, strong technicals). Tickers appearing in 3+ signals score maximum breadth points
- **Quality Score (0–100)** — proprietary composite scoring system:
  - Market cap weight (institutional-grade names)
  - Relative volume (confirms institutional participation)
  - EPS year-over-year growth (fundamental quality gate)
  - Stage analysis alignment (Weinstein method)
  - VCP pattern detection (Minervini method)
  - Distance from 52-week high (runway check)
- **Weinstein Stage 2 gate** — automatically filters out Stage 3/4 stocks (distribution/decline). Only stocks in confirmed Stage 2 uptrends qualify for entry
- **VCP pattern bonus** — Volatility Contraction Pattern detection (+15 points) identifies institutional accumulation before breakout
- **Runs Monday–Friday**, output published to GitHub Pages as a browsable HTML report with chart gallery

---

## 2. Market Regime Classification

**What it does:** Before any trade is placed, the system classifies the current market regime across 7 states and gates all activity accordingly. No trades in bear markets, ever.

**7-State Market Model:**

| State | Condition | Trading Action |
|-------|-----------|---------------|
| BLACKOUT | Sep 1–Oct 15, Feb 1–Mar 15 | No new trades (seasonal weakness) |
| DANGER | 500+ stocks down 4%+ in a day | No entries, raise stops immediately |
| COOLING | Was GREEN, conditions fading | Trim, tighten, no new entries |
| THRUST | 500+ stocks up 4%+ in a day | Start building watchlist NOW |
| GREEN | 5d ratio ≥2.0, F&G ≥35, SPY above 200d MA | Full-size entries |
| CAUTION | 5d ratio ≥1.5, F&G ≥25, SPY above 200d MA | Half-size, watchlist building |
| RED | Everything else | No new trades |

- Breadth data sourced from Alpaca snapshots (NYSE + NASDAQ, 500k+ share universe filtered by Bonde's dollar-volume criteria)
- State changes trigger Slack alerts within minutes of market close
- **Sizes down automatically in CAUTION; blocks all entries in RED, DANGER, BLACKOUT**

---

## 3. Dynamic Risk Management

**What it does:** Every position is monitored against a multi-layer rules engine. No trade is left unmanaged.

**Layer 1 — ATR-Based Position Rules:**
- **Hard stop:** $4,500 maximum loss per position (absolute dollar cap)
- **Dynamic stop loss:** `5% base + (ATR% × 0.5)` — widens for volatile stocks, tightens in bear markets (3% base in RED/DANGER)
- **ATR exit signal:** Structural breakdown when price falls >1.5× ATR from 50-day MA — distinguishes breakdown from normal pullback
- **ATR incremental trail:** From entry, stop continuously ratchets up as price rises (`price − 2×ATR`)

**Layer 2 — Minervini-Based Rules Engine:**
- **Hard stop at entry-defined price** — every trade enters with a pre-set stop, no exceptions
- **No averaging down** — adding to a losing position is blocked. Averaging up is allowed: merges shares and recomputes weighted cost basis automatically
- **Gain protection ladder:**
  - +20% gain → stop moves to breakeven (+0.5%)
  - +30% gain → 10% trailing stop from highest price seen
- **Market state gate** — no new entries in RED or BLACKOUT regime, regardless of signal quality

**Sizing mode engine:**
- 3 consecutive losses → model portfolio only (suspension mode)
- 2 consecutive losses → max 5% position size (reduced mode)
- 2+ consecutive wins + GREEN/THRUST → aggressive mode
- Default → normal mode

---

## 4. Per-Ticker Sell Signal Calibration

**What it does:** One of the hardest problems in active trading — knowing *when to sell into strength* rather than holding until the stock rolls over. Most traders fail here. The system solves it with per-ticker calibrated sell signals rather than fixed rules.

**How it works:**
- For each held position and watchlist name, pulls 4+ years of daily OHLCV from Alpaca
- Computes **ATR% Multiple From MA** = `(price − SMA50) / (SMA50 × ATR14%)` — matches the TradingView "ATR% Multiple From MA" indicator exactly
- Detects **historical run peaks**: continuous periods where the stock trades above its 50-day MA for ≥10 consecutive days
- Calibrates **sell signal threshold** at p75 of observed peaks (floor: 10×), **early warning threshold** at 75% of signal (floor: 7.5×)
- Falls back to a global ATR tier table for stocks with insufficient history

**Result:** A stock like AAOI that historically runs to 15–18× before peaking gets a 15.8× sell signal threshold — not a generic 10×. A low-momentum stock gets tighter thresholds. The system scales out into strength, not weakness.

**Why this matters:** Most traders either sell too early (leaving money on the table) or hold too long and give back gains. This layer is the system's answer to that problem — sell signals calibrated to each stock's own historical behavior.

Calibration runs automatically: positions after daily screener, watchlist after weekly review.

---

## 5. Algo Model Validation Layer (Alpaca Integration)

**What it does:** The top-ranked candidates from the daily screening run are automatically submitted as model portfolio positions via Alpaca's broker API. This layer validates the algorithm's signal quality with real-time execution — no human decision required.

**Execution logic:**
- Evaluates top 10 tickers by Quality Score (Q≥60, Stage 2 only)
- Merges watchlist names so high-quality tracked tickers are always evaluated
- Checks market regime (no entries in RED/BLACKOUT/DANGER)
- Checks sizing mode (suspended = skip; reduced = smaller size)
- Checks averaging-down rule (no entry if price below existing position cost)
- Places **GTC limit orders at closing price** — fills at open next day
- Cancels stale GTC orders older than 2 days (prevents stale fills)
- Tracks all model positions with entry price, stop, and ATR% reference

---

## 6. Real-Time Position Monitoring

**What it does:** Monitors live brokerage positions (via SnapTrade) and model portfolio positions (via Alpaca) hourly during market hours and at open/close.

**Monitoring schedule:** Hourly 14:00–21:00 UTC + special runs at 12:00 and 22:00 UTC, Monday–Friday.

**Per-position output (Slack):**
- Current price vs. entry price and stop
- ATR multiple from 50-day MA (current reading)
- Early warning / sell signal levels (per-ticker calibrated)
- Target 1 (+20%) / Target 2 (+40%) status
- Rule violations (gain fading, approaching stop)
- AI-generated commentary (Claude API) synthesizing technicals + fundamentals

**Alert types:**
- Stop hit → EXIT alert
- Early warning / sell signal → scale-out into strength recommendation
- Gain fading → warning (was +20%, now <+5%)
- Target 1 / Target 2 hit → take profit recommendation

---

## 7. Weekly Deep-Dive Research

**What it does:** Every Sunday, runs a full weekly review with AI-powered catalyst research on every signal that surfaced during the week.

**Process:**
- Pulls all tickers that appeared in daily screening during the week
- Computes persistence score (how many days each ticker appeared)
- Runs Claude AI with web search tool to research earnings catalysts, news, sector context
- Scores and ranks by Quality Score + persistence
- Generates HTML report published to GitHub Pages
- Sends curated top picks to Slack `#weekly-alerts`

---

## 8. Pre-Market Intelligence

**What it does:** At 8:00 AM ET (before market open), delivers a pre-market briefing covering gap movers and gap risk on watched positions.

**Coverage:**
- Overnight gap analysis for all watchlist and held positions
- Volume and news context via Alpaca data API
- Formatted Slack message delivered before open

---

## 9. Market Pulse (Intraday)

**What it does:** 4× daily intraday breadth pulse (10am, 12:10pm, 2:20pm, 4:00pm ET) tracking momentum shifts in real time.

**Output:**
- Advancing vs. declining breadth
- Relative volume vs. 20-day average
- Market state inference (intraday)
- Watchlist ticker status

---

## 10. Earnings Alert System

**What it does:** Scans for earnings events on held and watched positions and alerts before reports to allow position sizing decisions.

**Coverage:**
- Earnings date detection
- Pre-earnings alert 1–5 days before
- Post-earnings gap alert (if major move detected)

---

## 11. Infrastructure & Reliability

**What it does:** Cloud-native, fully automated infrastructure requiring zero manual intervention. Scales without adding headcount or servers.

| Capability | Detail |
|------------|--------|
| **Scalability** | Horizontally scalable — adding new accounts, strategies, or clients requires configuration, not re-architecture |
| **Data storage** | Hot storage for recent data + S3 archival (cold, eu-central-1) for historical data older than 70 days |
| **Observability** | Real-time alerts for every agent run; failure notifications with direct link to the failed run |
| **Infrastructure as Code** | AWS infrastructure defined in Python CDK — reproducible, version-controlled, deployable in minutes |
| **Test coverage** | 227 unit tests, full mock coverage of all agents — changes can be validated without live API access |
| **Live reports** | Browsable HTML screener output, chart gallery, and weekly review published automatically |
| **Reliability** | Managed cloud SLA; no self-managed servers to maintain or patch |

---

## 12. Roadmap

| Item | Status |
|------|--------|
| Weinstein Stage 2 screening | Live |
| Multi-screener signal fusion | Live |
| 7-state market regime model | Live |
| Dynamic ATR stop engine | Live |
| Algo model validation layer (Alpaca) | Live |
| SnapTrade live position monitoring | Live |
| AI weekly research (Claude + web search) | Live |
| Per-ticker peel calibration | Live |
| S3 cold archival | Live |
| Power-move scan (9M+ vol + 5%+ move, Bonde method) | Planned |
| Real-money backtesting framework | Planned |
| Sector rotation layer | Planned |
| Interactive web dashboard (client self-config) | Planned |
| Multi-account support (fund-level) | Planned |

---

## Monetization Paths *(internal reference)*

| Model | Description |
|-------|-------------|
| **Turnkey system setup** | Deploy a configured instance of this system for a client — personalized risk limits, account size, stop rules, Slack channel. One-time setup + ongoing support ($5K–$25K per client) |
| **White-label license** | License the screening + risk engine to prop shops, family offices, or RIAs. Client runs it under their brand on their infrastructure |
| **Managed signal service** | Signal feed licensed to active traders or fund managers. Daily alerts, weekly research, position monitoring ($500–$2,000/mo per seat) |
| **SaaS signals tier** | Screening signals + alerts delivered via Slack/email for self-directed traders ($99–$299/mo per subscriber) |
| **Fund vehicle** | Operate as a systematic fund using the signal feed as the core strategy |

---

*System designed and operated by Anantha Srinivasan Manoharan. Performance figures sourced from 2024–2025 Robinhood 1099 tax statements. Past performance is not indicative of future results.*
