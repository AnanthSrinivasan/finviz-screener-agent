# 21 EMA Trail Mode — low-vol runners (paper lab)

**Problem (VIK 2026-07-02):** the ATR-from-peak tier trail measures *noise* on low-volatility compounders in orderly trends. VIK (ATR 4.23%, peak +24.0%) was auto-sold at $100.07 when the ~1×ATR trail off the $105.66 peak fired — while the 21 EMA, which had defined the entire 4-month advance (every touch bought, including the 7/10 tag at 98.44 vs EMA 98.45), never broke. Eight days later VIK traded $100.10: the exit bought certainty worth 3 cents and sold the option on the trend's continuation. For the $150k goal the books need to occasionally ride a +24% into a +45% (DAVE-class leg: +41% in 29 days post-breakout); the ATR trail structurally cannot.

**User decision (2026-07-10):** approved for spec — "agree spec out fix for VIK type names with that atr% to trail 21ema."

## Rule

**Scope: PAPER BOOK ONLY** (`alpaca_monitor.py`). Manual and live books unchanged. Paper is the lab; live adoption only after the measurement gate below.

**Mode activation** (per position, checked each run):
- `atr_pct ≤ 5.0` (VIK/OSCR class — low-vol) AND `peak_gain_pct ≥ +20` (the runner remainder, post-T1-peel)
- Once activated, `trail_mode = "ema21"` persists for the life of the position (no flip-flopping back to ATR mode if ATR drifts).

**Exit trigger in ema21 mode (v2 — user refinement 2026-07-10: "lower low is the problem under the 21 EMA"):** evaluated on the post-close run only (22:00 UTC pass, same data path as Layer 1b). Intraday runs do NOT exit on the trail while the mode is active (they still process floors below). Exit when EITHER:

1. **Breakdown (primary):** daily close < 21 EMA **AND** daily close < the swing low — defined as `min(low of the prior 10 sessions, excluding today)`. A close below the EMA that holds above prior lows is a shakeout (OSCR-etched pattern) → HOLD. A close below the EMA that takes out the lows is trend damage → exit at that close.
2. **Camping fallback:** 4 consecutive daily closes below the 21 EMA, even with no lower low — a dead trend must not squat under the EMA indefinitely on equal lows. Counter resets to 0 on any close back above the EMA.

Reference sanity: VIK 7/2 closed ABOVE its 21 EMA (the old ATR trail fired intraday) → ema21 mode holds. VIK 7/10 tagged the EMA intraday (low 98.44 vs 98.45), no close below → holds, counter 0.

**Floors always win — unchanged, never lowered, evaluated every run including intraday:**
- Breakeven floor (`entry × 1.005` once armed), hybrid loss-cap floor, hard dollar stop.
- +30% disaster floor `peak × 0.90` stays — on an ATR≤5 name the 21 EMA sits ~5–8% off peak so the EMA normally fires first; the 0.90 floor covers gaps and crash days.
- T1/T2 peel logic unchanged; mode governs only the trail on the remainder.

**New state fields** (`paper_stops.json`): `trail_mode` ("ema21" | absent = ATR), `ema21_close_breaches` (consecutive counter), `trail_mode_since`.

## Implementation checklist (APPROVED — build without further questions)

1. `agents/trading/rules.py`: pure fn `ema21_trail_verdict(closes, lows, current_close, atr_pct, peak_gain_pct, breach_count) -> {action: hold|exit|activate, reason, new_breach_count, ema21, swing_low}`. EMA helper exists (Layer 1b path). Swing low = `min(lows[-11:-1])`.
2. `agents/trading/alpaca_monitor.py` post-close pass: activation check (atr ≤5, peak ≥20, mode not set) → set `trail_mode`; when mode set, replace the tier-trail sell decision with the verdict fn; floors (breakeven / loss-cap / peak×0.90 / hard stop) unchanged and still checked every run including intraday.
3. A/B log: append `{date, ticker, close, ema21, atr_stop, atr_would_exit, ema21_exited, breach_count}` to `data/trail_mode_ab.json` each post-close run for every ema21-mode position.
4. Slack on exit: `[PAPER] 📐 21EMA TRAIL EXIT {ticker} — close ${c} < 21EMA ${e} + lower low ${sl}` (or `camping 4d`).
5. Tests `tests/test_ema21_trail.py`: VIK 7/2 hold (close above EMA) · EMA tag no close-below → hold · close<EMA but no lower low → HOLD (the shakeout case) · close<EMA + lower low → exit · 4-day camping → exit · reclaim resets counter · ATR 6% never activates · gap below peak×0.90 → floor exits regardless of mode.
6. Run full suite, push, then trigger position-monitor workflow and verify logs per CLAUDE.md rule 3.

## Measurement (the point of the experiment)

Every post-close run, for each ema21-mode position, log BOTH verdicts — what the ATR tier trail would have done vs what ema21 mode did — to `data/trail_mode_ab.json` (`{date, ticker, price, atr_stop_would_exit: bool, ema21_exited: bool, ema21, atr_stop}`). **Review gate: 8 weeks or 5 completed ema21-mode exits, whichever first.** Compare realized exit prices A vs B per trade. Only after that review do we discuss the manual book or live.

## Tests (`tests/test_ema21_trail.py`)

- VIK replay fixture: mode activates at +20% peak; does NOT exit on 7/2 (no close below 21 EMA); still holding through 7/10 intraday tag (low 98.44 vs EMA 98.45 — a tag is not a close).
- Breakdown case: 2 consecutive closes below 21 EMA → exits at second close; breakeven floor honored.
- Gap-crash case: price gaps below `peak × 0.90` intraday → floor exits immediately, mode irrelevant.
- One close below then reclaim → counter resets to 0.
- ATR 6% name at +25% peak → mode never activates (tier trail as today).

## Reference cases

VIK 7/2 exit (this spec's trigger) · OSCR Mar–Jul 2026 (21 EMA metronome, user-etched shakeout pattern) · DAVE May–Jul 2026 (+41%/29d leg a tight trail would have surrendered) · flush-suppress spec (same close-based structure-EMA philosophy, [docs/specs/flush-suppress-stop-filter.md]).
