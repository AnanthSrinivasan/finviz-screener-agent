# Rotation Catalyst Block + ETF-Aware Sizing + Plain-English Rotation Labels

**Status:** Draft 2026-05-28. Awaiting review.
**Reference cases:** UMAC 05-27 (drone, UFO #1), ONDS 05-27 (drone), SNOW today (cloud, IGV rotating), PATH (Stage 0 V-recovery).

## Problem

Three gaps surfaced 2026-05-28:

### Gap A — Rotation-aligned setups miss every actionable block by a hair

UFO (drone ETF) is rank **#1 of 28** with RS 96 and Δ5d -4. ARKK Δ5d -13. The sector signals are loud. But the underlying names UMAC and ONDS were rejected from every actionable block on 05-26 and 05-27, the two days they were setting up:

| | 05-27 dist52 | S20% | RVol | Rejected by |
|---|---|---|---|---|
| UMAC | -19.7 | +25.7 | 1.55 | HTF-BR swing-pivot gate (too steep), RTE/FB upper SMA20% (way above +3%) |
| ONDS | -17.5 | +12 | 1.15 | 21 EMA PB needs PerfMonth ≥+12 (was base-building, fails), HTF-BR swing-pivot |

By 05-28 close UMAC was +61% intraday and ONDS +22%. The system saw the persistence (UMAC 7 screener days, ONDS 3) but never bumped either name to the front of Slack while still actionable.

### Gap B — Single-stock rotation entries carry asymmetric blowup risk

A user wants to play drone rotation. UMAC ATR 7.8%, ONDS ATR 8.0%. One bad day = -10-15% drawdown on a single name. UFO ETF ATR 1.5% delivers the same rotation thesis with idio risk diversified across the bucket. The system never surfaces the ETF as an alternative — user has to know it.

### Gap C — Sector rotation dashboard uses jargon, not decisions

`data/etf_rotation.html` shows `rank_delta_5d -13`, `RS 96`, regime classifications like `mid-rotation`. Reading the table requires holding 3-4 numeric concepts at once. User wants one-glance decision labels: is this sector hot, rising, stable, fading, cold?

## Solution

Three independent changes, each shippable on its own.

### 1. 🌊 Rotation Catalyst block (new actionable lane)

**Predicate** in `agents/screener/predicates.py` (or wherever the other `_is_*` predicates live):

```python
def _is_rotation_catalyst(row, today_rotation: dict, ticker_to_etf) -> bool:
    """True when name is in a sector that's actively rotating IN, and the
    name itself is setting up (Stage 2, dist52 wider than HTF-BR, close > SMA20)."""
    etf = ticker_to_etf(row["Ticker"], row.get("Sector"), row.get("Industry"))
    if not etf: return False
    etf_info = today_rotation.get(etf)
    if not etf_info: return False

    # Sector gate — must be HOT (rank ≤5) or strongly RISING (rank ≤10 AND Δ5d ≤ -5)
    rank = etf_info.get("rank", 99)
    delta = etf_info.get("rank_delta_5d", 0)
    sector_hot = rank <= 5
    sector_rising = rank <= 10 and delta <= -5
    if not (sector_hot or sector_rising): return False

    # Name-level gate
    stage = compute_stage(row)
    if stage["stage"] != 2: return False           # Stage 2 (not strict 2P — drone names dip S20)
    if not (-35 <= row["DistFromHigh%"] <= 0): return False
    if row["Close"] <= row["SMA20$"]:              # reclaim confirmed
        return False
    if row["RelVolume"] < 1.0: return False
    if not peel_safe(row): return False             # ATR%/SMA50% under per-ticker peel_warn
    if held(row["Ticker"]): return False
    return True
```

**Wider bands than HTF-BR by design:**

- `dist52`: HTF-BR is -25 to -12 · Rotation Catalyst is **-35 to 0**
- Stage: HTF-BR requires perfect · RC accepts Stage 2 (S20 can dip)
- No swing-pivot gate (the drone names with sharp swings fail it)
- Sector-rotation tailwind is the proof-of-quality — earns the looser entry.

**Top 5 by Q in Slack block:**

```
🌊 Rotation Catalyst — sector tailwind setups
1. UMAC · UFO 🔥 #1 · Q83 · dist -19.7% · S20 +25% · RVol 1.6 · ATR 7.8%
   Single-stock: $19 · ⚠ ATR 7.8% — size 50%
   ETF play: UFO @ $32 · ATR 1.5% · whole rotation no idio risk
   /stock-research UMAC
2. ONDS · UFO 🔥 #1 · Q78 · dist -17.5% · S20 +12% · RVol 1.2 · ATR 8.0%
   …
```

**Auto-add to watchlist** at `priority=focus` with `source=rotation_catalyst_auto` (9th entry path).

**Gallery section** in `finviz_chart_grid_*.html`: collapsible `<details open>` with badges showing parent ETF + rotation status emoji.

### 2. ETF-companion line for every Rotation Catalyst fire

Every 🌊 line shows both the single-stock and the ETF play. Output structure:

```python
{
    "ticker": "UMAC",
    "single_stock": {"price": 30.24, "atr_pct": 7.8, "risk_note": "size 50% — ATR 7.8%"},
    "etf_play":     {"ticker": "UFO", "price": 32.10, "atr_pct": 1.5, "rotation_label": "🔥 #1"},
    "thesis": "drone rotation",
}
```

User picks: concentrated for the edge, ETF for the safety. Same thesis, two sizing options. No code changes elsewhere — purely additive output field.

### 3. Plain-English rotation labels — relabel dashboard + Slack

Replace `rank_delta_5d` / `RS` jargon in the ETF rotation dashboard (`data/etf_rotation.html`) and any Slack output that references rotation status with one column:

| Label | Condition |
|---|---|
| 🔥 HOT | rank ≤ 5 AND delta ≤ 0 (in the top, holding or improving) |
| ↗ RISING | rank 6-15 AND delta ≤ -3 (climbing) |
| → STABLE | abs(delta) < 3 (no meaningful move) |
| ↘ FADING | delta ≥ +5 AND rs_score < 60 (worsening) |
| ❄ COLD | rank ≥ 20 AND delta ≥ +3 (deep bottom and getting worse) |

Helper in `agents/utils/rotation_label.py` — pure function `rotation_label(rank, delta, rs) → str`. Used by `etf_rotation.html` generator and the Rotation Catalyst Slack block. Numeric rank still shown as `#X/28` next to the emoji so user knows position.

Dashboard column today: `Rank · Δrank · RS · ATR% · mult50 · …`
After: `Rank #X/28 · 🔥 HOT · ATR% · mult50 · …` (Δrank and RS hidden behind a "show metrics" toggle for nerds).

## Implementation order

1. **Spec approval** (this doc)
2. **Task 1 — rotation_label helper** + 6 unit tests covering each label band. Wire into `etf_rotation.html` and weekly `sector_setup` summary.
3. **Task 2 — Rotation Catalyst predicate** + 5 unit tests (UMAC 05-27 fires, ONDS 05-27 fires, UMAC 05-28 rejected as extended, PATH today fires under PerfQ-loosened Recovery Leader OR under Rotation Catalyst, name with no parent ETF returns False).
4. **Task 3 — Slack block + ETF companion line + gallery section.** Backtest: replay 05-26 / 05-27 and confirm UMAC + ONDS would have fired into Slack. SNOW today fires.
5. **Task 4 — Watchlist auto-add path** `source=rotation_catalyst_auto`. Watchlist test for lifecycle.
6. **Task 5 — Trigger daily-finviz workflow, verify Slack output + HTML output renders correctly.**

## Open questions for review

1. **Stage gate — Stage 2 only, or also Stage 0 / 1?**
   - SNOW today is Stage 0 (200MA overhead). PATH is Stage 1 (S200 -7%).
   - If Stage 2 only: SNOW + PATH miss this block, would need Recovery Leader's loosened-PerfQ-when-sector-HOT path instead.
   - If Stage 0/1 allowed when sector HOT: one block covers all three reference cases. Riskier — Stage 0 names with sector tailwind still have 200MA overhang.
   - Recommendation: **Stage 2 in Rotation Catalyst** (clean setup gate) · **add `sector_hot_perfq_floor = 15` to Recovery Leader** so PATH/SNOW class fires there with explicit V-recovery framing. Two blocks, two risk profiles.

2. **dist52 band — exactly -35 to 0?**
   - -35 lets UMAC 05-22 (-28) fire 5 days earlier than 05-27. Higher hit rate.
   - But -35 also surfaces some genuinely broken Stage 2s that happen to be in a hot ETF.
   - Recommendation: **-35 to 0** for first version, gate via Stage 2 + close > SMA20 + RVol ≥ 1.0 — the reclaim conditions filter the broken names.

3. **ETF-companion — show on every fire, or only when ATR ≥ 7%?**
   - Show always: redundant for low-vol names where the single-stock IS the play (no ETF needed).
   - Show only when ATR ≥ 7: cleaner output, ETF appears exactly when single-stock risk matters.
   - Recommendation: **show only when single-stock ATR ≥ 7%**.

4. **Rotation labels — full replace, or additive?**
   - Full replace (hide Δrank / RS by default): cleanest, what user asked for.
   - Additive (label + numbers side by side): safer, no regression for power-users who already read the numeric.
   - Recommendation: **full replace on dashboard hero row + Slack** · **keep numbers in expandable detail row** (click to expand).

## Rollout summary

| Change | Files touched | Risk |
|---|---|---|
| `rotation_label()` helper + tests | `agents/utils/rotation_label.py`, `tests/test_rotation_label.py` | none — pure function |
| Wire into `etf_rotation.html` | `agents/sector_rotation.py` (renderer) | low — replaces jargon column with emoji+rank |
| `_is_rotation_catalyst()` predicate + tests | `agents/screener/predicates.py` (or `finviz_agent.py`), `tests/test_rotation_catalyst.py` | low — net-new predicate |
| Slack block + ETF companion | `agents/screener/finviz_agent.py` (Slack composer) | medium — adds a new Slack section |
| Watchlist auto-add | `agents/screener/finviz_agent.py` lifecycle hook | low — mirrors existing 8 entry paths |
| `etf_rotation.html` relabel | `agents/sector_rotation.py` | low — UI |

Total: ~5 commits · ~12 new tests · ~300 LoC additive.
