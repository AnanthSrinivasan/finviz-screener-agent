# Flush-Suppress Stop Filter

**Problem (TEM 2026-07-09):** a position-level ATR trail can be triggered by an *index-level* flush while the name's own structure is intact. TEM's stop ($57.35) was breached at the 7/8 close ($57.31) on Day 2 of a market flush (7/7: 454 down-4% vs 150 up); the name never broke its 21 EMA ($54.88) and ripped to $61.49 the next session. Exit was rule-correct and still cost ~$870 vs one more day. DAVE breached its stop at TWO closes (7/7, 7/8) while holding its 8 EMA; the user's discretionary hold was right (+$2,642 peel at $390). TNA broke stop AND both EMAs — correct exit (real loser).

## Reference outcomes (7/8 close)

| Name | Stop breach | 8 EMA | 21 EMA | Correct call | Rule that gets it right |
|---|---|---|---|---|---|
| TEM | yes ($57.31 ≤ $57.35) | below | ABOVE | hold | 21 EMA test |
| DAVE | yes (2nd day) | ABOVE | ABOVE | hold | 8 EMA test (high-momentum) |
| TNA | yes | below | below | exit | any |

## Rule

When a stop (ATR trail / dynamic stop) is breached, **suppress the exit** iff ALL of:

1. **Market flush window active:** a session with ≥400 down-4% stocks occurred within the last 2 trading days, AND SPY close > 50 SMA, AND VIX < 20. (Real breakdowns — June 2026 selloff, SPY structure failing — never activate the filter; stops behave exactly as today. VIK-Apr and June-6 are regression cases that MUST NOT suppress.)
2. **Name structure intact at the close:**
   - High-momentum name (`Perf Month ≥ 40%` at entry-check time, tunable): close above **8 EMA**. Per user 2026-07-10: "8 EMA breach is bad for high momentum names" — DAVE class gets the tighter test.
   - Otherwise: close above **21 EMA**. (TEM class — saved.)
3. **Profit floor:** current price ≥ entry AND ≥ loss-cap floor. Suppression only protects winners from whipsaw; it never widens a loss.
4. **Expiry:** suppression lives only while the flush window lasts (max 3 sessions from the flush day). Any close below the structure EMA → exit at that close, no second chance. Stop is NOT lowered — if price is still below stop when the window expires, exit.

Evaluation is **close-based** (like the user's DAVE hold), computed on the post-close run; intraday runs during an active suppression emit `flush_suppress` info events instead of stop alerts.

## Scope & rollout

- **Manual book (`position_monitor.py`):** alert text changes to `🛡 STOP BREACHED — FLUSH SUPPRESS (holding: above {8|21} EMA ${x}, flush day {n}/3)`. Human still decides.
- **Paper (`alpaca_monitor.py`):** gates the auto-sell immediately (user-approved 2026-07-10).
- **Live retrofit gate:** enable only after BOTH: (a) replay backtest over last 90 days of stop-outs shows saves ≥ 2× damage ($ terms), and (b) ≥3 real suppression events observed on paper with net positive outcome, minimum 4 weeks. Then set `"flush_suppress_live": true` in `data/live_alpaca_trading_state.json` via a manual dispatch input (same pattern as `live_reenable`). Until then live behaves as today.

## Implementation sketch

- `agents/trading/rules.py`: new pure fn `should_suppress_stop_exit(closes, current_price, atr_pct, entry_price, perf_month, flush_ctx) -> (bool, reason)` + `flush_window_active(history) -> ctx` reading `market_monitor_history.json` (down_4_today, spy_sma50_pct, vix_close). EMA helpers already exist in Layer 1b path.
- Callers: paper sell path in `alpaca_monitor.py`; manual alert path in `position_monitor.py` (Layer 2 Rule 1 / Rule 5 stop checks). Both already fetch daily closes for the SMA5 filter / MA trail — reuse.
- Existing SMA5 filter (ATR ≤5%) unchanged; this rule is orthogonal (market-gated, all tiers).
- New state fields on position: `flush_suppress_active`, `flush_suppress_day`, dedup date.
- Tests: `tests/test_flush_suppress.py` — TEM/DAVE/TNA reference fixtures + June-selloff non-activation + profit-floor + expiry.
- Backtest: `scripts/replay_flush_suppress.py` over closed positions w/ Alpaca bars; report saves vs damage in $.

## Open items

- `Perf Month ≥ 40%` momentum threshold — validate against DAVE (+~40) / TEM (+14) in replay; tune.
- Whether suppression should also gate the loss-cap floor stop (currently: no — floor always wins).

## Rollout status (2026-07-10 — shipped)

- Implemented: `rules.flush_window_active` / `should_suppress_stop_exit` /
  `evaluate_flush_suppress` (+ `flush_suppress` in `CRITICAL_EVENT_KINDS`);
  manual-book alert path in `position_monitor.apply_minervini_rules`; paper
  auto-sell gate in `alpaca_monitor`; live behind `flush_suppress_live` in
  `live_alpaca_trading_state.json` (off). Tests: `tests/test_flush_suppress.py`.
- Side fix shipped with this spec: Alpaca daily-bars fetches in both monitors
  were returning `[]` (missing `start` param — API defaults to current day),
  which had silently no-oped the SMA5 filter and Layer 1b MA trail. Fixed.
- **First replay result (90d, stop-driven exits only): saves $643 vs damage
  $1,805 → 0.36x. Live gate (≥2x) NOT met.** Damage concentrated on June 3–4
  2026 stop-outs held into the June 5 break: 6/3 read down4 496 / SPY +6.3%
  over 50SMA / VIX 16.1 — the first day of a real selloff passes the
  SPY/VIX guard and is indistinguishable from a benign flush at that point.
  The 7/7–7/9 window (TEM/DAVE reference) nets positive. Candidate tunings if
  the paper observation confirms the pattern: require F&G/5d-ratio sanity,
  or activate only from flush day 2 (a second session of held index structure).
- Caveat: the replay's actual-exit prices are recorded closes (broker fills),
  so the TEM 7/8 reference save itself is filtered out (its recorded fill was
  7/9 @ 60.45, above stop×1.02). Treat the replay as directional; the paper
  observation window is the binding half of the gate.
