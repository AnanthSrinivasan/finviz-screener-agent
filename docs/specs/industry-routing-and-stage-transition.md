# Industry-based ETF routing + Stage Transition screener block

**Date:** 2026-05-19
**Status:** Draft — awaiting review
**Triggered by:** Missed software rotation (May 2026). Semis (SMH) topping while software (IGV) accumulating; system mapped both to XLK and the rotation was invisible. Separately, Stage 2 gate rejected early-rotation reclaims whose 200 SMA was still overhead.

---

## Problem

### Bug 1 — "Technology" sector lumps semis + software + hardware

`agents/utils/sector_lookup.py:19-33` — `FINVIZ_SECTOR_TO_ETF` maps the entire Finviz `Sector="Technology"` bucket to **XLK**. A HUBS/SNOW/TEAM (software) and an AMAT/LRCX (semis) both resolve to XLK. When semis top and software rotates in:
- Held-ticker → ETF resolution can't see the rotation (both still ping XLK).
- Sector RS signal averages two opposing trends → mush.
- Position monitor's eventual "SECTOR ROTATING OUT" alert (per CLAUDE.md §Pending integrations) will mis-fire on software because semis are dragging XLK down.

Finviz already exposes `Industry` per ticker (already scraped and persisted — see `agents/screener/finviz_agent.py:114`, `124`, `156`). We're throwing the signal away.

### Bug 2 — Stage 2 gate kills early-rotation reclaims

`compute_stage()` in `agents/screener/finviz_agent.py:297-326` requires:
- `sma200 > sma50` (stacking) AND
- `sma50 > -10` (price near or above 50 SMA)

A software name 6 months into a Stage 1 base, just reclaiming the 50 SMA with the 200 SMA still overhead, returns Stage 1 or transitional → drops out of every actionable block (Ready-to-Enter, Fresh Breakout, RS Leader, HTF Base Reclaim — all require Stage 2 perfect). That's the **Stage 1→2 transition (Minervini "stage 2A")** — exactly the rotation entry we want.

User's signature for this setup: `8 EMA > 21 EMA > 50 SMA`, price above 50 SMA, 200 SMA flexible (rising, or price within reach).

---

## Proposed change

### Part A — Industry-based ETF routing

**File:** `agents/utils/sector_lookup.py`

Add an `INDUSTRY_TO_ETF` dict consulted **before** the sector fallback. Routes by substring match on Finviz Industry strings.

```python
INDUSTRY_TO_ETF = {
    # Technology subsectors — the bug fix
    "Semiconductor":           "SMH",   # "Semiconductors", "Semiconductor Equipment & Materials"
    "Software - Application":  "IGV",
    "Software - Infrastructure": "IGV",
    "Internet Content":        "FDN",   # "Internet Content & Information"
    "Information Technology Services": "XLK",
    "Computer Hardware":       "XLK",
    "Electronic Components":   "XLK",
    # Financials subsectors (bonus — banks vs insurance vs capital markets diverge often)
    "Banks":                   "KBE",   # "Banks - Regional", "Banks - Diversified"
    "Capital Markets":         "KCE",
    "Insurance":               "KIE",
    # Biotech vs broad healthcare
    "Biotechnology":           "XBI",
    "Drug Manufacturers":      "XBI",   # "Drug Manufacturers - Specialty & Generic" included
    # Homebuilders vs broad consumer cyclical
    "Residential Construction": "XHB",
    "Building Products":       "XHB",
}
```

Update `lookup()` signature to accept industry:

```python
def lookup(ticker: str, finviz_sector: Optional[str] = None,
           finviz_industry: Optional[str] = None) -> Optional[str]:
    # 1. Explicit ticker map (unchanged) — highest priority
    # 2. NEW: Industry substring match
    # 3. Sector fallback (unchanged)
```

Industry match uses **substring `in` check**, not equality, so "Semiconductors" and "Semiconductor Equipment & Materials" both map. First matching key wins (dict insertion order = priority — list specific industries before generic ones).

**Callers to update:**
- `agents/screener/finviz_agent.py` — wherever `lookup()` is called (one location for daily-quality writes)
- `agents/screener/finviz_weekly_agent.py` — same
- `agents/trading/alpaca_monitor.py` — pass industry from position state if available
- `agents/utils/etf_rotation_summary.py` — read-only, no change

**`data/ticker_sector_map.json`** — most entries become redundant once industry routing exists (NVDA/AMAT/LRCX/MU all match "Semiconductor" automatically). Keep file as override-only for edge cases (e.g. AAOI's industry is "Communication Equipment" but we want it on SMH because of its actual revenue mix). Document in file header.

### Part B — Stage Transition screener block

**File:** `agents/screener/finviz_agent.py`

New helper `_is_stage_transition(row, open_positions_tickers, sector_rotation_snapshot)` placed adjacent to `_is_ready_to_enter` (`finviz_agent.py:1775`). Predicate:

```python
sma20  > sma50               # 20 SMA above 50 SMA (proxy for 21 EMA > 50 SMA on Finviz)
sma50  > 0                   # price above 50 SMA
sma200 > -5  OR  sma200 > 0  # 200 SMA overhead by ≤5%, OR price above it
atr_pct <= 7
quality_score >= 70
rvol >= 1.0
not in open_positions_tickers
not in {Ready-to-Enter, Fresh Breakout, HTF Base Reclaim, RS Leader, Power Play, Hidden Growth, 21 EMA PB}
peel-safe (SMA50% / ATR% <= peel_warn)  # same calibration source as other blocks
sector_etf_rank_delta_5d <= -5  # parent ETF's RS rank improving over 5d (rotation confirm)
```

**Why the sector-rank gate matters:** without it, this block becomes a junk-reclaim catcher (any dead stock reclaiming its 50 SMA). With it, we only fire when the *sector itself* is rotating in. A software reclaim while IGV's rank_delta_5d = -8 = real rotation. A retail reclaim while XRT is sideways = noise.

**Data dependency:** the sector_rotation snapshot (`data/sector_rotation_YYYY-MM-DD.json`) must be loadable at screener runtime. Sector rotation runs at 21:15 UTC, daily screener at 20:30 UTC — **screener runs BEFORE sector rotation**. Fix options:
1. Swap cron order: sector rotation at 20:15, screener at 20:30. Simplest.
2. Screener reads *yesterday's* snapshot. Acceptable (rank deltas are 5d windows; 1d staleness ≈ 20% of signal).
3. Screener computes sector RS inline. Heaviest.

**Recommend option 1.** One-line cron change in `.github/workflows/sector-rotation.yml`.

**Slack block:** `🌱 Stage Transition` — top 5 by Q.
```
*🌱 Stage Transition (Early Stage 2 — sector rotating in)*
• TICKER — Q82 · ATR 5% · sma20+3% sma50+1% sma200-2% · sector IGV (+8 RS)
  /stock-research TICKER
```

**Watchlist:** auto-add at `priority=focus`, `source=stage_transition_auto` (sixth entry path).

**HTML gallery:** new `<details open>` section under the Top Picks gallery in `data/finviz_chart_grid_YYYY-MM-DD.html`.

---

## Open questions for user

1. **Industry override behavior** — if `ticker_sector_map.json` has TICKER → XLK, but industry says "Semiconductor" → SMH, which wins? Spec currently says ticker map wins (priority 1). Confirm or flip.
2. **Stage Transition Q threshold** — proposed 70. Ready-to-Enter is 80, Fresh Breakout is 70, RS Leader is 75. 70 keeps the bar lower because the setup is earlier in the cycle. OK?
3. **Cron reorder** — move `sector-rotation.yml` from 21:15 → 20:15 so screener can read today's snapshot? Or accept yesterday's snapshot?
4. **Watchlist priority** — `focus` (same as RS Leader / HTF BR / 21 EMA PB) or `watching` (same as Hidden Growth)? Stage Transition is earlier than focus-tier setups, so arguably `watching`. Confirm.
5. **Promote to Slack-actionable or research-only?** — proposing Slack-actionable (in main message). Alternative: HTML-gallery-only first run, validate signal quality for 2 weeks, then promote.

---

## Files touched

| File | Change |
|---|---|
| `agents/utils/sector_lookup.py` | Add `INDUSTRY_TO_ETF` + `finviz_industry` param to `lookup()` |
| `agents/screener/finviz_agent.py` | Pass industry to `lookup()`; add `_is_stage_transition()` + Slack/HTML wiring |
| `agents/screener/finviz_weekly_agent.py` | Pass industry to `lookup()` |
| `agents/trading/alpaca_monitor.py` | Pass industry to `lookup()` where available |
| `data/ticker_sector_map.json` | Trim entries now redundant; keep as override-only |
| `.github/workflows/sector-rotation.yml` | (Conditional) cron 21:15 → 20:15 |
| `tests/test_sector_lookup.py` | Industry routing cases (semis, software, banks, biotech) + ticker-map-wins precedence |
| `tests/test_finviz_agent.py` | `_is_stage_transition` predicate cases (pass / Stage 2 already / dead sector / extended) |

---

## Tasks (after spec approval)

1. Add `INDUSTRY_TO_ETF` + extend `lookup()` signature.
2. Update all 4 callers to pass industry.
3. Trim `ticker_sector_map.json` to override-only.
4. Write `tests/test_sector_lookup.py` industry cases.
5. (If user says yes to cron reorder) move sector-rotation cron to 20:15.
6. Add `_load_sector_rotation_snapshot()` helper in `finviz_agent.py`.
7. Add `_is_stage_transition()` predicate.
8. Wire Slack block + HTML gallery section + watchlist auto-add.
9. Write `tests/test_finviz_agent.py` predicate cases.
10. Run full test suite locally.
11. Manually trigger `daily-finviz.yml` workflow and verify logs.
12. Commit + push + verify next scheduled run.
