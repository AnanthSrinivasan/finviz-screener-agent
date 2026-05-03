# Autonomous Trading Intelligence System

> A fully automated, rules-based equity screening and position management platform built on proven institutional methodologies. Zero infrastructure cost. Zero discretionary emotion. Runs 24/7 on GitHub Actions.

---

## Verified Performance (from 1099 Tax Records)

| Year | Notional Traded | Net Realized P&L | Notes |
|------|----------------|-----------------|-------|
| 2024 | $418,892 | +$47,636 | Account rebuilt from near-zero Jan 2024. Net new capital injected: $44,792 — gains exceeded capital deployed. |
| 2025 | $1,608,125 | +$42,481 | No external capital needed. Operator net-withdrew $4,200 while system generated $42,481. Fully self-sustaining. |
| **Combined** | **~$2.03M** | **+$90,117** | Two years of live trading. No backtests. |

| Risk Metric | Value |
|-------------|-------|
| Max loss per position | $4,500 hard cap (absolute dollar, not %) |
| Max portfolio drawdown 2024 | −4.9% from peak |
| Max portfolio drawdown 2025 | −23.5% (Jul peak ~$115K → Dec ~$88K; driven by concentrated volatile positions Aug–Sep) |
| Execution | Limit orders at closing price; fills at next open |
| Mode | Rules-only. No human intervention on entries or exits. |

**Year 1 context:** Account rebuilt from near-zero in January 2024. Gains exceeded net new capital deployed — equivalent to 100%+ return on capital in the first active year.

**Year 2 context:** Fully self-sustaining. The operator net-withdrew capital while the system generated gains to replace it.

---

## 1. Intelligent Stock Screening

**What:** Scans the entire US equity universe every trading day and surfaces only the highest-conviction setups — so you never have to manually sort through thousands of tickers.

**Why:** The US market has 8,000+ listed equities on any given day. Without a systematic filter, even experienced traders miss the best setups or get distracted by noise. The system does the full universe scan automatically, every night, before you wake up. The output isn't just a list — it's a ranked, categorised, actionable report.

**How:**

Every ticker is scored through a proprietary **Quality Score (0–100+)**:

| Component | Max Points | What it measures |
|-----------|-----------|-----------------|
| Market Cap | 30 | Institutional-grade names — funds can actually move in and out |
| Relative Volume | 25 | Confirms real participation — institutions are moving, not just retail noise |
| EPS Growth | 20 | Uses best of Y/Y TTM or Q/Q — rescues spin-offs and IPOs with distorted trailing EPS |
| Institutional Transactions | 8 | % change in institutional ownership — flags accumulation before the move |
| Multi-screener appearances | 15 | Ticker in 3+ independent signals = maximum breadth points |
| Stage 2 alignment | +25 / +10 | Weinstein Stage 2 confirmed / perfect alignment bonus |
| VCP pattern | +15 | Volatility Contraction Pattern — institutional accumulation before breakout |
| Distance from 52w high | 10 | Runway check — not already extended |

Daily output — curated across 5 sections:

| Section | What it contains |
|---------|----------------|
| Stage 2 Leaders | Weinstein Stage 2 confirmed. The primary trade universe. |
| IPO Lifecycle | Recent IPOs scored on lifecycle metrics — SMA rules are unreliable on young charts |
| Momentum / Catalyst | High relative volume + significant intraday move. 2–4 week plays. |
| Watch List | Transitional or lower-conviction setups. Monitor, not chase. |
| Excluded | Triggered a filter but failed the quality gate — shown for full transparency |

Top setups are further refined into 6 actionable callouts:

| Callout | Criteria summary | Action |
|---------|-----------------|--------|
| 🎯 Ready to Enter | Stage 2 perfect, VCP ≥70, Q≥80, dist −1% to −10%, ATR%≤7, RVol≤1.2 | Highest-priority buy candidates |
| 🔬 Hidden Growth | EPS Q/Q or TTM >50%, institutional accumulation ≥3%, Stage 2 perfect | Research prompt — fundamental + accumulation driven |
| 🚀 Fresh Breakout | Stage 2, SMA50% in (0,25%], RVol≥1.2, ATR%≤8, Q≥70, dist 0% to −12% | Breakout-from-base — complementary to pullback-based Ready to Enter |
| ⭐ Textbook VCP | VCP≥85, appearances≥3, ATR%≤5, Stage 2 perfect, dist −3% to −15%, Q≥80 | Badge overlay — highest-confidence pattern setup |
| 💎 Power Play | Perf Month≥50% or Perf Quarter≥100%, ATR%≤5, volume drying, Stage 2 | Rare Minervini/O'Neil high tight flag pattern |
| 🏗 Base Building | Stage 2, Q≥75, dist −12% to −25%, ATR%≤7 | Watch-only — wider bases not yet ready |

---

## 2. Market Regime Classification

**What:** Before any trade is placed, the system reads the market environment and either opens the door, sizes it down, or shuts it completely. You never buy into a falling market.

**Why:** The single biggest mistake retail traders make is trading against the market — buying a good-looking stock in a bad market. 70–80% of stocks follow the broader market direction. A great setup in a bear market is still a losing trade. This layer is the system's answer to that problem — it knows when to act and, more importantly, when to do nothing.

**How:**

A **7-state market model** computed nightly from three inputs: breadth (how many NYSE+NASDAQ stocks moved >4% up or down), Fear & Greed Index, and SPY vs its 200-day MA:

| State | Condition | What the system does |
|-------|-----------|---------------------|
| BLACKOUT | September | No new trades — confirmed worst month from live 2024–2025 data (−$5,478 Sep 2025) |
| DANGER | 500+ stocks down 4%+ today | No entries. Raise stops immediately. |
| COOLING | Was GREEN, conditions fading | Trim positions, tighten stops, no new entries |
| THRUST | 500+ stocks up 4%+ today | Start building watchlist. Breadth explosion signal. |
| GREEN | 5d breadth ratio ≥2.0, F&G ≥35, SPY above 200d MA | Full-size entries |
| CAUTION | 5d ratio ≥1.5, F&G ≥25, SPY above 200d MA | Half-size entries, watchlist building |
| RED | Everything else | No new trades |

A **confidence layer** handles edge cases that a pure rules model would get wrong:

| Scenario | Behaviour | Why |
|----------|-----------|-----|
| After any THRUST day | Min state = CAUTION for 3 calendar days | Prevents whipsaw THRUST → RED-next-day flips |
| Extreme greed (F&G >74) + conditions fading | Skips COOLING buffer, RED fires immediately | No soft landing when market is at peak greed |
| Extreme fear (F&G <25) + breadth explosion from RED/DANGER | Overrides THRUST → CAUTION with `high_confidence_recovery` | Capitulation + breadth surge = high-confidence bottom |
| Normal deterioration | 2 consecutive weak days required before RED | Recovery to CAUTION always immediate — avoids false exits |

*Blackout note: February and March blackouts were removed after live 2024–2025 data showed both were profitable in both years. September blackout confirmed by Sep 2025 data. October was profitable in both years and is not blocked.*

---

## 3. Dynamic Risk Management

**What:** Every position has a pre-defined exit before the trade is placed. The system manages the stop automatically as the position gains — locking in profits as the stock moves in your favour.

**Why:** Most traders know their entry. Almost none have a real exit plan. They sell too early (small wins) or hold too long (winners become losers). The system encodes two things most traders never have: a hard rule for when you are wrong, and a mechanical process for protecting gains as you are right. Every rule here came from a real loss — the $4,500 hard cap exists because of an SLV position in February 2026.

**How:**

**Layer 1 — ATR-based rules (runs on every position, every tick):**

| Rule | Mechanism |
|------|-----------|
| Hard position cap | $4,500 maximum loss per position — absolute dollar floor, no exceptions |
| Dynamic stop loss | `5% base + (ATR% × 0.5)` — widens for volatile names, tightens to 3% base in RED/DANGER |
| ATR structural exit | Price falls >1.5× ATR from 50-day MA — distinguishes real breakdown from a normal pullback |
| Loss-cap floor | At peak ≥+5%: stop ≥ `max(entry × 0.97, entry − 0.5×ATR$)` — a meaningful winner can never fade back to a full loss |

**Layer 2 — Minervini trail (continuous, ratchets off highest price seen):**

| Peak Gain | Trail Distance | Additional Floor |
|-----------|---------------|-----------------|
| < +10% | 2.0 × ATR from peak | — |
| ≥ +10% | 1.5 × ATR from peak | — |
| ≥ +20% | 1.0 × ATR from peak | Breakeven floor activated (`entry × 1.005`). `BE` indicator on dashboard. |
| ≥ +30% | 1.0 × ATR from peak | Additional floor: `max(1.0×ATR trail, peak × 0.90)` — 10%-from-peak guard |

The trail ratchets off the **highest price seen intraday** — not hourly snapshots — so a spike between monitoring ticks doesn't lose the peak.

**Sizing mode engine** — adjusts position size based on recent performance:

| Mode | Trigger | Position sizing |
|------|---------|----------------|
| Suspended | 3 consecutive losses | Paper trades only — no live capital at risk |
| Reduced | 2 consecutive losses | Max 5% position size |
| Normal | Default | Standard sizing |
| Aggressive | 2+ consecutive wins + GREEN/THRUST | 1.25× size |

*Neutral band: ±1% result does not count as a win or loss — streak unaffected.*

---

## 4. Per-Ticker Sell Signal Calibration

**What:** Knowing when to sell into strength — before the stock rolls over — is one of the hardest problems in trading. The system solves it with sell thresholds calibrated to each stock's own historical behaviour, not generic rules.

**Why:** A fixed "sell at 10× ATR from the MA" rule treats a slow-moving consumer staple the same as a high-momentum biotech. They are completely different. A stock that historically runs to 18× before peaking shouldn't have a 10× sell signal — you'll exit too early on every trade. This layer is the system's answer to the "selling too soon vs too late" problem that most traders never solve.

**How:**

For each held position and watchlist name, the system:

1. Pulls 4+ years of daily OHLCV from Alpaca
2. Computes **ATR% Multiple from MA** = `(price − SMA50) / (SMA50 × ATR14%)` — matches the TradingView "ATR% Multiple from MA" indicator exactly
3. Detects **historical run peaks** — continuous periods trading above the 50-day MA for ≥10 consecutive days
4. Calibrates signal and warning thresholds from observed peak history:

| Threshold | Calculation | Floor |
|-----------|-------------|-------|
| Sell signal | p75 of observed historical peaks | 10× |
| Early warning | 75% of sell signal | 7.5× |

If a ticker has insufficient history, it falls back to a global ATR tier table:

| ATR% Tier | Early Warning | Sell Signal |
|-----------|-------------|-------------|
| Low (≤4%) | 3× | 4× |
| Mid (≤7%) | 5× | 6× |
| High (≤10%) | 6.5× | 8× |
| Extreme (>10%) | 8.5× | 10× |

Calibration runs automatically: positions after the daily screener, watchlist after the weekly review.

---

## 5. Algo Model Validation (Paper Trading Layer)

**What:** The top-ranked daily screening candidates are automatically submitted as paper portfolio positions via Alpaca. This validates signal quality with real execution — no human decision required.

**Why:** A signal is only useful if it can be acted on reliably. The paper trading layer proves that the screening signals translate to executable entries — at real market prices, with real slippage. It also stress-tests the rules engine (stops, sizing mode, averaging-up logic) continuously without risking live capital.

**How:**

| Step | Mechanism |
|------|-----------|
| Candidate selection | Top 10 tickers by Quality Score (Q≥60, Stage 2 only) + watchlist names merged in |
| Market state check | No entries in RED/DANGER/BLACKOUT. CAUTION = half size. GREEN/THRUST = full. |
| Sizing mode check | Suspended → skip entirely. Reduced → clamp to 25% size. Aggressive → 1.25× in GREEN/THRUST. |
| Averaging-down block | No entry if price < existing position entry price (Rule 4) |
| Order type | GTC limit order at closing price — fills at open next day |
| Stale order cleanup | GTC orders older than 2 days auto-cancelled — prevents stale fills |
| Initial stop | Set at `entry − 2×ATR$` at buy time |
| Extended entry gate | Blocked when ATR multiple from 50MA > per-ticker peel_warn threshold |

Paper positions share the same rules engine as live positions — ATR trail, breakeven crossover, T1/T2 targets, sizing mode streaks. Paper and live streak counters are independent.

---

## 6. Real-Time Position Monitoring

**What:** Monitors live brokerage positions and paper portfolio positions every hour during market hours — surfacing alerts the moment a position needs attention.

**Why:** A stop loss that nobody checks isn't a stop loss. The system watches every position continuously so you don't have to. Alerts fire the moment a position hits a stop, reaches a target, or shows a gain fading — you get the signal in Slack and decide.

**How:**

Monitoring runs hourly 14:00–21:00 UTC + dedicated open (12:00) and post-close (22:00) UTC runs, Monday–Friday.

Per-position output on every run:

| Output | Detail |
|--------|--------|
| Price vs entry / stop | Current reading against all levels |
| ATR multiple from 50-day MA | Current structural position — how far extended or contracted |
| Peel warn / signal | Per-ticker calibrated early warning and sell signal levels |
| T1 / T2 status | Target 1 (+20%) and Target 2 (+40%) — ✅ hit or ⏳ pending |
| AI commentary | Claude API synthesis of technicals + fundamentals. Non-fatal — fails silently if unavailable. |

Alert types fired to Slack:

| Alert | Trigger |
|-------|---------|
| 🚨 STOP HIT | `current_price ≤ stop_price` — position status stays active; human decides |
| 📉 Gain fading | Peak ≥+20% and price now < `highest_price_seen − 1×ATR`. 5pp dedup. |
| 🎯 T1 hit | Price reaches +20% — recommendation: sell half |
| 🎯 T2 hit | Price reaches +40% — recommendation: trail tight |
| 📊 Peel warn / signal | Per-ticker calibrated early warning or sell signal level reached |
| 🟡 Shares increased | SnapTrade reports more shares than rules engine — avg-up detected, T1/T2 recalculated |
| 🟡 Partial sell | SnapTrade reports fewer shares — sync applied, flags preserved |

**Post-close MA trail layer (22:00 UTC only):**

| ATR% Tier | Trail mechanism | Regime sensitivity |
|-----------|----------------|-------------------|
| Low (≤5%) | 21 EMA close-below (GREEN/THRUST) · 1 close below 21 EMA (CAUTION) · 8 EMA (COOLING) | Adapts to market state |
| Mid (5–8%) | 1 close below 8 EMA | — |
| High (>8%) | Close below 10% trail from `highest_price_seen` | MA can't keep up with volatile names |

Alert-only — human decides. RED/DANGER/BLACKOUT skipped (ATR stops are already tighter).

---

## 7. Weekly Deep-Dive Research

**What:** Every Sunday, a full weekly review with AI-powered catalyst research on every signal that surfaced during the week — so Monday morning you already know what to watch.

**Why:** Daily screening surfaces the setup. The weekly review adds the fundamental context: why is this stock moving, is there a real catalyst, is the sector rotating in its favour. Without this layer, you're trading price action without understanding what's driving it. That's how you get caught on the wrong side of an earnings miss or a sector reversal.

**How:**

| Step | Detail |
|------|--------|
| Signal aggregation | All tickers that appeared in daily screening during the week |
| Persistence scoring | How many days each ticker appeared — higher persistence = more conviction |
| AI catalyst research | Claude API with web search: earnings catalysts, news, sector context per ticker |
| Ranking | Quality Score + persistence combined |
| Output | HTML report (GitHub Pages) + curated top picks to Slack `#weekly-alerts` |

Additional weekly sections:

| Section | What it contains |
|---------|----------------|
| 🔭 Next on the Radar | Predictive emerging candidates: Stage 2 + Q≥70 + fresh catalyst. Excludes current Top 5 and held positions. |
| 📊 Macro context | Lifted above AI brief — environment first, then setups |
| 🏆 Top 5 | Highest-conviction names with full metrics and AI commentary |
| 📋 Full ranked table | All screened names for the week, sortable by score |

---

## 8. Pre-Market Intelligence

**What:** At 9:00 AM ET — before market open — delivers a briefing covering the top setup of the day and gap risk on watched positions.

**Why:** The first 30 minutes of trading are the most volatile and most dangerous. A position that gapped down overnight needs a different plan than one that opened flat. Having this brief before the open means decisions are made on information, not reaction.

**How:**

| Output | Detail |
|--------|--------|
| Setup of the Day | Top Quality Score ticker from previous day's screener — Finviz chart attached. Published to X/Twitter via EventBridge + Lambda pipeline. |
| Watchlist gap scan | Overnight gap analysis for all watchlist and held positions |
| Volume context | Pre-market volume vs 20-day average from Alpaca data API |
| Slack delivery | Formatted message to `#daily-alerts` before market open |

---

## 9. Market Pulse (Intraday)

**What:** 4× daily breadth pulse — 10am, 12:10pm, 2:20pm, 4pm ET — tracking momentum shifts in real time throughout the trading day.

**Why:** The market state computed at close tells you the regime. But intraday, the picture changes. A day that starts CAUTION can turn into a THRUST day by 2pm. The pulse catches that shift and gives you the signal to act — or to hold back — while the session is still live.

**How:**

| Output | Detail |
|--------|--------|
| Breadth | Advancing vs declining count, up/down 4%+ stocks vs 20d average |
| Relative volume | Market-wide RVol — confirms or denies the move |
| Intraday state inference | Current reading mapped to market state model |
| Watchlist ticker status | Price vs key levels for all tracked names |

---

## 10. Earnings Alert System

**What:** Scans for upcoming earnings events on held and watched positions — so you can size down or exit before a binary event, not after.

**Why:** Earnings are the single biggest source of overnight gap risk. A position that looks perfect on technicals can open down 20% on a miss. The system flags upcoming reports far enough in advance to make a conscious decision — hold through, reduce size, or step aside.

**How:**

| Alert type | Timing |
|------------|--------|
| Pre-earnings alert | 1–5 days before the report date |
| Post-earnings gap alert | Next morning if a major gap move is detected |

Coverage: all held positions (live + paper) and all watchlist tickers.

---

## 11. Infrastructure & Reliability

**What:** Cloud-native, fully automated infrastructure. No servers to maintain, no manual runs, no single point of failure. Runs 24/7 on GitHub Actions with S3 cold archival and AWS EventBridge for publishing.

**Why:** Most algorithmic trading systems require dedicated servers, scheduled jobs, and manual intervention when something breaks. This system runs entirely on managed cloud infrastructure — GitHub Actions handles scheduling, S3 handles storage, AWS Lambda handles publishing. If a run fails, a Slack alert fires with a direct link to the failed run log.

**How:**

| Capability | Detail |
|------------|--------|
| Scheduling | 10 agents on GitHub Actions cron. Zero self-managed servers. |
| Data storage | Hot: `data/` directory in the repo. Cold: S3 (`screener-data-repository`, `eu-central-1`). Files older than 70 days archived automatically — upload → verify → delete local. |
| Publishing | AWS EventBridge + Lambda pipeline. SetupOfDay and PersistencePick tweets with Finviz chart. Non-fatal — a failed publish never blocks the screener. |
| Infrastructure as Code | AWS infra defined in Python CDK — S3 bucket, IAM user, EventBridge bus, Lambda publisher. Reproducible and version-controlled. |
| Test coverage | 227+ unit tests. Full mock coverage — no API keys needed to run. Executes on every push to main. |
| Live reports | Screener HTML, chart gallery, weekly review, performance charts published to GitHub Pages automatically. |
| Failure alerting | Every agent run reports to Slack. Failures include a direct link to the GH Actions run log. |
| Scalability | Adding new accounts, strategies, or clients requires configuration — not re-architecture. |

---

## 12. Roadmap

| Feature | Status |
|---------|--------|
| Weinstein Stage 2 screening | ✅ Live |
| Multi-screener signal fusion | ✅ Live |
| 7-state market regime model | ✅ Live |
| Confidence layer (post-THRUST floor + F&G extremes) | ✅ Live |
| Dynamic ATR stop engine (tiered trail + loss-cap floor) | ✅ Live |
| Per-ticker peel calibration | ✅ Live |
| Algo model validation layer (Alpaca paper trading) | ✅ Live |
| SnapTrade live position monitoring | ✅ Live |
| ATR%-tiered MA trail (post-close, regime-adaptive) | ✅ Live |
| AI weekly research (Claude + web search) | ✅ Live |
| Performance charts (FIFO, equity curve, monthly P&L) | ✅ Live |
| S3 cold archival | ✅ Live |
| X/Twitter publishing (EventBridge + Lambda) | ✅ Live |
| Hidden Growth detection (character-change EPS filter) | ✅ Live |
| Fresh Breakout, Power Play, Base Building callouts | ✅ Live |
| Position transaction timeline (expandable dashboard) | ✅ Live |
| TradingView MCP integration (chart pattern recognition) | Planned — Mac Mini |
| Interactive web dashboard (client self-config) | Planned |
| Multi-account support (fund-level) | Planned |

---

## Monetization Paths *(internal reference)*

| Model | Description |
|-------|-------------|
| Turnkey system setup | Deploy a configured instance for a client — personalized risk limits, account size, stop rules, Slack channel. One-time setup + ongoing support ($5K–$25K per client) |
| White-label license | License the screening + risk engine to prop shops, family offices, or RIAs. Client runs it under their brand on their infrastructure. |
| Managed signal service | Signal feed licensed to active traders or fund managers. Daily alerts, weekly research, position monitoring ($500–$2,000/mo per seat) |
| SaaS signals tier | Screening signals + alerts delivered via Slack/email for self-directed traders ($99–$299/mo per subscriber) |
| Fund vehicle | Operate as a systematic fund using the signal feed as the core strategy |

---

*Designed and operated by Anantha Srinivasan Manoharan. Performance figures sourced from 2024–2025 Robinhood 1099 tax statements. Past performance is not indicative of future results.*
