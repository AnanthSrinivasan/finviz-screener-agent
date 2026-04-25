# Backlog

Living list of feature/fix work for the screener system. Mark items `✅` when shipped (with the commit hash), `🚧` when in flight, `⏳` when pending.

## Pending

| ID | Item | Priority | Status | Notes |
|---|---|---|---|---|
| B-01 | Performance charts page — populate 2026 YTD numbers | med | ⏳ | Needs user's Robinhood 2026 YTD transaction report (CSV) — system has no historical broker data before SnapTrade was wired in. |
| B-02 | Open positions: rename `Entry` → `First Entry`, add new `Avg Price` column | low | ⏳ | First Entry = original avg cost when first opened; Avg Price = current weighted (post avg-up). Both already in `positions.json`; just a dashboard column add. |
| B-03 | Move closed positions to a separate page | low | ⏳ | Standalone `closed.html` linked from dashboard. Reuse the expandable timeline component already built. |
| B-04 | Add SPY / QQQ / TNA / IWM tile-row to dashboard | low | ⏳ | Already in MACRO_WATCHLIST in weekly; need a compact tile component on dashboard near Crypto/Market State. Could just embed the macro snapshot from weekly. |
| B-05 | More categories in `recent_events.json` feed | med | ⏳ | Currently only `market_state`. Add `position_close`, `target_hit`, `breakeven`, `stop_hit`, `peel_signal`, `retro_patch`. Helper already exists — just append from each call site. |
| B-06 | Auto-MAE/MFE rebuild after each close | low | ⏳ | `utils/analyze_mae.py` is ad-hoc. Wire into position-monitor post-close so the analysis stays fresh without manual run. |
| B-07 | Calibrate emerging-score weights against historical Top 5 transitions | low | ⏳ | Track which "Next on the Radar" candidates make Top 5 the following week; tune CC_WATCH/EP/HIGH multipliers from outcomes. |

## Done this session (2026-04-25)

| ID | Item | Commit | Notes |
|---|---|---|---|
| D-01 | ATR%-tiered MA trail (≤5%→21EMA · 5–8%→8EMA · >8%→10% trail) | `1acd91a` | Fixes FLY/PL profit-giveback class. |
| D-02 | Auto-close uses real SnapTrade SELL fill, not peak high | `1acd91a` | Falls back to live quote → `highest_price_seen`. |
| D-03 | Neutral 1% band (BREAKEVEN exits don't bump streak/sizing) | `1acd91a` | `recent_trades.result = "neutral"`. |
| D-04 | Breakeven (+20%) + trail (+30%) key off `peak_gain_pct`, not current | `1acd91a` | Brief intraday touch locks floor forever. |
| D-05 | SnapTrade activities parser hardened (envelope + string shapes) | `67c2f7b` | Handles `{data:[]}`, `{activities:[]}`, bare list, stringy symbols. |
| D-06 | Share-drift reconcile (avg-up + partial sell branches in sync) | `55d72ae` | Found 5 stale ticker counts in production on first run. |
| D-07 | Stale `stop_hit` auto-reset when SnapTrade still holds | `061c87a` | User override path — stop value left intact. |
| D-08 | Retro-patch closed records when broker activity arrives late | `bf4db74` | Scans last 14d of fallback/user-reported closes; rewrites with real fill, adjusts win/loss totals. |
| D-09 | Retro-fix AMD/FLY/PL closed records with real fills + corrected streak | `061c87a`, `a52cfa6` | AMD weighted SELL $293.09 (+19.1%), FLY $35.13 (0%), PL $35.49 (+2.31%). |
| D-10 | Per-position transaction timeline (expandable child rows on dashboard) | `17456fa` | 90-day SnapTrade activity grouped by ticker; running cost basis shown. |
| D-11 | Per-row timeline filter: entry_date + system-wide 2026-04-01 floor | `284e973` | Stops prior trade cycles polluting the view (e.g. FIGS Mar round-trip). |
| D-12 | Recent Alerts widget reads new `data/recent_events.json` rolling feed | `284e973` | Severity color bar (red/amber/green). 6 backfilled state transitions. |
| D-13 | Market-state transitions auto-append to `recent_events.json` | `284e973` | Helper `_append_recent_event` reusable for future categories. |
| D-14 | Weekly: 🔭 Next on the Radar — predictive emerging candidates section | `8b06717`, `012f4ca` | Stage 2 + Q≥70 + fresh catalyst, excluding Top 5 + held. Bugfix `012f4ca` accepts Weinstein "Uptrend" label. |
| D-15 | Weekly page reorder: macro lifted above AI brief | `8b06717` | Read environment first, then setups. |

## Older / archived items

(Track here once we close out a meaningful chunk.)
