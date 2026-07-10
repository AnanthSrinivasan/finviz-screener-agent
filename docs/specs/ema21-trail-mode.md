# 21 EMA Trail Mode — low-vol runners (paper lab)

**Problem (VIK 2026-07-02):** the ATR-from-peak tier trail measures *noise* on low-volatility compounders in orderly trends. VIK (ATR 4.23%, peak +24.0%) was auto-sold at $100.07 when the ~1×ATR trail off the $105.66 peak fired — while the 21 EMA, which had defined the entire 4-month advance (every touch bought, including the 7/10 tag at 98.44 vs EMA 98.45), never broke. Eight days later VIK traded $100.10: the exit bought certainty worth 3 cents and sold the option on the trend's continuation. For the $150k goal the books need to occasionally ride a +24% into a +45% (DAVE-class leg: +41% in 29 days post-breakout); the ATR trail structurally cannot.

**User decision (2026-07-10):** approved for spec — "agree spec out fix for VIK type names with that atr% to trail 21ema."

## Rule

**Scope: PAPER BOOK ONLY** (`alpaca_monitor.py`). Manual and live books unchanged. Paper is the lab; live adoption only after the measurement gate below.

**Mode activation** (per position, checked each run):
- `atr_pct ≤ 5.0` (VIK/OSCR class — low-vol) AND `peak_gain_pct ≥ +20` (the runner remainder, post-T1-peel)
- Once activated, `trail_mode = "ema21"` persists for the life of the position (no flip-flopping back to ATR mode if ATR drifts).

**Exit trigger in ema21 mode:** **2 consecutive daily closes below the 21 EMA** — evaluated on the post-close run only (22:00 UTC pass, same data path as Layer 1b). Intraday runs do NOT exit on the trail while the mode is active (they still process floors below). Matches the Layer 1b GREEN/THRUST regime convention and the user's MA framework (orange 21 EMA = trend line for this class).

**Floors always win — unchanged, never lowered, evaluated every run including intraday:**
- Breakeven floor (`entry × 1.005` once armed), hybrid loss-cap floor, hard dollar stop.
- +30% disaster floor `peak × 0.90` stays — on an ATR≤5 name the 21 EMA sits ~5–8% off peak so the EMA normally fires first; the 0.90 floor covers gaps and crash days.
- T1/T2 peel logic unchanged; mode governs only the trail on the remainder.

**New state fields** (`paper_stops.json`): `trail_mode`, `ema21_close_breaches` (consecutive counter), `trail_mode_since`.

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
