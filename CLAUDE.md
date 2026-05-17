# Finviz Screener Agent

## MANDATORY — Read before every action

1. **Read memory FIRST.** Open `MEMORY.md` and every file it references in `/Users/sananth/.claude/projects/-Users-sananth-Documents-Mac-Backup-Languages-Python-finviz-screener-agent-new/memory/` BEFORE the first non-read action of every session, BEFORE writing a spec, and BEFORE executing tasks. The MEMORY.md auto-loaded into the prompt is the index — you must actually read the linked files (user prefs, feedback, project state) not just the index. If you skip this step, you will repeat past mistakes the user already corrected.
2. Always `git pull --rebase origin main` before `git push` — Actions commits data files back constantly.
3. After any screener/agent logic change: run the relevant GH Actions workflow and verify the logs, not just unit tests.
4. Run `python -m unittest discover -s tests -t .` before every push.
5. When you learn something new about the user's preferences or the project (especially after a correction), save it to memory as the FIRST step of the response, not the last.

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
| Weekly Review | `finviz_weekly_agent.py` | 10:00 UTC Saturday | `#weekly-alerts` — adds 🎯 Re-entry Setup (21 EMA pullback lane) block when actionable |
| Market Monitor | `market_monitor.py` | 21:00 UTC Mon-Fri | `#market-alerts` (state changes + THRUST) |
| Position Book | `position_monitor.py` (`BOOK_RUN=1`) | 13:15 / 14:30 / 17:30 UTC Mon-Fri (3x daily) | `#positions` — consolidated table |
| Position Critical | `position_monitor.py` | Every 30 min 14:00-21:00 UTC Mon-Fri | `#positions` — only when a critical event fires |
| Alerts | `alerts_agent.py` | 21:00 UTC Mon-Fri | `#general-alerts` |
| Earnings Alert | `earnings_alert.py` | 21:30 UTC Mon-Fri | `#general-alerts` |
| Market Pulse | `market_pulse.py` | 4x daily (10am, 12:10pm, 2:20pm, 4pm ET) | `#daily-alerts` |
| Winners Watchlist | `winners_watchlist.py` | Monday evenings | `#weekly-alerts` |
| **Paper Executor** | `alpaca_executor.py` | After Daily Screener (workflow_run) + manual | `#daily-alerts` (BUY placements + summary only) |
| **Paper Monitor** | `alpaca_monitor.py` | Runs inside position-monitor.yml | `#positions` (prefixed `[PAPER]`) |
| Sector Rotation | `agents/sector_rotation.py` | 21:15 UTC Mon-Fri (Slack on Mon/Thu) | `#daily-alerts` |

**Note on naming:** `finviz_` prefix kept only where Finviz is the primary data source (`finviz_agent.py`, `finviz_weekly_agent.py`). All other agents renamed to reflect their actual data source (Alpaca, SnapTrade, etc.).

**Shared rules engine — `agents/trading/rules.py`** (used by BOTH paper monitor and live position_monitor):
- `apply_position_rules()` — per-tick continuous ATR-tiered trail. **Trail ratchets off `highest_price_seen`** (intraday-aware, immune to hourly-snapshot peak gaps — the VIK Apr-2026 regression). Tier ladder by `peak_gain_pct`: `<10%` → 2.0×ATR · `≥10%` → 1.5×ATR · `≥20%` → **1.25×ATR if atr_pct ≤ 5%, else 1.0×ATR** (low-vol names get one extra quarter-ATR breathing room at the lock tier — May 2026). **Loss-cap floor** at peak ≥ +5%: `stop ≥ max(entry × 0.97, entry − 0.5×ATR$)` — hybrid α/β, vol-aware for low-vol names with -3% ceiling for high-vol. **Breakeven crossover** at peak ≥ +20% sets `breakeven_activated` flag (drives Slack/dashboard `BE` indicator) and floors stop at `entry × 1.005` as a fallback when ATR data missing — no longer gates the trail. **+30% floor** = `max(1.25/1.0×ATR trail, peak × 0.90)` — the 10%-from-peak guard kicks in only for >10% ATR names where ATR trail goes looser. T1/T2 alerts. 1×ATR fade alert. **Returns structured events** (`{kind, ticker, message, ...}`) — caller forwards `message` to Slack and may key side effects off `kind`.
- `price_above_sma5(closes, current_price)` — helper used by monitors: True when price ≥ SMA(last 5 daily closes). Callers use this to suppress premature stop exits on low-ATR names when the short-term trend is still intact. Returns False when fewer than 5 closes available (don't suppress).
- `check_ma_trail_alert()` — Layer 1b ATR%-tiered MA trail (alert-only): low-vol (≤5%) regime EMA close-below (21 EMA GREEN/THRUST/CAUTION, 8 EMA COOLING); mid-vol (5-8%) 8 EMA close-below; high-vol (>8%) 10% pct trail from peak. RED/DANGER/BLACKOUT skipped. Caller passes daily closes.
- `update_sizing_mode()` / `record_trade_result()` — streak → mode transitions and recent_trades append. Neutral band `|result_pct| < 1.0%` does not bump streaks. `record_trade_result` accepts optional `profit_loss_usd` and returns the result label. `recent_trades` capped at 30 entries (shared constant `RECENT_TRADES_CAP`).

**Supporting files:**
- `utils/generate_index.py` — Generates GitHub Pages index
- `utils/calibrate_peel.py` — Per-ticker peel threshold calibration. Formula: `(close-SMA50)*close/(SMA50*ATR14)` matching TradingView "ATR% Multiple". Finds historical run peaks (continuous periods above 50MA), computes p75 as signal threshold (floor 10x), p75×0.75 as warn (floor 7.5x). CLI: `--mode positions|watchlist|all`. Runs daily (positions) and weekly (watchlist). Output: `data/peel_calibration.json`.
- `utils/analyze_mae.py` — MAE/MFE analysis from 1099-B CSV + Alpaca OHLCV. Run ad-hoc: `python utils/analyze_mae.py`. Output: `data/mae_analysis.html` + `data/mae_analysis.json`.
- `utils/archive_data.py` — Archives dated data files older than 70 days to S3 (`screener-data-repository`, `eu-central-1`). Runs in `daily-finviz.yml` before git commit. Upload → verify (`head_object`) → delete local. Never archives state files.
- `utils/dedupe_watchlist.py` — One-time migration. Deduplicates `data/watchlist.json` by keeping highest-priority row per ticker (entry-ready > focus > watching > archived), merging earliest `added`/`focus_promoted_date`. Run once after code fix; runtime lifecycle prevents future dupes. `python utils/dedupe_watchlist.py` (dry-run) · `--apply` to write.
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
| Position Book | `position-book.yml` | Cron 13:15 / 14:30 / 17:30 UTC Mon-Fri (`BOOK_RUN=1`) + workflow_dispatch (BUY/SELL) |
| Position Critical | `position-critical.yml` | Cron `*/30 14-21 UTC` Mon-Fri — fires immediate Slack on stop_hit / auto_close / share_drift / T1 / T2 / hard_stop only |
| Daily Screener | `daily-finviz.yml` | Cron + workflow_dispatch |
| Weekly Review | `weekly-finviz.yml` | Cron + workflow_dispatch |
| Market Pulse | `market-pulse.yml` | Cron (4x daily) + workflow_dispatch |
| Earnings Alert | `earnings-alert.yml` | Cron + workflow_dispatch |
| Finviz Alerts | `alerts-finviz.yml` | Cron + workflow_dispatch |
| Market Monitor | `market_monitor.yml` | Cron + workflow_dispatch |
| Pre-Market Alert | `premarket-alert.yml` | 9:00 AM ET (13:00 UTC) Mon-Fri + workflow_dispatch |
| Sector Rotation | `sector-rotation.yml` | Cron 21:15 UTC Mon-Fri + workflow_dispatch |
| Test Suite | `test.yml` | On push to main / PRs |

## Position Book / Critical Slack split (May 2026)

The position monitor no longer posts a per-event hourly status block. It now
splits Slack output into two streams:

- **Book post** (`BOOK_RUN=1`, 3x daily at 13:15 / 14:30 / 17:30 UTC). One
  consolidated table with TK / Avg / Now / Move% / Peak% / Stop / $P/L / STATE
  per position, plus an `🚨 ACTIONS TODAY` block (top-priority
  TRIM/ROUND-TRIP/STOP-NEAR/STOPPED) and an `📋 EVENTS SINCE LAST POST` digest
  of inter-post events. Replaces ~6 alert types × 9 positions × hourly noise.
  Strategy-readable in 10 seconds.
- **Critical post** (every 30 min during market hours, no `BOOK_RUN`). Fires
  ONLY when a critical event hits — `stop_hit`, `auto_closed`,
  `share_drift_avg_up`, `share_drift_partial_sell`, `target1`, `target2`, or
  `hard_stop`. Each event is a one-shot Slack message. The same event is also
  appended to `data/book_last_post.json` so the next book post acknowledges it
  in the EVENTS DIGEST footer.

`agents/trading/rules.py` exports `CRITICAL_EVENT_KINDS`. The router lives in
`agents/trading/position_monitor.py` (Step 14b–c, Book/critical router) and
the table renderer is `agents/trading/book_table.py`. Digest log file:
`data/book_last_post.json`.

State map (book post `STATE` column):

| State | Trigger |
|---|---|
| `🔻 STOPPED` | stop_hit / auto_closed / hard_stop fired this run |
| `🚨 STOP NEAR` | `abs(price − stop) / price < 0.5%` |
| `⚠ TRIM` | peak ≥ 25% AND price gave back > 10pp from peak AND target1_hit (more specific than ROUND-TRIP — evaluated first) |
| `🚨 ROUND-TRIP` | peak ≥ 15% AND price gave back > 18pp from peak (no T1 lock yet) |
| `✓ HOLD` | default |

## Position Monitor — Rules Engine

The position monitor has two layers:

**Layer 1 — ATR-based (runs on every position from SnapTrade):**
- Hard stop: $-4,500 per position (SLV Feb 2026 rule)
- ATR exit: ATR multiple from SMA50 <= -1.5
- Dynamic stop: 5% base + (ATR% × 0.5). Tightens to 3% base in RED/DANGER market state.
- Peel warn/signal: per-ticker calibrated from `data/peel_calibration.json` (p75 as signal, floor 10x; p75×0.75 as warn, floor 7.5x). Falls back to ATR% tier table if ticker not calibrated: low(≤4%): 3/4x · mid(≤7%): 5/6x · high(≤10%): 6.5/8x · extreme: 8.5/10x
- AI commentary via Claude API

**Layer 1b — ATR%-tiered, regime-adaptive MA trail (runs post-close only, 22:00 UTC):**
- Fetches last 30 daily bars from Alpaca per held position
- Trail signal picked by ATR% tier (high-vol stocks need a $-floor, not an MA):
  - **ATR% ≤ 5%** (low-vol) → regime-adaptive EMA close-below:
    - GREEN / THRUST → 2 closes below **21 EMA** (Qullamaggie, give room)
    - CAUTION → 1 close below 21 EMA
    - COOLING → 1 close below **8 EMA**
  - **5% < ATR% ≤ 8%** (mid-vol) → 1 close below **8 EMA**
  - **ATR% > 8%** (high-vol) → close below **10% trail from `highest_price_seen`** (FLY/PL class — MA can't keep up)
  - RED / DANGER / BLACKOUT → skipped (existing ATR stops tighter)
- Non-exit: fires Slack alert only, human decides. Dedup via `ma_trail_alerted_date`

**Layer 2 — Minervini 6-rules engine (via `positions.json` state):**
- Rule 1: Stop loss check (`positions.json` `stop_price`) — alert only; `status` stays "active".
- Rule 4: No averaging down (blocks BUY if price < entry). Averaging UP merges shares + recomputes weighted avg cost, recalculates T1/T2.
- Rule 5: Gain protection — continuous ATR-tiered trail off `highest_price_seen` (2.0/1.5/1.0× ATR by peak +0/+10/+20). Hybrid +5% loss-cap floor `max(entry × 0.97, entry − 0.5×ATR$)`. Breakeven crossover flag at peak +20% (with `entry × 1.005` fallback floor when ATR missing). +30% floor `max(1.0×ATR trail, peak × 0.90)`. Trail ratchets off recorded peak so hourly-snapshot gaps don't lose intraday peaks (VIK Apr-2026 regression)
- Rule 6: Market state gate — no entries in RED/BLACKOUT
- Target alerts: Target 1 (+20%) → sell half; Target 2 (+40%) → trail tight. T1/T2 status (✅/⏳) shown in every daily summary; daily reminder while T1 locked and T2 pending
- Gain fading warning: `peak_gain_pct ≥ +20% AND current_price < highest_price_seen − 1×ATR`. Every-run alert with 5pp dedup. ATR-normalized so volatile names aren't choked
- `highest_price_seen` and `peak_gain_pct` use Finviz intraday "Range" high (fixes missed-intraday-peak bug where hourly snap missed spikes between ticks)

**Stop hit fires alert only — no status mutation.** When live engine detects `current_price <= stop_price`, it emits the 🚨 STOP HIT Slack alert and logs at WARNING. Position `status` stays `"active"` (user often holds through the alert — system signals; human decides). Persisted state schema: `stop_price` and `breakeven_activated` (renamed from legacy `stop` / `breakeven_stop_activated` in the Apr 29 2026 port; one-shot migration in `utils/migrate_positions_keys.py`).

**Share-drift reconcile (ticker in both, share counts differ):**
- Avg-up (SnapTrade > rules): trust SnapTrade weighted `avg_cost`, recompute T1/T2, reset `target1_hit` + `breakeven_activated`. Alert: 🟡 SHARES INCREASED.
- Partial sell (SnapTrade < rules): sync `shares` only; entry/T1/T2/flags untouched. Alert: 🟡 PARTIAL SELL.

**Recent events feed (`data/recent_events.json`):** rolling last 50 events written by agents. **Market events only** (state transitions, breadth thresholds). Dashboard "Recent Alerts" widget reads this. Severity color-bar: high/med/low (red/amber/green left border). Helper `_append_recent_event()` — called from `market_monitor.py` on state change. Position events (stop_hit, breakeven, target hits, position close) deliberately do NOT write here — those go to Slack only (Apr 29 2026: removed prior position-event writes per user spec).

**Position history cache (`data/position_history.json`):** every run pulls 90d of BUY+SELL activities grouped by ticker. Dashboard renders expandable per-row transaction timeline (chevron click) showing avg-up / partial sell / full close with running cost basis.

**Retro-patch lagged fills:** every run, closed positions in last 14 days with `close_source ∈ {fallback_high, user_reported_breakeven, live_quote}` get re-checked against SnapTrade activities. Real fill → patched in (close_price/result_pct/source). Adjusts total_wins/losses on result-type flip. Solves 24-48h broker sync lag. `live_quote` added Apr 30 2026 after NVDA/MU/CORZ/NBIS got stuck on Finviz quote estimates because the SnapTrade activities API didn't return the SELL at close-detection time.

**Auto-close (positions in positions.json gone from SnapTrade):**
- Real exit price priority: SnapTrade SELL fill (via `/accounts/{id}/activities`) > live Finviz quote > `highest_price_seen` (last-resort fallback)
- Neutral band: `|result_pct| < 1.0%` → tagged BREAKEVEN, **does not** bump consecutive_wins/losses or total_wins/losses (sizing mode unaffected). `recent_trades.result = "neutral"`.
- Slack alert tags fill source: `(fill)`, `(quote)`, or `(peak — fill unavailable)`. `close_source` field persisted on closed position.

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
  paper_stops.json                        # Paper trade state: {ticker: {stop_price, entry_price, atr_pct, entry_date, highest_price_seen, peak_gain_pct, breakeven_activated, target1, target2, target1_hit, pending_close}}
  book_last_post.json                     # Position-book digest log {last_book_post_ts, events_since_last: [{kind, ticker, message, ts, ...}]}. Accumulated by position_monitor.py between book runs; cleared on each book post.
  paper_trading_state.json                # Paper streaks/sizing — independent from live trading_state.json. Same schema (consecutive_wins/losses, current_sizing_mode, recent_trades). Drives executor's size_mul + suspended block.
  hidden_growth.json                      # Today's Hidden Growth candidates (overwritten daily) — {date, candidates: [{ticker, signal_score, criteria, eps_yy_ttm, eps_qq, inst_trans, appearances}]}
  rs_leaders.json                         # RS Leader persistent tracker — {ticker: {first_triggered, trigger_state, trigger_q, trigger_dist, trigger_atr_mult, rs_rating, current_status, last_active_date, pullback_started, reacquired_dates, days_tracked}}
  alerts_state.json                       # Breadth/F&G alert state (rolling 15-day)
  market_monitor_history.json             # Rolling 30-day breadth history
  market_monitor_YYYY-MM-DD.json          # Daily market breadth snapshot
  daily_quality_YYYY-MM-DD.json           # Q-rank, stage, section per ticker
  finviz_screeners_YYYY-MM-DD.csv         # Enriched daily screener data
  finviz_screeners_YYYY-MM-DD.html        # HTML table
  finviz_chart_grid_YYYY-MM-DD.html       # Chart gallery (sector rotation panel + click-to-filter + Base Building + Watchlist tiers)
  finviz_weekly_YYYY-MM-DD.html           # Weekly report
  finviz_weekly_persistence_YYYY-MM-DD.csv # Weekly signal scores
  positions_YYYY-MM-DD.json               # Position snapshots
  sector_etf_map.json                     # ETF universe (sectors + thematics + benchmarks) — hand-curated
  ticker_sector_map.json                  # Held-ticker → ETF map (with Finviz-sector fallback)
  sector_rotation_YYYY-MM-DD.json         # Daily ETF RS snapshot + dispersion + regime
  sector_rotation_history.json            # Rolling 180d of {date,etf,rs_score,rank,is_20d_rs_high,ret_1d}
```

## Sector Rotation Tracker (`agents/sector_rotation.py`)

Daily ~33-ETF RS snapshot. Pulls bars from Alpaca, computes 1d/5d/20d returns + ret-vs-SPY, percentile-ranks 20d-vs-SPY into a 0–99 RS score, ranks the universe. History layer (`sector_rotation_history.json`, 180d) drives:

- `rank_delta_5d` — change vs 5 trading days ago
- `decay_streak_days` — consecutive worsening-rank days while `rs_score < 50`
- `is_20d_rs_high` + `anticipation_confirmed` — today's relative-perf is 20d high AND yesterday's was too (2-day confirmation)

**Trend signals:**
- Leadership IN: `rank_delta_5d <= -10 AND rs_score >= 70`
- Leadership decay (OUT): `rank_delta_5d >= +10 AND rs_score < 50`
- Anticipation (confirmed): `is_20d_rs_high AND rs_score < 60` for 2 consecutive days

**Regime classification** (cycle context — informational only):
- `bootstrapping` (history <20 days — percentile not yet meaningful; falls back to neutral action block)
- `correlation_phase` (dispersion p<20)
- `early-rotation` (p20–p50, narrow leadership)
- `mid-rotation` (p50–p80)
- `late-rotation` (p≥80)
- `blow-off-risk` (p≥80 AND SPY at 20d high)

**Regime → action map** (Phase 1, informational; `REGIME_ACTIONS` in `agents/sector_rotation.py`). Each regime tag maps to a Slack action block (headline + sizing/entries/held lines), injected into every Slack post beneath the phase line.

| Regime | Headline | Sizing | Entries | Held |
|---|---|---|---|---|
| `correlation_phase` | Beta tape — no sector edge | Size down. Trade SPY/QQQ if anything. | No new sector entries. | Hold, no adds. |
| `early-rotation` | Leadership forming | Normal size. | Build watchlist in emerging RS leaders. Wait 5d confirm. | Hold. |
| `mid-rotation` | Best entry tape | Full size in GREEN/THRUST · half in CAUTION. | Press confirmed RS leaders. | Add to leaders, hold others. |
| `late-rotation` | Leadership narrowing | Reduce new-entry size 50%. | Fresh RS-rising leaders only. Skip extended names. | Trim names ≥+25% from entry. No adds. |
| `blow-off-risk` | Risk-off | No new entries. | Skip all entries. | Tighten stops · trim aggressively · cash is a position. |

**Phase 2 (deferred — 4-week validation gate):** wire `blow-off-risk` to block paper executor entries, `late-rotation` to halve `size_mul`, position monitor TIGHTEN-STOPS alert on `blow-off-risk`, regime-transition Slack post on day-over-day flips.

**Slack:** posted Mon/Thu at 21:15 UTC; other weekdays still write the snapshot + update history but skip Slack.

**Held-ticker → ETF resolution** (`agents/utils/sector_lookup.py`):
1. Explicit map at `data/ticker_sector_map.json` (e.g. AAOI/AMAT/LSCC/NVDA → SMH).
2. Fallback to caller-provided Finviz `Sector` string via `FINVIZ_SECTOR_TO_ETF` (e.g. "Healthcare" → XLV).
3. None → caller skips sector signal for that position.

**Pending integrations** (per spec rollout §13 — wire in after observing signal quality): position monitor SECTOR ROTATING OUT alert on `decay_streak_days >= 2`, paper executor lagging-sector annotation, weekly agent rotation section, `rotation.html` dashboard tab.

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
| Entry gate (extended) | `alpaca_executor.py` blocks new entry when ATR multiple > per-ticker `peel_warn` (from `peel_calibration.json`). Falls back to ATR% tier warn when ticker is uncalibrated. Slack notes `calibrated` vs `tier` source. |
| Paper market-state gate | `alpaca_executor.py` reads `market_state` from `market_monitor_history.json` (single source of truth — replaced the old SPY/SMA200 check). RED/DANGER/BLACKOUT → no buys, but still posts a Slack alert listing top-5 would-have candidates ("your call"). CAUTION/COOLING → half size. GREEN/THRUST → full. Sizing mode overlays: `suspended` blocks entirely, `reduced` clamps size_mul ≤ 0.25, `aggressive` boosts to 1.25× in GREEN/THRUST. |
| No averaging down | Rule 4 — BUY blocked if price < existing entry |
| Averaging up | BUY on existing position when price > entry → merges shares, recomputes weighted avg, recalculates T1/T2 |
| Initial stop (paper) | `alpaca_executor.py` sets stop = `entry − 2×ATR$` at buy time |
| Loss-cap floor | At peak ≥ +5%: stop ≥ `max(entry × 0.97, entry − 0.5×ATR$)` — hybrid α/β. Caps fade-back-to-loss after any meaningful win |
| ATR-tiered trail | Continuous, ratchets off `highest_price_seen`. Tiers by `peak_gain_pct`: <10% → 2.0×ATR, ≥10% → 1.5×ATR, ≥20% → **1.25×ATR if atr_pct ≤ 5% else 1.0×ATR**. No freeze, no dead zone |
| Breakeven crossover | At peak ≥ +20%: `breakeven_activated` flag set (Slack/dashboard `BE` indicator). Floor `entry × 1.005` applies when ATR data missing — otherwise the 1.25/1.0×ATR trail is already above entry |
| Trailing stop floor | At peak ≥ +30%: stop ≥ `max(1.25/1.0×ATR trail, peak × 0.90)`. The 10%-from-peak guard kicks in only for >10% ATR names where ATR trail is looser than 10% |
| SMA5 stop filter | For low-ATR names (≤5%): if `price >= SMA(last 5 closes)` when stop is triggered, exit is suppressed for that run — trend still intact. Paper skips sell; live skips alert. |
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

## Daily Screener Signals (Slack blocks)

Two actionable callouts in the daily Slack message, ordered by urgency:

**🎯 Ready to Enter** — top-of-message, top 5 by Quality Score. All must pass:
Stage 2 perfect · VCP conf ≥70 · Q ≥80 · dist from 52w high -1% to -12% ·
ATR% ≤7% · RVol ≤1.2 · not in `positions.json` open positions.
(Dist gate softened from -10% → -12% May 2026 — MTSI/RMBS class missed by 0.02-0.33pp.) Each line shows
metrics + `/stock-research <ticker>`. Also drives the `focus → entry-ready`
watchlist promotion (same criteria, pure `_is_ready_to_enter` predicate).

**🔬 Hidden Growth (3+/6 or 4+/6 criteria)** — research prompt, **no cap** (score is the
filter; count signals regime health). Scans `summary_df` (pre-10%-gate) so
deep-base breakouts aren't filtered out. Criteria: persistence (3+ screens),
EPS Y/Y TTM >50, EPS Q/Q >50 (or Q/Q>20 when TTM<0), Inst Trans ≥3, Stage 2
perfect, IPO-lifecycle tag. Excludes slow-growth sectors (utilities, energy,
real estate, basic materials, consumer defensive) and commodity/construction
industries. **Distorted TTM is NOT a criterion** — clean TTM earns a point
via `eps_yy_strong`; spin-off/IPO distortion is captured implicitly via the
`eps_qq_strong` clause. **Distorted-TTM threshold (May 2026):** when `eps_qq_strong=True`
AND `eps_yy_strong=False` (Q/Q strong but TTM negative — prior-loss company in character
change), threshold lowers to 3/6. `eps_qq_strong + inst_buying + stage2_perfect = 3` is
enough to surface without the persistence gate. FSLY (Q86, Q/Q +55%, Inst +7.91%,
Stage 2 perfect, 1 appearance) is the reference case.

**🚀 Fresh Breakout** — top 5 by Quality Score. Catches ANET-Apr8 / ARWR-today class (breakout-from-base with volume expansion; complementary to Ready-to-Enter which is pullback-based). Criteria: Stage 2 (not requiring perfect) · SMA20% > 0 · SMA50% in (0, 25%] · SMA200% > 0 · ATR% ≤8% · Q ≥70 · dist from 52w high 0% to -12% · peel-warn safe (SMA50%/ATR% ≤ per-ticker calibrated) · not held. RVol default ≥1.2 OR tight-quality exception `(Q≥80 AND ATR≤6 AND RVol≥1.0)` — May 2026, catches RMBS/TWLO-class quiet pre-break setups. Auto-adds to watchlist with `source=breakout_auto` (third entry path alongside technical + Hidden Growth).

**🌀 HTF Base Reclaim (May 2026 — RKLB-class)** — catches Stage 2 perfect names that have reclaimed their recent swing pivot from a deeper 52w drawdown (RKLB Apr 16 reference: -16.7% from 52w high but -5%-ish from Jan/Feb swing high). Pre-filter (Finviz snapshot, no network): Stage 2 perfect · Q ≥75 · ATR% ≤7 · dist from 52w high < -12% · rising MA stack (SMA20/50/200 all > 0) · RVol ≥1.0 · peel-safe · not held · not already in RTE/FB/BB/PP/HG. Final gate (`agents/utils/swing_pivot.py`): fetches 90d daily bars from Alpaca, computes `swing_high = max(high)` over last 90d excluding last 5 days, requires `dist_from_swing_high_pct ≥ -10%`. Top 5 by Q in Slack block "🌀 HTF Base Reclaim". Gallery: `<details open>` section with all qualifiers (uncapped). Watchlist: auto-adds at `priority=focus` (`source=htf_base_reclaim_auto`) — fifth entry path alongside technical/HG/breakout/RS Leader.

**⭐ Textbook VCP marker** — overlay badge, not a separate list. Promotes VCP confidence ≥85 · appearances ≥3 · ATR% ≤5 · Stage 2 perfect · dist -3% to -15% · Q ≥80 setups with a ⭐ badge on Slack Top Picks / Ready-to-Enter lines and watchlist.html ticker cells. Flag written to `daily_quality.json` as `textbook_vcp: true/false`. **Dist band widened from -8% to -15% (Apr 30 2026)** after INDV — a textbook setup at -13% — was missed by the prior tighter band.

**💎 Power Play / High Tight Flag** — rare Minervini/O'Neil monster pattern. Criteria: Perf Month ≥50% OR Perf Quarter ≥100% (rocket) · ATR% ≤5 (tight flag) · RVol <1.0 (volume drying) · Stage 2 · peel-warn safe. Uses new Finviz snapshot fields `Perf Month` / `Perf Quarter` — `get_snapshot_metrics` now returns 14-tuple instead of 12.

**🏗 Base Building** — watch-only research tag (no watchlist auto-add). Criteria: Stage 2 · Q≥75 · dist -12% to -25% from 52w high · ATR%≤7 · not held · not already in Ready-to-Enter, Fresh Breakout, Power Play, or Hidden Growth. Top 10 by Q (May 2026 — bumped from 5 to surface RKLB-class Q=78 names ranked out on busy days), all 10 in Slack block "🏗 Base Building". HTML gallery: collapsed `<details>` section with chart cards.

**⚠ High-vol card annotation** — when ATR%>7 AND Q≥80, `_build_card` adds a `badge-warn` "⚠ High-vol — size 50%" tag to the chart card. Ready-to-Enter (ATR≤7) and Fresh Breakout (ATR≤8) already hard-block these; the badge is the only signal for human to right-size on Top Picks cards.

**🎯 21 EMA Pullback (May 2026 — ANET/APP-class)** — continuation entries on names that ran, pulled back to the EMA21/SMA20 area, and are showing either quiet drift or active bounce. Criteria (Finviz-only — SMA20% as EMA21 proxy, Perf Month as ret20 proxy): Stage 2 (pullback-friendly: `sma200 > sma50 > 0 AND sma20 ≥ -2`) · ATR% ≤ 6 · Q ≥ 75 · SMA20% in `[-2%, +3%]` · Perf Month ≥ 12% · RVol `<1.0` (quiet drift) OR `1.0-2.5` (active bounce) · peel-safe · not held · not in another callout. Top 5 by Q in Slack block "🎯 21 EMA Pullback". Watchlist: auto-enters at `priority=focus` (`source=ema21_pb_auto`). Catches ANET Apr 22 (RVol 2.15) and APP Sep 9 2024 (RVol 1.76) class missed by RS Leader's RVol ≤ 1.5 cap. Derived from retro coverage audit ([docs/specs/retro-coverage-nbis-class.md](docs/specs/retro-coverage-nbis-class.md)).

**🌀 HTF Base Reclaim — ATR cap raised 7 → 8.5 (May 2026)** — DOCN Apr 13 2026 (ATR 8.0, dist -15.4%, Stage 2 perfect, clean reclaim) was dropped by the prior 7 cap. New cap catches deeper-base reclaims at the high-vol edge without admitting junk (peel-warn gate still filters extension). High-vol `⚠ size 50%` badge still applies to ATR > 7 cards.

**🎯 Ready-to-Enter / 🛡️ RS Leader — pullback-friendly Stage 2 when dist ≤ -10% (May 2026)** — at the deep edge of each block's dist band, accept Stage 2 with `SMA20% ≥ -3` (instead of requiring `SMA20% > 0`). On a legitimate pullback to EMA21/SMA20, price routinely dips 1-3% below SMA20 — the prior strict perfect ladder killed these setups (e.g. SMCI Jan 17 2024 class). Inside the band (dist > -10%) the strict ladder still applies.

**🛡️ RS Leader (Phase 1 + Phase 2, May 2026)** — catches DOCN-class setups (single-screener, never hits persistence gate, VCP low) by detecting stock-level relative strength independent of market_state. Predicate (`_is_rs_leader_candidate`): Stage 2 perfect · Q ≥ 75 · dist [-10%, +2%] from 52w high · rising MA stack (SMA20/50/200 all > 0) · ATR% ≤ 8 · peel-safe (SMA50%/ATR% ≤ peel_warn) · RVol ≤ 1.5 · **RS Rating ≥ 60** · not in {Utilities, Energy, Real Estate, Basic Materials, Consumer Defensive} · not held. **No market_state gate** — trigger_state is logged for analytics only. Scans `summary_df` (pre-10%-gate) like Hidden Growth. Persistent tracker in `data/rs_leaders.json` with 14-day pullback grace. Lifecycle: active → pulling_back → reacquired (or aged_out). Slack: 🛡️ NEW / REACQUIRED / 📉 pulling back — lines show `RS {rating} · Q{score} · dist`. Watchlist: first-trigger `new` and `reacquired` auto-enter at `priority=focus` (`source=rs_leader_auto`). Gallery: `🛡️ Relative Strength Leaders` collapsible section with NEW/REACQUIRED + purple RS badge. DOCN Apr 6 2026 is the reference case (Q=84, dist -4.9%, mult 4.1x, RVol 0.78). **Phase 2 — RS Rating (`_compute_rs_ratings`):** IBD-style weighted composite `(Perf Quarter × 0.4) + (Perf Half Y × 0.3) + (Perf Year × 0.3)` (9M approximated as average of 6M+12M, weights simplify to 0.3+0.3). Percentile-ranked within today's screener universe → integer 0–99. Computed after Quality Score, stored as `RS Rating` column on `summary_df` and persisted in `rs_leaders.json`. Data source: Finviz snapshot fields `Perf Half Y` and `Perf Year` (extends existing 14-tuple to 16-tuple).

**Hidden Growth vs watchlist tiers — two independent axes.** Hidden Growth is a
fundamental/accumulation flag (EPS + institutional + IPO lifecycle); tiers
(`watching`/`focus`/`entry-ready`) are technical setup readiness. They overlap
freely. A ticker can be Hidden Growth at any tier, or Hidden Growth but not in
the watchlist yet (like NVTS-Apr16 which was 10%-excluded). Hidden Growth
hits auto-enter the watchlist at `priority=watching` with `source=hidden_growth_auto`
(parallel funnel to the technical `screener_auto` path); if already present,
no-op (reactivates if aged-out). Daily snapshot written to `data/hidden_growth.json`.

## Market State Classification

The cycle flows directionally: RED → THRUST → CAUTION → TREND-FOLLOW ⇌ GREEN → COOLING → EXTENDED → DANGER → RED. STEADY-UPTREND kept as a safety net when TREND-FOLLOW gates miss.

| State | Condition | Priority | Direction | Trading Action |
|-------|-----------|----------|-----------|---------------|
| BLACKOUT | Feb 1–end of Feb · Sep 1–Sep 30 | 1 | — | No new trades in Feb or Sep — both flagged as seasonally unreliable months |
| DANGER | 500+ stocks down 4%+ today AND 5d ratio < 0.5 | 2 | ↓ hard | No entries, raise stops immediately |
| **EXTENDED** | SPY ATR mult from 50MA ≥ 7 **OR** SPY %above 50MA ≥ 8 **OR** QQQ ATR mult from 50MA ≥ 9 | 3 | ↑↑ blow-off | **No new entries** — parabolic tape, tighten stops, no chase. Overrides THRUST/GREEN/CAUTION/TREND-FOLLOW/STEADY (May 2026 — SNDK distribution-day class) |
| COOLING | prev_state == GREEN AND GREEN conditions no longer met | 4 | ↓ fading | Trim positions, tighten stops, no new entries |
| THRUST | 500+ stocks up 4%+ today (Bonde "Very High" buying pressure) | 5 | ↑ signal | Start building watchlist NOW |
| GREEN | 5d ratio >= 2.0, 10d >= 1.5, F&G >= 35, SPY above 200d MA | 6 | ↑ bull | Full size entries |
| **TREND-FOLLOW** | SPY>SMA50>SMA200 AND SMA50 rising 10d AND SPY within 3% of 20d high AND participation proxy (up_25_qtr/universe) ≥ 8% AND (VIX<25 OR VIX falling) AND not EXTENDED | 7 | ↑ steady trend | **Full size, entries allowed.** Trend-persistence path independent of 5d/10d thrust ratio. Rides steady grind-up tapes (Apr 24–May 5 2026 reference) instead of falling to RED. May 2026. |
| CAUTION | 5d ratio >= 1.5, F&G >= 25, SPY above 200d MA | 8 | ↑ recovering | Half size, build watchlist, get ready |
| STEADY-UPTREND | SPY > 200d AND SPY > 50d AND F&G ≥ 50 AND up4 ≥ dn4 AND 5d_ratio ≥ 0.9 AND prev_state ∉ {RED, DANGER, BLACKOUT, EXTENDED} AND not EXTENDED | 9 | ↑ steady | Half size, entries allowed on confirmed RS leaders. Safety net when TREND-FOLLOW gates just miss (e.g. participation right under 8%). |
| RED | Everything else (SPY below 200d MA or breadth weak) | 10 | ↓ bear | No new trades |

**SPY/QQQ extension metrics** — `spy_atr_mult_50`, `spy_sma50_pct`, `spy_sma50_slope_10d`, `spy_pct_from_20d_high`, `qqq_atr_mult_50`, `qqq_sma50_pct` are computed from Alpaca daily bars via `fetch_index_extension()` and persisted in each daily JSON record. ATR% Multiple formula matches `utils/calibrate_peel.py`: `(close − sma50) × close / (sma50 × atr14)`. `is_extended()` fires if any of: SPY ATR mult ≥ 7, SPY %above 50 ≥ 8, QQQ ATR mult ≥ 9. `is_trend_follow()` requires all 6 gates above. VIX comes from `fetch_vix_snapshot()` (Yahoo `^VIX`). Participation proxy `pct_above_50ma` ships as `up_25_quarter / universe_size` — true %above-50MA computation is a follow-up.

**5d/10d breadth ratio demoted to gauge (v3, May 2026).** The 5-day and 10-day up4/down4 ratios are no longer used to gate any state. They were thrust-day detectors being mis-used as trend detectors — steady grind-up tapes produce few 4% moves either way → ratio ~1.0 → fell through to RED (Apr 24–May 4 missed-rally bug). The fields stay in the daily JSON and Slack message as a "thrust strength gauge"; trend decisions now flow through TREND-FOLLOW.

**STEADY-UPTREND prev_state guard** is strict by design: the only path out of RED stays RED → THRUST → CAUTION → GREEN. A single greedy-day bounce inside a downtrend cannot auto-rescue entries. Also blocked when EXTENDED is active (priority 3 wins).

**Executor / position_monitor wiring:** EXTENDED → `(block=True, size_mul=0.0)`, TREND-FOLLOW → `(block=False, size_mul=1.0)`, GREEN/THRUST → `(False, 1.0)`, STEADY-UPTREND → `(False, 0.5)`. `aggressive` mode bumps any `size_mul == 1.0` state to 1.25× (covers GREEN/THRUST/TREND-FOLLOW). `effective_max_positions` returns 10 for GREEN/THRUST/TREND-FOLLOW, 7 for CAUTION/STEADY-UPTREND, 5 otherwise. In EXTENDED, dynamic stop base tightens to 3% (same as RED/DANGER). Backtest replay: `python scripts/replay_state_machine.py --days 60`.

**Confidence Layer (two overlays — May 2026):**
- **Layer 1 — Post-THRUST floor:** After any THRUST day, minimum state = CAUTION for 3 calendar days. Fixes THRUST→RED-next-day flips. DANGER still bypasses immediately. `post_thrust_floor_active` written to daily record + `trading_state.json`.
- **Layer 2a — Extreme greed (F&G > 74):** When prev ∈ {GREEN, THRUST} and conditions deteriorate, the 2-day COOLING buffer is skipped → RED fires immediately. Tagged `extreme_greed_caution` in record. Slack: `⚠️ EXTREME GREED`.
- **Layer 2b — Extreme fear (F&G < 25) + THRUST from RED/DANGER:** Override THRUST → CAUTION with `high_confidence_recovery` tag. Capitulation + breadth explosion = bottom signal. Slack: `⚡ HIGH-CONFIDENCE THRUST`.
- **2-day COOLING buffer (normal F&G 25–74):** From COOLING, RED-level conditions require 2 consecutive weak days before allowing RED. Recovery to CAUTION always immediate. Tracked via `consecutive_weak_days` in `trading_state.json`.
- **New `trading_state.json` fields:** `consecutive_weak_days`, `last_extreme_greed_date`, `last_extreme_fear_date`.
- **New daily record fields:** `fg_regime`, `post_thrust_floor_active`, `confidence_context`.

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
