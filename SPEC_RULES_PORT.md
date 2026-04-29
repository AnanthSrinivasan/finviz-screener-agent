# Spec — Live position_monitor parity port to `rules.py`

**Goal:** Make live `agents/trading/position_monitor.py` consume the shared `agents/trading/rules.py` engine that paper already uses, so paper and live behave identically. Eliminate ~200 lines of duplicated rules code in the live file.

**Status of refactor:** Paper side landed in commit `753361c`. Live side ("position_monitor parity port pending" per CLAUDE.md) is what this spec covers.

---

## A. Pure name/rename diffs

| Concept | Live (current) | Shared (`rules.py`) | Action |
|---|---|---|---|
| Trail/breakeven/targets fn | `apply_minervini_rules` | `apply_position_rules` | Live wrapper delegates; old name kept as thin alias for one release |
| MA trail fn | `check_ma_trail_violation` | `check_ma_trail_alert` | Same — alias |
| Stop field (persisted) | `position["stop"]` | `entry["stop_price"]` | **Adopt rules.py keys.** One-shot migration of `data/positions.json` |
| Breakeven flag (persisted) | `breakeven_stop_activated` | `breakeven_activated` | **Adopt rules.py keys.** Migrated alongside `stop` |
| `recent_trades` cap | `[-20:]` | `[-50:]` | **Both → `[-30:]`** |

Migration script also clears any persisted `status="stop_hit"` → `"active"` (see B.1).

## B. Behavioral diffs (deeper, not pure renames)

### B.1 Stop-hit (Rule 1)
Live currently sets `position["status"] = "stop_hit"` and appends a `_append_recent_event` entry. Both wrong:
- Position should stay `active` — user often holds through the alert (FIGS pattern). The system only signals; the human decides.
- recent_events feed is for market events, not position events.

**Fix:**
- Stop-hit fires the 🚨 Slack alert only. No `status` mutation. No `_append_recent_event`.
- Remove the now-dead "Stale stop_hit override" block in `sync_snaptrade_with_rules` (lines ~999–1008) — never triggers once we stop setting the flag.
- Stays in the live caller (NOT in shared rules.py); paper handles its own stop-hit.

### B.2 ATR units in `apply_position_rules`
Live caller passes `atr` (dollars). `rules.py` takes `atr_pct` (%) and computes dollars internally.
**Fix:** caller passes `atr_pct` (already available from Finviz metrics). One signature.

### B.3 Side effects to remove from rules engine
Currently inside live `apply_minervini_rules`:
- `_append_recent_event(...)` × 4 (stop_hit, breakeven, T1, T2) — **all removed.** recent_events stays market-only.
- `_save_winner_chart(ticker, "T1", today_str)` on T1 — **removed from rules engine.** Winner chart fires only on actual position close with profit (auto-close branch when SnapTrade closes the position AND `result_pct > 0`). The manual-sell branch (`handle_trade_input`) already does this at line 1472; we add the equivalent to the auto-close branch around line 1080.
- `position["stop_type"] = "atr_trail"` tag — drop (unused downstream).
- `position["current_gain_pct"]` write — drop (recomputable from price/entry).
- `log.info / log.warning` lines — moved to live wrapper, keyed off the structured events the engine returns.

### B.4 Structured event return from `rules.apply_position_rules`
Engine returns a list of `{kind: str, message: str, ...payload}` events instead of plain Slack strings. Kinds: `breakeven`, `trailing_stop`, `target1`, `target2`, `fade`. Live wrapper:
- Forwards `message` to Slack.
- Writes a log line per event.
- Paper wrapper: just forwards messages, no extra side effects.

Stop-hit is NOT a kind from the engine — it's handled in the live caller before/after the engine call (engine doesn't see hard stops).

### B.5 MA trail purity
Live `check_ma_trail_violation` (lines 461–523) duplicates `_ema`, `_ma_trail_signal_for_atr`, and `_MA_TRAIL_REGIME` from rules.py. Bit-for-bit identical logic.

**Fix:**
- Delete live `_ema`, `_ma_trail_signal_for_atr`, `_MA_TRAIL_REGIME`, `check_ma_trail_violation`.
- Caller fetches bars via existing `fetch_alpaca_daily_bars(ticker)`, passes `closes` to `rules.check_ma_trail_alert(closes, market_state, atr_pct, highest_price_seen)`.
- Same return shape (`{ma_type, consecutive, last_close, last_ema, atr_pct, tier}` or None).
- Slack output unchanged — caller's existing formatter consumes the dict.

**Parity check:** unit test feeds identical inputs to old `check_ma_trail_violation` and new `check_ma_trail_alert`, asserts equal output across the regime grid (THRUST/GREEN/CAUTION/COOLING × low/mid/high-vol ATR).

### B.6 `update_sizing_mode` alert coverage
Live only emits Slack alerts on transitions to `suspended` / `reduced`. Shared engine also emits on `aggressive` and `normal` transitions. **Adopt shared (more visibility on regime relaxation/tightening).** Already what paper does.

### B.7 Trade record schema
Shared `record_trade_result` lacks the `profit_loss_usd` field that live includes. **Add `profit_loss_usd` as optional kwarg.** Live passes it; paper passes None.

### B.8 Auto-close inline block (lines 1080–1115)
30-line block hand-rolls neutral/win/loss classification, streak update, `recent_trades.append`, cap. **Replace with single call** to `rules.record_trade_result(trading_state, ticker, result_pct, today, side="SELL", source="auto_detected", profit_loss_usd=_pnl_usd)`.

T1 winner chart save (B.3) gets added here, gated on `result_pct > 0`.

## C. Out of scope (not touched)
- SnapTrade fetch/sync logic, retro-patch closed positions, share-drift reconcile (avg-up / partial sell)
- Slack formatting / alert routing / dashboard rendering
- Paper-side code (already on shared engine)
- `paper_trading_state.json` ↔ `trading_state.json` separation

## D. Migration script (`utils/migrate_positions_keys.py`, throwaway)
For `data/positions.json`:
- For every position in `open_positions` and `closed_positions`:
  - If `stop` present and `stop_price` absent → rename.
  - If `breakeven_stop_activated` present and `breakeven_activated` absent → rename.
  - If `status == "stop_hit"` → set to `"active"` (B.1 implies no live position should carry that flag anymore).
- Idempotent. Run once, commit the migrated file, delete the script after the next push.

## E. Task list (execute in order, one commit per logical chunk or one commit at the end — TBD)

1. Scan repo for all references to `position["stop"]`, `breakeven_stop_activated`, `apply_minervini_rules`, `check_ma_trail_violation`. Catch dashboards / utils / tests.
2. Run migration script on `data/positions.json`. Commit migrated file separately.
3. Update `rules.py`:
   - `apply_position_rules` returns list of `{kind, message, ...}` events.
   - `record_trade_result` accepts `profit_loss_usd=None`.
   - `recent_trades` cap → 30.
4. Update tests for `rules.py` (event shape, profit_loss_usd, cap).
5. Live `position_monitor.py`:
   - Replace `apply_minervini_rules` body: keep Rule 1 (stop hit, alert-only), delegate rest to shared. No `_append_recent_event`, no T1 winner chart, no `stop_type`, no `current_gain_pct`.
   - Replace `update_sizing_mode` with shared.
   - Replace inline auto-close trade record (1080–1115) with `rules.record_trade_result(...)`. Add T1 winner chart here, gated on profit.
   - Delete `_ema`, `_ma_trail_signal_for_atr`, `_MA_TRAIL_REGIME`, `check_ma_trail_violation`. Port caller to `rules.check_ma_trail_alert`.
   - Delete dead "Stale stop_hit override" block in `sync_snaptrade_with_rules`.
   - Rename any remaining `breakeven_stop_activated` writes (e.g. avg-up reset at line 1029) to `breakeven_activated`.
6. Update paper side ONLY if cap or signature changes affect it (cap to 30 + profit_loss_usd kwarg accept).
7. Update tests:
   - MA trail parity test (input grid → equal output between deleted live fn and shared fn).
   - Live `apply_minervini_rules` wrapper test (event forwarding, stop-hit alert without status mutation).
   - Migration script test.
8. `python -m unittest discover -s tests -t .` — must remain green, plus new tests.
9. Run `gh workflow run position-monitor.yml` + `gh run watch <id>`. Verify logs: trail/breakeven/T1/T2 events fire, no field-name KeyErrors, MA trail alerts fire correctly.
10. Update `CLAUDE.md` (drop "live position_monitor parity port pending" note) + `SYSTEM_DOCS.md`.
11. Commit, push, verify GitHub Actions run.

## F. Open decisions still requiring confirmation

None. All previously open questions resolved:
- Persisted-key strategy → adopt rules.py keys, migrate positions.json.
- Structured events → yes (B.4).
- `recent_trades` cap → 30 both sides.
- Stop-hit → alert only, no status mutation, no recent_event (B.1).
- T1 winner chart → auto-close branch only, on profit (B.3 / B.8).
- MA trail → shared engine, parity test (B.5).
- Sizing mode → shared (B.6).
- profit_loss_usd → optional kwarg, both sides (B.7).

## G. Regression risks

- **Field rename**: any caller still reading `position["stop"]` / `breakeven_stop_activated` post-port crashes. Mitigation: full repo grep in step E.1, fix every site, add a test that opens current `positions.json` and exercises one full position monitor pass.
- **MA trail parity**: a subtle EMA-window or consec-needed mismatch silently changes alert timing. Mitigation: explicit parity test (E.7).
- **Winner chart**: if T1 chart save was relied on for the dashboard gallery, removing it from T1 means we lose those mid-trade snapshots. User confirmed: chart save belongs at close-on-profit only.
- **No status=stop_hit**: dashboards or filters keying off this status no longer see it. Acceptable per user (system only signals; human decides).
