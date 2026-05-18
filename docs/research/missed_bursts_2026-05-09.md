# Missed +30% bursts — retro 2026-03-15 → 2026-05-09

**Trigger:** user complaint that RKLB (+34% earnings gap May 8) wasn't surfaced.
**Script:** `utils/retro_missed_bursts.py` · **Output:** `data/retro_missed_bursts.json`

## Method
1. Aggregate all unique tickers from `data/finviz_screeners_2026-0[3-5]*.csv` (856 unique, 36 days).
2. For each, fetch Alpaca daily bars from first-appearance date → today (`feed=iex`, `adjustment=split`).
3. Compute max close after first appearance + `gain_pct`.
4. Classify whether current screener rules would have surfaced the row (RTE / Fresh Breakout / Base Building / Hidden Growth predicates).

## Findings

**Headline:** of 693 clean rows (post-CSV-schema-update), 98 ran +30%. We surfaced 13. Miss rate 87%.

**But the 87% breaks down to mostly out-of-mandate names:**
- 76/85 misses had Q < 70 (penny pumps, low-quality runners) — by design
- 61/85 not Stage 2 perfect — by design
- 50/85 dist < -25% (deep-base reanimations, e.g. WOLF, OGN, AKAN)
- 37/85 ATR > 8 (already-volatile breakouts) — by design (vol filter)
- 12/85 dist > 0 (already extended past 52w high)

**Genuine in-mandate leakage = 4 names** (Stage-2 perfect, Q≥70, ATR≤8, dist in -25 to 0):

| Ticker | Date | Q | dist | RVol | ATR | gain | Why missed |
|---|---|---|---|---|---|---|---|
| MTSI | 2026-04-06 | 94 | -10.02% | 0.57 | 5.8 | +54% | RTE dist gate `≤-10` strict, missed by 0.02% |
| RMBS | 2026-04-14 | 82 | -10.33% | 0.85 | 5.3 | +30% | RTE dist gate strict; FB RVol<1.2 |
| TWLO | 2026-04-21 | 73 | -0.41% | 0.90 | 4.5 | +39% | Q<80 for RTE; FB RVol<1.2 |
| IHRT | 2026-04-24 | 74 | -0.37% | 1.82 | 5.7 | +38% | Q<80 for RTE; FB needs SMA stack recheck |

**RKLB Apr 16:** retro showed `bb: true` (Q=78, dist -16.7%, ATR 6.8, Stage 2 perfect — qualified for Base Building). Base Building feature was merged after Apr 16, so it didn't render that day. Going forward, this class is caught.

## Patterns worth capturing

1. **Distance from 52w high is the wrong yardstick for HTF base reclaims.** RKLB at -16.7% from ATH was -11% from relevant Jan/Feb swing high. Need a swing-high-pivot detector that pulls 90d daily bars and computes `dist_from_30d_swing_high` as alternate path.
2. **Strict dist gates leak narrowly.** `≤-10` strict killed two clean Q≥80 names by 0.02-0.33pp. Soften to `≤-12` or use closed range with float tolerance.
3. **Low-RVol pre-break setups exist.** TWLO/RMBS broke after a sub-1.0 RVol day. RVol gate at 1.2 in FB excludes the *quietest* before-the-move setups. Trade-off: lowering it adds noise.
4. **Names already at 52w high (dist > 0) are uncatchable by a "buy the pullback" screen.** INTC, FLEX class — accept that mandate.

## Action items (proposed, not yet shipped)

1. RTE `dist <= -10` → `<= -12`
2. FB `RVol >= 1.2` → `RVol >= 1.0` when `Q≥80 AND ATR≤6` (quality + tightness exception)
3. Swing-high pivot detector — new module `agents/utils/swing_pivot.py` + `🌀 HTF Base Reclaim` Slack block
4. Bump BB Slack cap 5 → 10; HTML gallery uncapped

Decision pending from user on which to ship.

## Won't ship (out of mandate)

- Q<70 catchers — quality bar is intentional
- ATR>8 catchers — vol filter is intentional
- dist>0 catchers — we don't buy extended

## How to repeat this analysis

```bash
python3 utils/retro_missed_bursts.py
```

Re-run periodically (monthly?) to validate gates aren't drifting from intent. Compare `surfaced` count vs missed-but-fixable count.
