# Retro Coverage Audit — NBIS-class Setups

**Status:** Approved 2026-05-16 — execute
**Goal:** Validate that the screener's gates would have flagged historical NBIS-class winners on their key entry dates. Produce a coverage matrix that surfaces exactly which gates leak.

## Why

NBIS Apr 9 2026 reclaim → May run from $140 → $230 was missed. User wants quantified confidence the system catches this archetype next time, not just one-off ticker rationalizations. A coverage matrix across 20 historical winners (recent + prior bull markets) gives a measurable hit rate per block and exposes leakage by gate.

## Basket — 20 tickers

Sourced in three layers:

1. **Local winners (`data/chart_patterns/winners/`)** — current closed positions: `AAOI`, `MU`, `NVDA`, `Z`.
2. **Recent 2025–2026 NBIS-class** (user-known + screener history): `NBIS`, `RKLB`, `DOCN`, `INDV`, `ARWR`, `ANET`, `FSLY`, `RMBS`, `MTSI`, `ALAB`.
3. **Prior bull-market classics** (2020 post-COVID + 2024 AI rip): `SMCI` (2024), `VRT` (2024), `APP` (2024), `CRWD` (2020), `DDOG` (2020), `ENPH` (2020).

Total: **20 tickers**.

If any ticker has insufficient Alpaca history (delisted, pre-IPO at the time, etc.) it's logged as `n/a` in the matrix and replaced with a documented fallback.

## Key dates per ticker — 2–3 each

For each ticker, label up to 3 entry candidates against its actual chart:

- **D1 — Reclaim:** first close back above a prior swing high after a deeper 52w drawdown
- **D2 — First tight flag / breakout:** post-reclaim consolidation breakout
- **D3 — 21 EMA pullback:** clean PB to 21 EMA after the initial run, low ATR contraction

Dates picked manually from chart inspection by the agent during execution and stored alongside the matrix. Total: ~50 ticker-date rows.

## Per-row evaluation — reconstructed technical gates

For each `(ticker, date)`, reconstruct daily-bar technicals from **Alpaca** (we have full history) and evaluate the **technical portion** of each block. Output `pass`/`fail` per block + the failing gate when `fail`.

### Reconstructable inputs (from Alpaca daily bars)

- Stage 2 perfect (price > EMA8 > EMA21 > SMA50 > SMA200)
- ATR% (ATR14 / close × 100)
- ATR-multiple from 50MA: `(close − sma50) × close / (sma50 × atr14)` — matches `utils/calibrate_peel.py`
- Distance from 52w high (252-bar)
- MA-stack slope (50MA + 200MA both rising over last 10 bars)
- RVol (today vs 20d avg vol)
- Distance from EMA21, distance from EMA8
- Swing-pivot reclaim (90d window, exclude last 5)
- 20d range tightness `(max(high) − min(low)) / close × 100`

### Not reconstructable — marked `n/a` and excluded from scoring

- EPS Y/Y TTM, EPS Q/Q, Inst Trans %
- Multi-screener persistence count
- Quality Score components depending on the above
- RS Rating (needs full universe Finviz snapshot for that date)
- VCP confidence (Finviz-specific)

These show as `n/a` in the matrix; coverage scoring uses only the technical portion.

### Blocks evaluated (technical-only)

| Block | Technical filter |
|---|---|
| **RTE-tech** | Stage 2 perfect · ATR ≤7 · dist -1% to -12% · RVol ≤1.2 |
| **FB-tech** | Stage 2 · ATR ≤8 · dist 0% to -12% · SMA20%>0 · SMA50% (0, 25%] · RVol ≥1.2 OR tight-quality |
| **HTF-BR-tech** | Stage 2 perfect · ATR ≤7 · dist < -12% · MA stack rising · RVol ≥1.0 · swing-pivot reclaim ≥ -10% |
| **RS-Leader-tech** | Stage 2 perfect · ATR ≤8 · dist [-10%, +2%] · MA stack rising · RVol ≤1.5 |
| **BB-tech** | Stage 2 · dist -12% to -25% · ATR ≤7 |
| **21EMA-PB** *(proposed new lane)* | Stage 2 perfect · ATR ≤6 · `|price − EMA21|/price ≤ 2%` · prior 20d ret ≥ 15% · RVol declining (today < 20d avg) |

The **21EMA-PB** block does not exist in production. It's evaluated speculatively. If it catches a material share of misses, propose adding it as a follow-up implementation task — out of scope for this audit.

## Output

Files written to `docs/research/`:

- `retro_coverage_nbis_class.html` — coverage matrix table (ticker × date × blocks), per-ticker summary, per-block hit-rate stats, list of misses with reason
- `retro_coverage_nbis_class.json` — same data, machine-readable
- `retro_coverage_nbis_class_dates.json` — picked entry dates per ticker (so we can audit / re-run)

## Success criteria

- Coverage number: "X of 20 tickers (Y of 50 ticker-dates) flagged by ≥1 block"
- **X ≥ 16 → system is sound** (80%+ archetype coverage)
- **X < 16 → matrix surfaces the leaky gate** (e.g. "dist gate disqualified 5 of 8 misses" → propose widening dist cap)

## Out of scope

- Live re-screening with full Finviz fundamentals (we don't store historical snapshots)
- Implementing the 21EMA-PB lane (separate follow-up if audit justifies it)
- Multi-day re-screening per ticker (we evaluate only the user-labeled D1/D2/D3)

## Deliverable

One script (`utils/retro_coverage_nbis.py`), two output files, one summary in chat. Run via `python utils/retro_coverage_nbis.py`. No GH Actions workflow.
