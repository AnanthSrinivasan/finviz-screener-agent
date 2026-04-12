# Peel Calibration — ATR% Multiple Algorithm

Reference for reasoning about per-ticker peel (scale-out) thresholds used in `position_monitor.py`.
Production output: `data/peel_calibration.json`. Production script: `utils/calibrate_peel.py`.

## Formula

```
atr_pct_multiple = (close - SMA50) * close / (SMA50 * ATR14)
```

Equivalent decomposition:
```
= ((close - SMA50) / SMA50)   ← % distance above/below 50MA
  /
  (ATR14 / close)              ← ATR as % of price
```

Matches TradingView's "ATR% Multiple" indicator exactly.

**Verification — AAOI Jun 16 2023:**
- close = 4.85, SMA50 = 2.25, ATR14 = 0.259
- = (4.85 − 2.25) × 4.85 / (2.25 × 0.259) = **21.6x** ✓ (chart reads ~22x)

## ATR14 — Wilder's Smoothing

```
TR[i] = max(high[i] - low[i], |high[i] - close[i-1]|, |low[i] - close[i-1]|)
ATR[14] = average(TR[1..14])          ← seed: simple average of first 14 TRs
ATR[i]  = (ATR[i-1] × 13 + TR[i]) / 14   ← Wilder smoothing thereafter
```

## SMA50

```
SMA50[i] = average(close[i-49 .. i])
```

Both indicators require warmup: SMA50 needs 50 bars, ATR14 needs 14+1=15 bars (due to true range needing prev close).

## Run Detection

A **run** = continuous period where `atr_pct_multiple > 0` (stock trading above 50MA).

Rules:
- Minimum run length: **10 consecutive days** above 50MA
- Run ends when `atr_pct_multiple <= 0` (crosses below 50MA)
- Missing data (None) also ends a run
- In-progress runs at end of data are included
- Only runs with a peak `>= 5.0x` qualify (filters noise on low-volatility stocks)

For each qualifying run, extract the **peak** value (max ATR% multiple during that run).

## Threshold Computation

Collect all qualifying run peaks for a ticker. Then:

```
signal_threshold = max(P75_of_peaks, 10.0)   ← floor at 10x
warn_threshold   = max(P75 × 0.75, 7.5)      ← floor at 7.5x
```

Minimum 3 qualifying runs required. Fewer runs → "insufficient_runs" → fallback to ATR% tier table.

**Interpretation:**
- `warn` (~75% of signal): Stock is elevated — watch for momentum slowing
- `signal` (P75): Historically top-quartile peak — start peeling (scale out)
- P90: Historically extreme — hard exit candidate

## ATR% Tier Fallback (when not calibrated)

Used by `position_monitor.py` when ticker has no calibration data:

| ATR% Range | Warn | Signal |
|-----------|------|--------|
| ≤ 4% (low) | 3x | 4x |
| ≤ 7% (mid) | 5x | 6x |
| ≤ 10% (high) | 6.5x | 8x |
| > 10% (extreme) | 8.5x | 10x |

## How to Reason About a Position

1. Get current ATR% multiple from today's close, SMA50, ATR14
2. Compare to `data/peel_calibration.json[ticker].warn` and `.signal`
3. If multiple > signal → strong peel signal (scale out half or more)
4. If multiple > warn but < signal → elevated, watch closely
5. If ticker not in calibration → use ATR% tier fallback above

## Calibration Data Shape (peel_calibration.json)

```json
{
  "AAOI": {
    "signal": 14.2,
    "warn": 10.7,
    "p50": 11.3,
    "p75": 14.2,
    "p90": 18.9,
    "max_seen": 21.6,
    "runs": 4,
    "atr_pct_avg": 8.4,
    "calibrated": true,
    "updated": "2026-04-11"
  }
}
```

When `calibrated: false`, the `reason` field is either `insufficient_data` (< 64 bars) or `insufficient_runs` (< 3 qualifying runs).
