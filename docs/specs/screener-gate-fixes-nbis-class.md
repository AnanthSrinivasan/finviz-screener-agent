# Screener Gate Fixes — NBIS-class Coverage

**Status:** Draft — awaiting approval
**Predecessor:** [retro-coverage-nbis-class.md](retro-coverage-nbis-class.md) (audit found 11/16 actionable winners caught; 3 fixes push to 15-16/16 = 94%)

## Why

The retro audit named 4 historical winners we missed (DOCN Apr 13, ANET Apr 22, APP Sep 9 2024, SMCI Jan 17 2024) and proved each one fails a *specific* gate. This spec implements the 3 targeted fixes so the daily screener catches DOCN / 21 EMA pullback / Stage-1→2 reclaim setups going forward.

User-facing change: tomorrow's daily Slack starts catching DOCN-class deeper-base reclaims and adds a new **🎯 21 EMA Pullback** block. New hits auto-enter the watchlist at `priority=focus` (same path as `🛡️ RS Leader`).

## Fixes

### Fix 1 — HTF-BR ATR cap 7 → 8.5

**File:** [agents/screener/](agents/screener/) (the HTF Base Reclaim block builder)
**Change:** `atr_pct <= 7` → `atr_pct <= 8.5`
**Why:** DOCN Apr 13 2026 — dist -15.4%, ATR 8.0, Stage 2 perfect, MA stack rising. Real HTF reclaim but ATR 8.0 disqualified it. Bumping to 8.5 catches DOCN-class deeper-base reclaims without letting in junk (anything ATR > 8.5 is too volatile for trend-follow sizing anyway — flagged separately as `⚠ High-vol — size 50%`).
**Risk:** Slightly more candidates. Mitigated by existing peel-warn gate (already filters extended names) and the `⚠ High-vol` badge on cards with ATR > 7.

### Fix 2 — New 🎯 21 EMA Pullback block (daily)

**Files:**
- [agents/screener/](agents/screener/) — new block builder (analogous to `_build_rs_leader_block`)
- [agents/screener/finviz_agent.py](agents/screener/finviz_agent.py) — wire into Slack message order (place between Ready-to-Enter and Hidden Growth)
- watchlist auto-add path with `source=ema21_pb_auto`

**Predicate:**
- Stage 2 perfect *(use production definition — `price > EMA8 > EMA21 > SMA50 > SMA200`)*
- ATR% ≤ 6
- `|price − EMA21| / price ≤ 2%` (price right at EMA21)
- Prior 20d return ≥ 15% (there must have been a run to pull back from)
- **RVol filter:** `RVol < 1.0` (quiet PB) **OR** `1.0 ≤ RVol ≤ 2.5 AND ret20 ≥ 15%` (active bounce on real prior strength)
- Not held, peel-warn safe
- Top 5 by Quality Score

**Why both RVol branches:** ANET Apr 22 (RVol 2.15, dist -0.4%, EMA21 dist 3.4%) and APP Sep 9 2024 (RVol 1.76) were textbook PB-bounce days with high volume — strict `RVol<1.0` killed them. The expanded filter accepts both quiet-drift and active-bounce PBs.

**Slack block header:** `🎯 21 EMA Pullback — top 5 by Quality Score`

### Fix 3 — RTE / RS-Leader: pullback-friendly Stage 2 when `dist ≤ -10%`

**File:** [agents/screener/](agents/screener/) (RTE block + RS Leader block predicates)
**Change:** when `dist_from_52w_high ≤ -10%`, accept `stage2_pullback` (`EMA21 > SMA50 > SMA200 AND price > SMA50`) instead of requiring full `price > EMA8 > EMA21 > SMA50 > SMA200`.
**Why:** SMCI Jan 17 2024 (dist -11%, ATR 5.5, RVol 1.09) was a real RTE setup but fails because on reclaim/PB day EMA8 is bunched with price. Above -10% from high we keep the strict ladder (no need to relax — those are already extended-enough setups where stacking matters).
**Risk:** None for RS-Leader (already capped at RVol ≤ 1.5, ATR ≤ 8, ≥80 Q score). For RTE we keep dist ≤ -1% lower bound + RVol ≤ 1.2 — the relaxation only widens which day-stacking patterns qualify within the dist band.

## Out of scope

- Bucket A misses (deep-drawdown Stage 1→2 transitions: Z, FSLY, ALAB, MU Mar, NVDA Mar) — these are bottom-fishing setups we philosophically don't take. No change.
- Quality Score / Hidden Growth / Power Play / Base Building — unchanged.
- Position monitor logic — unchanged.

## Tests

New unit tests in `tests/`:
- `test_screener_ema21_pb.py` — predicate hits ANET-class active-bounce + quiet PB; rejects extended / non-Stage-2 / dist-from-EMA21 too wide
- `test_screener_htf_br_atr_cap.py` — DOCN-class fixture (ATR 8.0) passes; ATR 8.6 fails
- `test_screener_rte_pullback_stage.py` — SMCI-class fixture (-11% dist, EMA8 bunched) passes via stage2_pullback; setup at -8% dist requires perfect ladder

Plus full suite: `python -m unittest discover -s tests -t .` must remain green.

## Rollout

1. Implement Fix 1, 2, 3 + tests
2. Run full test suite — green
3. Commit + push
4. Trigger manual daily-finviz.yml run, verify Slack output includes new 🎯 21 EMA Pullback block, verify DOCN-class candidates appear (if any qualifying today)
5. Update CLAUDE.md (Daily Screener Signals section) with the new block and the 2 gate widenings
6. Update MEMORY.md if the gate change captures a recurring pattern worth recording

## Success criteria

- All new tests green, full suite green
- Daily-finviz workflow run succeeds with new block rendered
- At least one of the 4 retro misses (or a same-class current candidate) appears in the next 5 trading days
