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
| Weekly Review | `finviz_weekly_agent.py` | 10:00 UTC Saturday | `#weekly-alerts` — decision-first rebuild (Feature D, 2026-06-02): §1 Positioning & Book Risk · §2 Week-Ahead Shortlist (trade-plan cards) · §3 Book Weekend Review · §4 Leadership Map · §5 Strategist's Note |
| Market Monitor | `market_monitor.py` | 21:00 UTC Mon-Fri | `#market-alerts` (state changes + THRUST) |
| Position Book | `position_monitor.py` (`BOOK_RUN=1`) | 13:15 / 14:30 / 17:30 UTC Mon-Fri (3x daily) | `#positions` — consolidated table |
| Position Critical | `position_monitor.py` | Every 30 min 14:00-21:00 UTC Mon-Fri | `#positions` — only when a critical event fires |
| Alerts | `alerts_agent.py` | 21:00 UTC Mon-Fri | `#general-alerts` |
| Earnings Alert | `earnings_alert.py` | 21:30 UTC Mon-Fri | `#general-alerts` |
| Market Pulse | `market_pulse.py` | 4x daily (10am, 12:10pm, 2:20pm, 4pm ET) | `#daily-alerts` |
| Winners Watchlist | `winners_watchlist.py` | Monday evenings | `#weekly-alerts` |
| **Paper Executor** | `alpaca_executor.py` | After Market Monitor (workflow_run) + manual | `#daily-alerts` (BUY placements + summary only) |
| **Paper Monitor** | `alpaca_monitor.py` | Runs inside position-monitor.yml | `#positions` (prefixed `[PAPER]`) |
| **Live Executor** | `alpaca_executor.py` + `TRADING_PROFILE=live` | LIVE step in alpaca-executor.yml (after paper pass) | `#daily-alerts` (prefixed `[LIVE 🔴]`) |
| **Live Monitor** | `alpaca_monitor.py` + `TRADING_PROFILE=live` | position-book.yml (3x verbose) + position-critical.yml (30-min, `MONITOR_QUIET=1` — sells/alerts only) | `#positions` (prefixed `[LIVE 🔴]`) |
| Sector Rotation | `agents/sector_rotation.py` | 21:15 UTC Mon-Fri (Slack on Mon/Thu) | `#daily-alerts` |

**Live Alpaca profile (2026-06-12 — spec [docs/specs/live-alpaca-executor.md](docs/specs/live-alpaca-executor.md)):**
`agents/trading/trading_profile.py` resolves `TRADING_PROFILE` (paper default | live) into creds/base-URL/state-files/Slack-tag; executor + monitor are parameterized, not forked. The agent trades the **dedicated live Alpaca account (~$5k)** — a *scoped amendment* to the "never execute live orders" rule; **SnapTrade/Robinhood stays alert-only forever**. State: `data/live_alpaca_stops.json` + `data/live_alpaca_trading_state.json` (fresh streaks, independent of paper and of the user's manual `trading_state.json`). Live deltas: position cap **3** (`equity/3 × size_mul`, Q≥60 floor unchanged) · notional buys / fractional sells, $10 floor · marketable-limit buys (`last × 1.005`, day TIF; expired-unfilled logged `UNFILLED EOD — no chase`) · circuit breakers (−3% intraday → daily halt; equity < 85% high-water → `breaker_suspended`, re-enable only via dispatch input `live_reenable=1`) · order sanity (≤60% equity, today's qualified list only) · idempotent `client_order_id = "live-{YYYYMMDD}-{ticker}"` · **full exits only — NO T1/T2 peels**: ≥+20% tight tier trail + **hard full take-profit at +30%** · foreign positions refused (`[LIVE 🔴] FOREIGN POSITION` alert, never managed) · first-run gating: scheduled live entries skipped until `first_run_verified: true`, set by the first manual non-dry-run dispatch (`LIVE_DRY_RUN=1` evaluates but places/arms nothing). Tests: `tests/test_trading_profile.py`.

**Note on naming:** `finviz_` prefix kept only where Finviz is the primary data source (`finviz_agent.py`, `finviz_weekly_agent.py`). All other agents renamed to reflect their actual data source (Alpaca, SnapTrade, etc.).

**Shared rules engine — `agents/trading/rules.py`** (used by BOTH paper monitor and live position_monitor):
- `apply_position_rules()` — per-tick continuous ATR-tiered trail. **Trail ratchets off `highest_price_seen`** (intraday-aware, immune to hourly-snapshot peak gaps — the VIK Apr-2026 regression). Tier ladder by `peak_gain_pct`: `<10%` → 2.0×ATR · `≥10%` → 1.5×ATR · `≥20%` → **1.25×ATR if atr_pct ≤ 5%, else 1.0×ATR** (low-vol names get one extra quarter-ATR breathing room at the lock tier — May 2026). **Loss-cap floor** at peak ≥ +5%: `stop ≥ max(entry × 0.97, entry − 0.5×ATR$)` — hybrid α/β, vol-aware for low-vol names with -3% ceiling for high-vol. **Breakeven crossover** at peak ≥ +20% sets `breakeven_activated` flag (drives Slack/dashboard `BE` indicator) and floors stop at `entry × 1.005` as a fallback when ATR data missing — no longer gates the trail. **+30% floor** = `max(1.25/1.0×ATR trail, peak × 0.90)` — the 10%-from-peak guard kicks in only for >10% ATR names where ATR trail goes looser. T1/T2 alerts. 1×ATR fade alert. **Returns structured events** (`{kind, ticker, message, ...}`) — caller forwards `message` to Slack and may key side effects off `kind`.
- `price_above_sma5(closes, current_price)` — helper used by monitors: True when price ≥ SMA(last 5 daily closes). Callers use this to suppress premature stop exits on low-ATR names when the short-term trend is still intact. Returns False when fewer than 5 closes available (don't suppress).
- **Flush-suppress stop filter (2026-07-10 — spec [docs/specs/flush-suppress-stop-filter.md](docs/specs/flush-suppress-stop-filter.md), TEM/DAVE 7/9 whipsaw):** `flush_window_active(history)` reads market_monitor_history records — active iff a session with `down_4_today ≥ 400` sits within the last 3 records (flush day = 1/3) AND latest `spy_sma50_pct > 0` AND `vix_close < 20`. `should_suppress_stop_exit(closes, current_price, atr_pct, entry_price, perf_month, flush_ctx)` suppresses a stop-breach exit iff window active + price ≥ entry (never widens a loss) + price ≥ structure EMA (**8 EMA when Perf Month ≥ 40** — DAVE class; else **21 EMA** — TEM class). `evaluate_flush_suppress(...)` adds the close-based layer: returns `suppress`/`defer` (intraday wobble during active suppression — post-close run decides) /`exit`. Position state: `flush_suppress_active`, `flush_suppress_day`, `flush_suppress_alerted_date` (daily dedup). `flush_suppress` added to `CRITICAL_EVENT_KINDS`. **Wiring:** manual book (`position_monitor.py` Rule 1) alert becomes `🛡 STOP BREACHED — FLUSH SUPPRESS` (human decides); paper (`alpaca_monitor.py`) gates the auto-sell; **LIVE unchanged** until `flush_suppress_live: true` in `live_alpaca_trading_state.json` (gate: replay saves ≥ 2× damage AND ≥3 real paper suppressions net-positive over ≥4 weeks). Replay: `scripts/replay_flush_suppress.py`. ⚠ First 90-day replay (2026-07-10): **0.36x — net negative**, driven by June 3–4 stop-outs held into the June 5 break (down4 496 / SPY +6.3% over 50SMA / VIX 16 on 6/3 is indistinguishable from a benign flush). Live gate NOT met; paper observation running. Tests: `tests/test_flush_suppress.py` (26).
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
- `utils/generators/generate_daily_cockpit.py` — **Daily Cockpit** (`data/daily.html`). Decision-first single pane that replaces the chart "firehose" — designed around the user's documented leaks (round-tripping winners, hold-in-hope on losers, over-trading weak tapes). 6 blocks top→bottom = the morning routine: **0 Discipline banner** (sizing mode · streak · equity vs $150k/$200k goal) · **1 🚦 The Gate** (`gate_decision()` maps market_state + ETF regime + sizing_mode → FULL/HALF/**NO NEW ENTRIES**/PAPER ONLY + position cap; regime `blow-off-risk`/`late-rotation` and `reduced`/`suspended` only ever tighten) · **2 📓 The Book** (live SnapTrade via `fetch_positions` + `verdict_for`, scale-due/cut-due flags made loud; falls back to `positions.json`) · **3 🎯 Qualified Today** (`qualify_setups()` = Ready-to-Enter gate over latest screener CSV, ≤3 cards; greyed watch-only when gate closed; "0 qualify = patience") · **4 👀 On Deck** (watchlist tiers) · **5 🗺 Leadership** (reuses `etf_rotation_summary`) · **6 📊 The Record** (win-rate + avg-win/avg-loss payoff). Pure decision/render fns import-safe (unit-tested in `tests/test_daily_cockpit.py`, 19 tests). Regenerated from `position_monitor.py` (live book) AND `daily-finviz.yml` (fresh screener). Linked from `index.html` as first hero button **☀️ Daily Cockpit**. Spec: [docs/specs/daily-cockpit.md](docs/specs/daily-cockpit.md). Slack overhaul (daily Slack is the same firehose) deferred to its own spec.
- **Portfolio dashboards — unified architecture (2026-06-19 refactor).** The paper and live pages are now ONE dashboard reading from two sources. All shared layout + analytics live in `utils/generators/portfolio_common.py`; each generator is a thin adapter (fetch from source → normalize to common row/event schema → call shared renderers), so the two can no longer drift apart.
  - `utils/generators/portfolio_common.py` — shared module: formatters/heat classes, the `/pos-review` `verdict_for` ladder + `classify_action(gain, atr)` (single source for summary chips AND per-row `data-action` tags; mirrors `verdict_for`'s EXACT clause order — `gain≥10 → trail` checked before the high-vol peel clause, so a +11% high-ATR name is `trail`, NOT `peel`; counts can never disagree with the Verdict column), a **source-agnostic FIFO engine** (`closed_trades(events)` / `open_entry_dates(events)` / `trade_stats` / `monthly_realized` over a normalized `{symbol, side, qty, price, date}` event list), and the shared renderers `render_stat_cards`, `render_positions_section` (action chips + table w/ Entry/Held/ATR%/S20%/Stage/Verdict + legend, decision-first sort), `render_trade_history` (sortable headers + month filter), plus `PORTFOLIO_CSS`, `PORTFOLIO_JS` (sortTable/filterAction/filterMonth), `page_shell`. Tests: `tests/test_portfolio_common.py`.
  - `utils/generators/generate_live_portfolio.py` — SnapTrade adapter → `data/live_portfolio.html`. Account header ← SnapTrade balances; Open Positions ← SnapTrade positions + Finviz technicals; **Trade History + Month-over-Month Realized P&L ← the SnapTrade BUY/SELL activities already cached in `data/position_history.json`** (`_load_live_events()` flattens `{ticker:[{date,action,shares,price}]}` → events → shared FIFO). Entry dates from `positions.json` open_positions (`—` for SnapTrade-only holdings like leveraged ETFs). Non-fatal placeholder on failure. Called from `position_monitor.py` every run (3× daily book + 30-min critical). Linked from `index.html` as **Live Portfolio**.
  - `utils/generators/generate_portfolio.py` — Alpaca adapter → `data/claude_portfolio.html`. Account header + **equity curve** ← Alpaca account/portfolio-history (the one source-specific section live can't match — kept); Open Positions ← Alpaca positions **now enriched with Finviz technicals** so the table matches live (verdict/ATR%/S20%/Stage — paper had none of this before); Trade History + Month-over-Month ← Alpaca FILL activities via the shared FIFO. `main()` enriches technicals per open ticker (reuses live's `_technicals`); render stays network-free for tests. Tests: `tests/test_generate_portfolio.py`.

**Paper auto-peel + stale-cull (2026-05-27 — alpaca_monitor.py + rules.py):**
- `process_target_peels()` consumes `target1`/`target2` rules-engine events: on T1 sells `qty//2` + raises stop to `entry × 1.005` + sets `t1_peeled=True`; on T2 sells `qty//2` of remaining + sets `t2_peeled=True`. Skips when qty≤1 or `peel_qty × price < $50`. Slack `[PAPER] T1/T2 AUTO-PEEL`. New `paper_stops.json` fields: `t1_peeled`, `t2_peeled` (default False, idempotent migration).
- `check_stale_position()` / `check_live_stale_entry()` — paper auto-sells full qty; live emits `stale_entry` event (alert-only, never auto-sells — hard rule). Thresholds: `rules.STALE_DAYS = 14`, `rules.STALE_PEAK_THRESHOLD = 4.0`. Skipped when `t1_peeled` (already won). `stale_entry` added to `CRITICAL_EVENT_KINDS` → routes to immediate Slack. Dedup via `stale_alerted_date`. Slack `💤 [PAPER] STALE CULL` / `💤 STALE`.

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
| Sector Rotation | `sector-rotation.yml` | Cron 20:15 UTC Mon-Fri + workflow_dispatch (runs 15 min before daily screener so screener can read today's snapshot for Stage Transition gate) |
| Monitor Heartbeat | `monitor-heartbeat.yml` | Cron 15/17/19/21 UTC Mon-Fri + workflow_dispatch |
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
- Target alerts: **ATR-tiered T1/T2** (`compute_targets()` in `agents/trading/rules.py` — moved from `alpaca_monitor.py` 2026-07-09, alias kept): ATR ≤3% → +20%/+40%; 3–5% → +15%/+30%; 5–8% → +12%/+25%; >8% → +10%/+20%. **Applies to ALL THREE books** (manual/Robinhood `positions.json`, paper, live). `rules.retier_legacy_targets()` migrates legacy +20% targets on every monitor run for every tier (was high-vol-only, paper-only — the TEM 2026-07 miss: 6.7% ATR name sat on unreachable +20% T1 while peaking +13.8%, unpeeled). Never rewrites hit/peeled/manually-set targets. `position_monitor.py` target writes (auto-add, avg-up, dispatch BUY) use `tiered_targets_for()` (fresh Finviz ATR, legacy fallback). T1/T2 status (✅/⏳) shown in every daily summary; daily reminder while T1 locked and T2 pending
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
| `SLACK_WEBHOOK_MOMENTUM` | secret | Daily screener (`#momentum-alerts` for ⚡ Episodic Pivot fires) |
| `ANTHROPIC_API_KEY` | secret | Daily agent, weekly agent, position monitor |
| `PAGES_BASE_URL` | secret | All agents (gallery links in Slack) |
| `SNAPTRADE_CLIENT_ID` | secret | Position monitor |
| `SNAPTRADE_CONSUMER_KEY` | secret | Position monitor |
| `SNAPTRADE_USER_ID` | **variable** | Position monitor |
| `SNAPTRADE_USER_SECRET` | secret | Position monitor |
| `ALPACA_API_KEY` | secret | Paper executor, paper monitor, premarket alert, market pulse |
| `ALPACA_SECRET_KEY` | secret | Paper executor, paper monitor, premarket alert, market pulse |
| `ALPACA_BASE_URL` | secret | Paper executor, paper monitor (`https://paper-api.alpaca.markets/v2`) |
| `ALPACA_LIVE_API_KEY` | secret | Live executor + live monitor (`TRADING_PROFILE=live`, account 939406794) |
| `ALPACA_LIVE_SECRET_KEY` | secret | Live executor + live monitor |
| `ALPACA_LIVE_BASE_URL` | secret | Live executor + live monitor (`https://api.alpaca.markets/v2`) |
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
  live_alpaca_stops.json                  # LIVE Alpaca profile position state — same schema as paper_stops.json
  live_alpaca_trading_state.json          # LIVE profile streaks/sizing + first_run_verified + breaker_suspended + high_water_equity + last_expired_check_ts. Fresh streaks — independent of paper AND manual trading_state.json.
  hidden_growth.json                      # Today's Hidden Growth candidates (overwritten daily) — {date, candidates: [{ticker, signal_score, criteria, eps_yy_ttm, eps_qq, inst_trans, appearances}]}
  rs_leaders.json                         # RS Leader persistent tracker — {ticker: {first_triggered, trigger_state, trigger_q, trigger_dist, trigger_atr_mult, rs_rating, current_status, last_active_date, pullback_started, reacquired_dates, days_tracked}}
  alerts_state.json                       # Breadth/F&G alert state (rolling 15-day)
  market_monitor_history.json             # Rolling 30-day breadth history
  market_monitor_YYYY-MM-DD.json          # Daily market breadth snapshot
  daily_quality_YYYY-MM-DD.json           # Q-rank, stage, section per ticker
  finviz_screeners_YYYY-MM-DD.csv         # Enriched daily screener data
  finviz_screeners_YYYY-MM-DD.html        # HTML table
  finviz_chart_grid_YYYY-MM-DD.html       # Chart gallery (sector rotation panel + click-to-filter + Base Building + Watchlist tiers)
  etf_rotation.html                       # ETF Rotation Dashboard (daily — regime banner + bucket cards + full metrics table)
  etf_rotation.json                       # Machine-readable ETF rotation snapshot — {date, regime, etfs: [{ticker, name, kind, bucket, metrics}]}
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
1. Explicit map at `data/ticker_sector_map.json` (override-only — e.g. AAOI → SMH because Finviz industry is "Communication Equipment" but revenue mix is semis-adjacent).
2. **`INDUSTRY_TO_ETF`** substring match on Finviz Industry — semis ("Semiconductor*") → SMH, software ("Software - Application/Infrastructure") → IGV, internet ("Internet Content*") → FDN, banks → KBE, capital markets → KCE, insurance → KIE, biotech / drug manufacturers → XBI, residential construction / building products → XHB. Fixes the "Technology" lump bug where SMH semis and IGV software both resolved to XLK and rotation was invisible. First matching key wins (dict insertion order).
3. Fallback to caller-provided Finviz `Sector` string via `FINVIZ_SECTOR_TO_ETF` (e.g. "Healthcare" → XLV).
4. None → caller skips sector signal for that position.

**ETF Rotation Dashboard (May 2026)** — `agents/sector_rotation.py` extended to compute per-ETF setup metrics (ATR%, mult50, dist52, range20, ret20, ema21d, RVol, MA stack) and bucket each ETF: `BASE` / `PRE-BREAKOUT` / `EXTENDED` / `BROKEN` / `NEUTRAL`. Bucket logic in `assign_bucket()`. Daily outputs: `data/etf_rotation.html` (one-page dashboard with regime banner + bucket cards + full metrics table) + `data/etf_rotation.json`. Linked from `index.html` "ETF Rotation" tile. Universe curated 35 → 28 → 37 → **45** ETFs (sectors 11 + thematics 34). 2026-05-29 audit ran in two passes. v1: KWEB · ARKG · ARKF · REMX · WCLD · QTUM · IBIT · NLR (foreign-listed + basket-level themes invisible to prior screener). v2 (same-day second pass after user pushed back on artificial 35-cap): PAVE (US infrastructure / re-shoring — JBL/AMPX class) · IHI (med devices — ISRG/SYK class) · DRIV (autonomous & EV) · ICLN (broad clean energy — wind/grid/storage that TAN-solar misses) · JETS (airlines, un-dropped — recent rotation move invalidated prior "low signal" reasoning) · URNM (uranium pure-play, distinct from URA broad) · BLOK (blockchain stocks — COIN/MARA/MSTR exposure distinct from IBIT price) · EEM (emerging markets ex-China — INDA/EWZ since KWEB is China-only). Mostly-redundant explicitly skipped: SOXX (=SMH), CIBR/BUG (=HACK), BOTZ/ROBO/AIQ (=SMH+IGV), VIS/IYJ/FXR (=XLI), IBB (=XBI). Above prior 35-cap by design — "45 covering distinct themes > 28 with gaps." Dashboard now opens with **⭐ Sweet Spot section** at top: filters to `rank ≤ 20 AND climbed ≥ 3 spots 5d AND bucket ∈ {BASE, PRE-BREAKOUT}` — the actionable intersection of rotation flow + clean chart structure. Below it the RS Leaderboard (top 10 by RS) and full metrics table render unchanged. Spec: [docs/specs/etf-rotation-dashboard.md](docs/specs/etf-rotation-dashboard.md). Same cron as sector_rotation.yml (21:15 UTC weekdays).

**Weekly Sector Setup section (May 2026)** — Weekly review (`finviz_weekly_*.html` + Slack) now includes a `📊 Sector Setup This Week` block sourced from `data/etf_rotation.json` (Friday snapshot when weekly runs Saturday). After the Feature D rebuild it lives inside §4 Leadership Map (see below). Helper: `agents/utils/etf_rotation_summary.py` — pure `load_etf_rotation` / `summarize_etf_rotation` / `render_sector_setup_html` / `render_sector_setup_slack` + `REGIME_ADVICE` dict + `SECTOR_SETUP_CSS`. Top 5 per actionable bucket (BASE / PRE-BREAKOUT / EXTENDED / BROKEN); NEUTRAL filtered. Falls through gracefully when `etf_rotation.json` missing. Spec: [docs/specs/weekly-etf-rotation-section.md](docs/specs/weekly-etf-rotation-section.md).

**Weekly Review Rebuild — decision-first (Feature D, 2026-06-02).** The weekly was rear-view (ranked by appearance frequency = last week's already-extended winners; AI essay with no decision; a 21 EMA re-entry lane that was empty ~90% of weeks). Rebuilt around 4 jobs of a Saturday weekly for real capital: am I positioned right, what's my shortlist + plan, what do I do with what I hold, what's leadership doing. New section order in `generate_weekly_html` + `send_weekly_slack` + `main()`:
- **§1 Positioning & Book Risk** — `agents/utils/weekly_positioning.py`. Opens with USER state: market_state + ETF rotation regime + "N positions vs cap M" (🚨 over). Realized P&L this week (FIFO via `utils/pnl_walk.compute_pnl_from_events` semantics over `data/position_history.json` — the proven-correct source, NOT trading_state.json; see [[feedback_pnl_source_of_truth]]), W/L count, biggest winner/loser. Book health: green / underwater / past-stop-held + `$`-quantified leak for names held past stop. Pure fns + `render_positioning_html`/`_slack` + `POSITIONING_CSS`. Tests: `tests/test_weekly_positioning.py`.
- **§2 Week-Ahead Shortlist** (REPLACES Top 5) — `agents/utils/week_ahead_shortlist.py`. Forward funnel: entry-ready watchlist + emerging candidates + recent RS leaders, deduped, enriched with current screener metrics, gated Stage 2 + peel-safe, ranked by Q. Each name = full trade-plan card: Setup · Trigger · Stop · Size · Invalidation. Stop floor **−8%** (MAE-derived — 2024-25 winners' MAE median −4.8%, mean −10.5%; `data/mae_analysis.json`), widened to 2×ATR% for volatile names. Size from regime (Full/Half/No-new-entries) with high-vol downgrade. Optional terse AI setup/invalidation prose (`enrich_shortlist_notes_ai`, non-fatal, deterministic fallback). Normalizes the serialized `compute_stage()` dict in the CSV Stage column. Tests: `tests/test_week_ahead_shortlist.py`.
- **§3 Book Weekend Review** — `agents/utils/book_weekend_review.py`. Per-open-position verdict (cur% / peak% / dist-to-stop / verdict) reusing `utils/generators/generate_live_portfolio.verdict_for` (the /pos-review ladder — single source of truth). Sorted action-first (cut → trim → trail → working). Tests: `tests/test_book_weekend_review.py`.
- **§4 Leadership Map** — ETF Sector Setup block (above) + promoted Emerging "Next on Radar" cards + macro/crypto/F&G snapshot.
- **§5 Strategist's Note** — `generate_strategist_note` replaces the old `generate_weekly_ai_brief` essay + `research_catalysts` web-search (both removed). MAX 3 bullets (regime insight / best setup + why / the one risk), token-capped Claude call with deterministic fallback so it always renders.
- **Killed the 21 EMA re-entry lane** — removed `_render_pullback_section`, the pullback bucket build, the Slack pullback block; deleted `agents/utils/pullback_setup.py` + its test (pullback detection folded into §2 via Finviz SMA20% EMA proxy). The old rear-view "Top 5 This Week" focus cards are gone; recurring-names leaderboard + character-change alerts demoted to the bottom as reference. Spec: [docs/specs/weekly-review-rebuild.md](docs/specs/weekly-review-rebuild.md).

**Pending integrations** (per spec rollout §13 — wire in after observing signal quality): position monitor SECTOR ROTATING OUT alert on `decay_streak_days >= 2`, paper executor lagging-sector annotation, weekly agent rotation section. (`rotation.html` dashboard tab now lives — see ETF Rotation Dashboard above.)

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
| Entry gate (extended) | `alpaca_executor.py` blocks new entry when ATR multiple > per-ticker `peel_warn` (from `peel_calibration.json`), **capped at the ATR% tier warn — calibration can only tighten, never loosen** (2026-06-12: raw calibrated warns 10.3/8.7 waved ALAB +58.7%/MU +49.7% above 50SMA into the live dry-run; same rule as the screener's 2026-05-29 v2 cal-cap). Falls back to tier warn when uncalibrated. Slack notes `calibrated` / `tier-cap` / `tier` source. |
| Paper market-state gate | `alpaca_executor.py` reads `market_state` from `market_monitor_history.json` (single source of truth — replaced the old SPY/SMA200 check). RED/DANGER/BLACKOUT → no buys, but still posts a Slack alert listing top-5 would-have candidates ("your call"). CAUTION/COOLING → half size. GREEN/THRUST → full. Sizing mode overlays: `suspended` blocks entirely, `reduced` clamps size_mul ≤ 0.25, `aggressive` boosts to 1.25× in GREEN/THRUST. |
| No averaging down | Rule 4 — BUY blocked if price < existing entry |
| Averaging up | BUY on existing position when price > entry → merges shares, recomputes weighted avg, recalculates T1/T2 |
| Initial stop (paper) | `alpaca_executor.py` sets stop = `entry − 2×ATR$` at buy time |
| Screener CSV fallback (executor) | `_resolve_screener_csv()` falls back to the most recent `finviz_screeners_*.csv` with date ≤ today when today's is absent (off-cycle/manual/late-`workflow_run` runs fire before the 20:30 UTC screener — the 2026-06-05 00:07 UTC failure). Refuses data > `MAX_SCREENER_STALE_DAYS` (7) old so it never trades on badly stale data. |
| Loss-cap floor | At peak ≥ +5%: stop ≥ `max(entry × 0.97, entry − 0.5×ATR$)` — hybrid α/β. Caps fade-back-to-loss after any meaningful win |
| ATR-tiered trail | Continuous, ratchets off `highest_price_seen`. Tiers by `peak_gain_pct`: <10% → 2.0×ATR, ≥10% → 1.5×ATR, ≥20% → **1.25×ATR if atr_pct ≤ 5% else 1.0×ATR**. No freeze, no dead zone |
| Breakeven crossover | At peak ≥ +20%: `breakeven_activated` flag set (Slack/dashboard `BE` indicator). Floor `entry × 1.005` applies when ATR data missing — otherwise the 1.25/1.0×ATR trail is already above entry |
| Trailing stop floor | At peak ≥ +30%: stop ≥ `max(1.25/1.0×ATR trail, peak × 0.90)`. The 10%-from-peak guard kicks in only for >10% ATR names where ATR trail is looser than 10% |
| SMA5 stop filter | For low-ATR names (≤5%): if `price >= SMA(last 5 closes)` when stop is triggered, exit is suppressed for that run — trend still intact. Paper skips sell; live skips alert. |
| Flush-suppress stop filter | Market-gated, all ATR tiers (orthogonal to SMA5): stop breach during an index flush (≥400 down-4% within 3 sessions, SPY > 50SMA, VIX < 20) is suppressed while the name holds its structure EMA (8 EMA if Perf Month ≥ 40, else 21 EMA) in profit. Max 3 sessions; close below EMA → exit, no second chance. Manual book alert-only (🛡), paper gates auto-sell, live off until `flush_suppress_live` gate. |
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

**Screener price/volume floors (2026-05-30):** `10% Change` and `Power Move`
screeners use `sh_price_o2` + `sh_avgvol_o1000` (was `sh_price_o5` + `sh_avgvol_o500`).
Dropped the price floor $5→$2 so sub-$5 movers at the base are visible (HYLN was
~$2 on its best 5/5 & 5/11 entries, filtered out by the old $5 floor and only
appeared 5/13+ after +150%). Raised the avg-vol floor 500k→1M as a penny-junk
guard so a $2 name still needs real liquidity. Other screeners (Growth/IPO/52WHigh)
keep `sh_price_o10`; Week 20%+ has no price floor.

**Dollar-volume liquidity gate + Base/Near-High screen (2026-06-09 — DAVE-class):**
The 1M-*share* avg-vol floor (`sh_avgvol_o1000`) is a crude share count that hid
high-priced liquid names — DAVE (Dave Inc, +311% EPS Y/Y, +104% Q/Q, +58% sales,
Stage 2, −8% from high) trades ~573K shares but ~$155M/day and was invisible to the
*entire* system (0 of last 30 screener CSVs; the 5/27 paper position came from a
live SnapTrade auto-detect, not the screener). Fix: (1) **quality screens
(Growth/52WHigh/IPO/Base) lowered to `sh_avgvol_o200`**; (2) real liquidity now
enforced by `passes_dollar_volume_gate()` (module-level, tested) — drops a row when
avg **dollar** volume (`Avg Volume × Price`) < **$30M/day**, but **only** for
quality-screen names. **Mover screens (`10% Change`/`Power Move`/`Week 20%+`) are
exempt** so sub-$5 rockets (HYLN ~$2 × 1M = $2M/day) are never dropped. Price is
carried from the Finviz screener table (`cols[9]`, index 9 in both v=111 and v=151
layouts) into `summary_df['Price']`. Gate runs in `main()` after snapshot
enrichment, before scoring/CSV save. Also **dropped the `an_recom_buybetter`
analyst gate** from the Growth screen (was discarding ~4 under-covered quality
growth names; not DAVE's blocker but a real loss). **New 7th screen "Base /
Near-High"** = the pre-breakout growth base the mover-screens miss: Stage 2 (above
all 3 MAs) + coiling **0–10% under** the 52w high (NOT a new-high requirement) +
EPS Q/Q & Sales Q/Q > 20 + o200 share floor. Catches the high-quality name about to
break a ceiling BEFORE it pops (DAVE coiling at 270 under the 293 wall) — the best
R/R entry that new-high/10%-move screens can only catch after the fact. Auto-flows
into every downstream block (Ready-to-Enter, RS Leader, HTF-BR, 21 EMA PB) since
they all scan `summary_df`. Tests: `tests/test_dollar_volume_gate.py`.

**Dollar-volume PRE-filter (2026-06-09 perf — universe doubled 133→254, run 9m41s):**
the $30M gate above runs AFTER the per-ticker snapshot (the slow part), so it
cleans output but doesn't save scrape time. `passes_dollar_volume_prefilter()`
(module-level, tested) runs in `main()` BEFORE `fetch_snapshots_concurrent` using
the screener table's own raw `Volume × Price` (already scraped, no extra network),
dropping obviously-illiquid names at a looser **$20M** floor (`PREFILTER_MIN_DOLLAR_VOL`)
so a single quiet-volume day can't drop a genuine DAVE-class name. Movers still
exempt. The precise $30M avg-volume gate stays as the final cut. Tests:
`tests/test_dollar_volume_gate.py::TestDollarVolumePrefilter`.

**Routing fix shipped (2026-06-09):** `Credit Services` / `Financial - Credit
Services` → **ARKF** (fintech) in `INDUSTRY_TO_ETF` + explicit `DAVE → ARKF`
override in `ticker_sector_map.json` (DAVE's Finviz industry "Software -
Application" was mis-routing it to IGV). See
[docs/specs/system-staleness-and-routing-fixes.md] §5.

**🔥 Big Movers (top-of-message, 2026-05-30):** Power Move tickers (9M+ share
volume + 10%+ day = Bonde institutional-conviction signal) surfaced FIRST in the
Slack message, above Ready-to-Enter, so an ONDS-class +83%/248M-vol candle can't
get buried in the 200-row table. Compact one-liner enriched with %change + volume
(e.g. `*ONDS* (+83.1%, 248M)`), sorted by volume desc. Replaces the old buried
"Power Moves" block that sat mid-message. Still gated on the 9M+ post-filter
(`_parse_vol`, since Finviz `sh_vol_o*` URL params are silently ignored). The HTML
gallery `🔥 Power Moves` section (via `_classify_ticker`) is unchanged.

Two actionable callouts in the daily Slack message, ordered by urgency:

**🎯 Ready to Enter** — top-of-message, top 5 by Quality Score. All must pass:
Stage 2 perfect · VCP conf ≥70 · Q ≥80 · dist from 52w high -1% to -12% ·
ATR% ≤7% · RVol ≤1.2 · **peel-safe (SMA50% / ATR% ≤ tier warn)** · not in `positions.json` open positions.
(Dist gate softened from -10% → -12% May 2026 — MTSI/RMBS class missed by 0.02-0.33pp.
Peel-safe gate added 2026-05-29 — AMD/DELL/STX class with sma50/atr 9-12× was promoted to
Entry-Ready and stuck there; now hard-rejected and demoted.) Each line shows
metrics + `/stock-research <ticker>`. Also drives the `focus → entry-ready`
watchlist promotion (same criteria, pure `_is_ready_to_enter` predicate). The
inverse pass also runs: any entry-ready watchlist row that no longer satisfies
`_is_ready_to_enter` on the current run is demoted back to `focus` and stamped
`demoted_from_entry_ready_date` — prevents tier rot when a promoted name extends.
**Peel-warn calibration cap (2026-05-29 v2):** `_peel_warn_for` now returns
`min(calibrated_warn, tier_warn(atr))`. Per-ticker calibration floors warn at
7.5 for under-sampled tickers; that floor was masking the low-vol tier's
warn=3.0 (caught ADI/TSM/LLY-class extended low-vol names). Calibration can no
longer loosen tier discipline, only tighten it. **Stale-screener demotion
(2026-05-29 v2):** Entry-Ready rows absent from the screener for ≥5 trading
days are auto-demoted to focus with `demote_reason="stale — not in screener
since <date>"`. Backfill uses `entry_ready_date` or `added` as start-of-clock
so legacy stale rows demote immediately on the first run after deploy. Full
05-29 tier audit went 26 survivors → 7 real entry-ready (16 stale + 2 cal-cap
+ 1 dist-creep removed).

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

**🌱 Stage Transition (May 2026 — software-rotation-class)** — catches early Stage 2 reclaims while the **parent sector ETF is rotating in**. Solves the Minervini "stage 2A" miss: a name 6 months into a Stage 1 base just reclaiming the 50 SMA with the 200 SMA still overhead — strict Stage 2 gate rejects, every actionable block misses, the rotation entry is invisible. Criteria: `sma20 > sma50` (21 EMA > 50 SMA proxy) · `sma50 > 0` (price above 50 SMA) · `sma200 > -5` (200 SMA within 5% overhead, or already above) · ATR% ≤7 · Q ≥70 · RVol ≥1.0 · peel-safe · not held · not in another callout · **parent ETF `rank_delta_5d ≤ -5`** (sector RS rank improving over 5d). The sector-rank gate is what makes this high-confidence rather than a junk-reclaim catcher — fires only when the sector itself is rotating in. Top 5 by Q in Slack block "🌱 Stage Transition". Watchlist: auto-enters at `priority=focus` (`source=stage_transition_auto` — sixth entry path). Gallery: collapsible `<details open>` section with ETF Δrank badge per card. ETF resolution uses `agents/utils/sector_lookup.py` (ticker map > industry substring > sector). Data dep: `data/sector_rotation_YYYY-MM-DD.json` — `sector-rotation.yml` cron moved 21:15 → 20:15 UTC so the screener reads today's snapshot. Triggered by the May 2026 software-rotation miss (semis topping while software accumulating; both mapped to XLK → invisible). Spec: [docs/specs/industry-routing-and-stage-transition.md](docs/specs/industry-routing-and-stage-transition.md).

**🐉 Recovery Leader (May 2026 — ALAB-class)** — V-recovery runners where price has reclaimed everything but the 50MA hasn't yet crossed back above the 200MA. Every Stage 2 gate rejects by design (`compute_stage` requires `sma200_pct > sma50_pct`, i.e. SMA50 above SMA200 in price terms). This block scans **Stage 0/1** with strong momentum + RS to surface the structural miss. Predicate (`_is_recovery_leader`): Stage 0 or Stage 1 · SMA20% > 0 · SMA50% ≥ 15 · SMA200% ≥ 15 · Perf Quarter ≥ 50 · RS Rating ≥ 65 · **Q ≥ 40** (2026-06-08 — was 65; a pre-Stage-2 name structurally can't earn the +25–35 Stage-2 Q bonus or +15 VCP, so its Q ceiling is ~55–60 and the old 65 gate was unreachable for the exact class this block exists to catch — OSCR-class miss) · ATR% ≤ 9 · RVol ≥ 1.0 · peel-safe · not in slow-growth sectors · not held · not in another callout. **Peel discipline holds:** a name that has already run too far (e.g. OSCR 2026-05-18 at SMA50%/ATR ≈ 12×) is correctly rejected by the unchanged peel-safe gate — the Q+RS fixes surface OSCR-class on an *earlier, less-extended, peel-safe* day, not on the blown-off candle. Top 5 by Q in Slack block "🐉 Recovery Leader" (with `:dragon:` icon and "watch only, size half" framing). Watchlist: auto-enters at `priority=watching` (`source=recovery_leader_auto` — seventh entry path). Watch-only by design — these are pre-confirm, high-vol, structural cross pending; sizing must reflect the risk. ALAB May 19 2026 is the reference case (Q=71, RS=72, RVol=1.81, sma50%+49.8, sma200%+44.9 — 50MA still below 200MA from prior drawdown). Gallery: `<details open>` section with red `pre-cross` badge per card.

**🌊 Rotation Catalyst (2026-05-28 — UMAC/ONDS drone class)** — Stage 2 setups whose parent sector ETF is rotating IN. Wider name-level bands than HTF-BR by design; sector tailwind earns the looser entry. Predicate (`_is_rotation_catalyst`): parent ETF HOT (`rank ≤ 5 AND rank_delta_5d ≤ 0`) OR strongly RISING (`rank ≤ 10 AND rank_delta_5d ≤ -5`) · Stage 2 (not requiring perfect — drone names dip SMA20) · dist52 in `[-35, 0]%` · SMA20% > 0 (close above SMA20 = reclaim confirmed) · RVol ≥ 1.0 · peel-safe · not held · not in earlier-priority callout. Top 5 by Q in Slack block "🌊 Rotation Catalyst" (sits between Stage Transition and Recovery Leader). Each Slack line shows ticker · `{ETF} {rotation_label} #{rank}/28` · Q · dist · S20 · RVol · ATR · `/stock-research <ticker>`; when single-stock ATR ≥ 7% a sub-line appends "ETF play: `{ETF}` @ $price · ATR X% — same rotation, no idio risk" to surface the lower-risk alternative. Watchlist: auto-enters at `priority=focus` (`source=rotation_catalyst_auto` — **9th entry path**), reactivates aged-out entries. Gallery: collapsible `<details open>` 🌊 section with blue `{ETF} {rotation_label}` badge per card; Top Picks hero badge `RC`. Drone tickers UMAC/ONDS added to `data/ticker_sector_map.json` → UFO (Finviz industry "Computer Hardware" / "Communication Equipment" default to XLK which is not HOT). Backtest (2026-05-27/28 replay): UMAC fired 05-27 (UFO rank=1 Δ=-7), UMAC+ONDS fired 05-28. Helper `agents/utils/rotation_label.py` maps (rank, delta, rs) → 🔥 HOT / ↗ RISING / → STABLE / ↘ FADING / ❄ COLD for the Slack line. Note: rotation_label categorical badges are used ONLY in Slack (per-ticker against one parent ETF). The `etf_rotation.html` dashboard uses plain-English `up N` (green) / `down N` (red) / `—` (gray) for the 5d move column — user rejected the HOT/STABLE/FADING categories there as visually overlapping (2026-05-29). Spec: [docs/specs/rotation-catalyst-block.md](docs/specs/rotation-catalyst-block.md).

**⚡ Episodic Pivot (May 2026 — QBTS/AMKR-class)** — Pradeep Bonde / Stockbee Setup Bar (SB) lane. Detects the **quiet day BEFORE a catalyst-driven volume explosion**, not the explosion itself. Pattern B (pullback-reversal) only — Pattern A (single-bar high-tight drift) was backtested and discarded (0% hit at +15%/5d after tightening with consecutive-bars filter). Predicate (`agents/utils/episodic_pivot.py`): `RVol ≤ 1.0` + `range_contract ≤ 0.80` + `prior_3d_cum_return ≤ -8%` + `chg_pct ≥ +3` + no expansion (RVol≥3 OR chg≥+10) in last 7 trading days. Pre-filter: `SMA50% ≥ +10`, `Perf Quarter ≥ +15` (NOT our `RS Rating` — it breaks for names whose 1Y window straddles a base move; QBTS dropped 57 → 26 in 15 days on base-effect rolloff), ATR% ≤ 12, Avg Vol ≥ 1M, Cap ≥ $500M, Price ≥ $5, sector ∉ slow-growth, industry ∉ {Biotech, Drug Manufacturers, Pharmaceutical}, ticker appeared in `finviz_screeners_*.csv` ≥1× in last 20 trading days (~300-500 candidate universe). Context tags computed from existing infrastructure: 🔥 SECTOR+PEERS · 🌊 PEERS · 📈 LEADER (SECTOR only) · ⚡ STANDALONE — via `agents/utils/sector_lookup.py` + `data/etf_rotation.json` + `data/sector_rotation_*.json`. Per-ticker dedup 20 trading days via `data/episodic_pivots.json`. **Output split:** Full cards posted to new channel `#momentum-alerts` (webhook `SLACK_WEBHOOK_MOMENTUM`); 1-line teaser appended to `#daily-alerts` ("⚡ N EP setups today (X 🔥) — see #momentum-alerts"); collapsible `<details open>` section in `finviz_chart_grid_*.html`; cross-link "EP setups: TKR (emoji)" line on each ETF card in `etf_rotation.html`; Mon/Thu sector-rotation Slack adds 🔥 lines from last 4 calendar days. Watchlist: auto-enters at `priority=watching` (`source=episodic_pivot_auto` — 8th entry path; sets `last_ep_fire_date` on existing entries too). Backtest (152 watchlist tickers × 90d): Pattern B fired 13× with 23% hit at +10%/5d and no losses worse than +0.1%. Production projection: 2-5 fires/week. Spec: [docs/specs/episodic-pivot-block.md](docs/specs/episodic-pivot-block.md). Reference cases: QBTS 2026-05-20 SB → 5/21 EP +33%, AMKR/AXTI/COHU 5/19-5/20 semis SB cluster.

**🎯 21 EMA Pullback (May 2026 — ANET/APP-class)** — continuation entries on names that ran, pulled back to the EMA21/SMA20 area, and are showing either quiet drift or active bounce. Criteria (Finviz-only — SMA20% as EMA21 proxy, Perf Month as ret20 proxy): Stage 2 (pullback-friendly: `sma200 > sma50 > 0 AND sma20 ≥ -2`) · ATR% ≤ 6 · Q ≥ 75 · SMA20% in `[-2%, +3%]` · Perf Month ≥ 12% · RVol `<1.0` (quiet drift) OR `1.0-2.5` (active bounce) · peel-safe · not held · not in another callout. Top 5 by Q in Slack block "🎯 21 EMA Pullback". Watchlist: auto-enters at `priority=focus` (`source=ema21_pb_auto`). Catches ANET Apr 22 (RVol 2.15) and APP Sep 9 2024 (RVol 1.76) class missed by RS Leader's RVol ≤ 1.5 cap. Derived from retro coverage audit ([docs/specs/retro-coverage-nbis-class.md](docs/specs/retro-coverage-nbis-class.md)).

**🌀 HTF Base Reclaim — ATR cap 7 → 8.5 (May 2026) → 10 (2026-05-25)** — first bump caught DOCN Apr 13 (ATR 8.0). Second bump 8.5 → 10 after RDW May 8 (Q84, Stage 2 perfect, ATR 9.12, dist -50%, RVol 2.76) was missed by 0.6pp and went +90% over the next 2 weeks. The `⚠ High-vol — size 50%` card badge already covers ATR > 7, so the wider cap surfaces the deep-recovery Stage 2 reclaim class (RDW-class) with the correct sizing nudge. Peel-warn gate still filters extension. Regression: [tests/test_htf_br_atr_cap.py](tests/test_htf_br_atr_cap.py).

**🎯 Ready-to-Enter / 🛡️ RS Leader — pullback-friendly Stage 2 when dist ≤ -10% (May 2026)** — at the deep edge of each block's dist band, accept Stage 2 with `SMA20% ≥ -3` (instead of requiring `SMA20% > 0`). On a legitimate pullback to EMA21/SMA20, price routinely dips 1-3% below SMA20 — the prior strict perfect ladder killed these setups (e.g. SMCI Jan 17 2024 class). Inside the band (dist > -10%) the strict ladder still applies.

**🛡️ RS Leader (Phase 1 + Phase 2, May 2026)** — catches DOCN-class setups (single-screener, never hits persistence gate, VCP low) by detecting stock-level relative strength independent of market_state. Predicate (`_is_rs_leader_candidate`): Stage 2 perfect · Q ≥ 75 · dist [-10%, +2%] from 52w high · rising MA stack (SMA20/50/200 all > 0) · ATR% ≤ 8 · peel-safe (SMA50%/ATR% ≤ peel_warn) · RVol ≤ 1.5 · **RS Rating ≥ 60** · not in {Utilities, Energy, Real Estate, Basic Materials, Consumer Defensive} · not held. **No market_state gate** — trigger_state is logged for analytics only. Scans `summary_df` (pre-10%-gate) like Hidden Growth. Persistent tracker in `data/rs_leaders.json` with 14-day pullback grace. Lifecycle: active → pulling_back → reacquired (or aged_out). Slack: 🛡️ NEW / REACQUIRED / 📉 pulling back — lines show `RS {rating} · Q{score} · dist`. Watchlist: first-trigger `new` and `reacquired` auto-enter at `priority=focus` (`source=rs_leader_auto`). Gallery: `🛡️ Relative Strength Leaders` collapsible section with NEW/REACQUIRED + purple RS badge. DOCN Apr 6 2026 is the reference case (Q=84, dist -4.9%, mult 4.1x, RVol 0.78). **Phase 2 — RS Rating (`_compute_rs_ratings`):** IBD-style weighted composite `(Perf Quarter × 0.4) + (Perf Half Y × 0.3) + (Perf Year × 0.3)` (9M approximated as average of 6M+12M, weights simplify to 0.3+0.3). Percentile-ranked within today's screener universe → integer 0–99. Computed after Quality Score, stored as `RS Rating` column on `summary_df` and persisted in `rs_leaders.json`. Data source: Finviz snapshot fields `Perf Half Y` and `Perf Year` (extends existing 14-tuple to 16-tuple). **Perf-Quarter top-quintile override (2026-06-08 — OSCR/QBTS fix):** an explosive 90-day mover can be dragged under by a stale 1-year base (OSCR +89% quarter but +44% year → raw composite RS 61, under the Recovery-Leader RS≥65 gate). The function also percentile-ranks a quarter-only series; when a ticker's quarter percentile is top-quintile (≥80), its final RS is floored at that quarter rank (`max(composite_pctile, quarter_pctile)`). Mid-quarter names (quarter percentile <80) keep their composite rating untouched. Net effect: OSCR 61→~90.

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
| DANGER | 500+ stocks down 4%+ today AND (5d ratio < 0.5 **OR** dn4 ≥ 3 × up4) | 2 | ↓ hard | No entries, raise stops immediately. v4 (May 2026) widened to catch catastrophic single-day distribution (05-15: 535 vs 110) even when 5d hasn't yet deteriorated. |
| **EXTENDED** | Trip: SPY ATR mult ≥ 7 **OR** SPY %above 50MA ≥ 8 **OR** QQQ ATR mult ≥ 9. v4 stickiness (May 2026): once tripped, stay EXTENDED while SPY close ≥ 21 EMA AND > 50 SMA — the metric is NOT required during stay (digestion pulls to 8/21 EMA must not exit). Exits: 3 consecutive closes below 21 EMA → COOLING; any close below 50 SMA → RED. Re-entry from COOLING/CAUTION requires metric trip + new 20d close high. Re-entry from RED/DANGER/BLACKOUT is forbidden — must come up through CAUTION first. | 3 | ↑↑ blow-off | **No new entries** — parabolic tape, tighten stops, no chase. Overrides THRUST/GREEN/CAUTION/TREND-FOLLOW/STEADY. Counters persisted: `extended_since_date`, `days_below_21ema`. |
| COOLING | prev_state == GREEN AND GREEN conditions no longer met | 4 | ↓ fading | Trim positions, tighten stops, no new entries |
| THRUST | 500+ stocks up 4%+ today (Bonde "Very High" buying pressure) | 5 | ↑ signal | Start building watchlist NOW |
| GREEN | 5d ratio >= 2.0, 10d >= 1.5, F&G >= 35, SPY above 200d MA | 6 | ↑ bull | Full size entries |
| **TREND-FOLLOW** | All 6 v3 gates (SPY>SMA50>SMA200, SMA50 rising 10d, SPY within 3% of 20d high, participation proxy ≥ 8%, VIX calm, not EXTENDED) AND v4 (May 2026): `prev_state ∉ {EXTENDED, RED, DANGER, BLACKOUT, COOLING}` AND `dn4 < 2 × up4` (breadth sanity). Continuation path — must follow GREEN/THRUST/CAUTION/STEADY-UPTREND. Out of EXTENDED runs through COOLING → CAUTION → GREEN/THRUST. | 7 | ↑ steady trend | **Full size, entries allowed.** Trend-persistence path independent of 5d/10d thrust ratio. Rides steady grind-up tapes (Apr 24–May 5 2026 reference). v4 breadth-sanity gate rejects distribution days (05-15: 110 vs 535 = 4.86×). |
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
- **Finviz scraping:** Rotating user agents, exponential backoff, no proxy. Rate-limit-friendly delays between requests. **Snapshot parsing (2026-07-03):** the `quote.ashx` technical fields are read by collecting ALL `<td class="snapshot-td2">` cells page-wide and pairing them `[0::2]`(key)/`[1::2]`(value) — NOT by finding `<table class="snapshot-table2">` and walking rows. Finviz moved the technical fields (ATR/SMA/Price/Perf) out of that table on ~2026-06-29 while leaving the `snapshot-td2` cell class intact; the old table-walk returned only ~14 fundamental keys → ATR%=0 for the whole universe → screener silently posted "0 passed" and exited green for 3 days (June 29–July 2). Same pattern lived in 6 files: `finviz_agent.py`, `finviz_weekly_agent.py`, `earnings_alert.py`, `alerts_agent.py`, `market_monitor.py`, `position_monitor.py` (2×), `generate_dashboard.py`. **Regression guard:** `scrape_canary()` (Step 0 in `finviz_agent.py` `__main__` — scrapes AAPL, raises `ScrapeHealthError` if ATR% is None/0) + `assert_scrape_healthy(summary_df)` (post-scrape backstop, raises if the ENTIRE universe has ATR%=0 AND SMA50%=0). Both propagate non-zero so `daily-finviz.yml`'s `if: failure()` step fires the `#general-alerts` Slack alarm. Tests: `tests/test_finviz_agent.py::TestScrapeHealthGuard`. The mock fixture `make_mock_snapshot_html()` now emits flat `snapshot-td2` cells (matching real Finviz) — the old table-based mock passed while production was broken, which is exactly what let the break hide.
- **Weekly agent:** Uses Claude API with `web_search` tool for catalyst research (~$0.10-0.20/run).
- **Alpaca daily-bars fetches MUST pass an explicit `start` param (2026-07-10):** `/v2/stocks/{ticker}/bars` with no `start` defaults to the current day and returns `bars: null` — `fetch_alpaca_daily_bars` (position_monitor) and `fetch_daily_closes` (alpaca_monitor) were silently returning `[]` on every call, no-oping the SMA5 stop filter and the Layer 1b MA trail. Both now send `start = today − 2×limit days, limit=1000` and slice the last N bars. All other bars callers (swing_pivot, episodic_pivot, calibrate_peel, market_monitor, sector_rotation) already passed `start`. When adding a new bars fetch, always pass `start`.
