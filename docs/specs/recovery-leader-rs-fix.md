# Recovery Leader gate + RS Rating override — Spec (YOLO-executable)

**Status:** APPROVED 2026-06-08 — execute next window.
**Scope:** Fix #1 (Recovery Leader Q gate) + Fix #2 (RS Rating Perf-Quarter override). **#3 (CNC snapshot-fetch) is explicitly OUT — user doesn't care.**

## Problem (verified, not assumed)

OSCR ran +89% in a quarter into the managed-care rotation and the system never surfaced it. Verified from `data/finviz_screeners_2026-05-18.csv`:
- Stage 0 (pre-golden-cross: SMA50% 58.6 still below SMA200% 54.8 — V-recovered too fast), Perf Quarter +88.95, RVol 1.87, ATR 4.86, **Quality Score 45**, **RS Rating 61**.

The block built for exactly this pattern — 🐉 **Recovery Leader** (`_is_recovery_leader`, finviz_agent.py:2493) — rejected it on TWO self-defeating gates:
- **Q ≥ 65** — but a pre-Stage-2 name structurally can't earn the Stage-2 Q bonus (+25–35) or VCP (+15), so its Q ceiling is ~55–60. The gate is unreachable for the class the block exists to catch.
- **RS Rating ≥ 65** — OSCR scored 61. `_compute_rs_ratings` (finviz_agent.py:2828) weights `p3*0.4 + p6*0.3 + p12*0.3`; OSCR's 1-year (+44%) includes its old base and drags a +89%-quarter name under the cutoff. Same defect previously seen with QBTS.

Both fixes are needed together for OSCR to flag (RS fix lifts 61→~90 past the ≥65 gate; Q fix lets Q45 through).

## Fix #1 — Recovery Leader Q gate: 65 → 40

`agents/screener/finviz_agent.py`, `_is_recovery_leader`, lines ~2549–2551:
```python
    qs = row.get("Quality Score")
    if qs is None or pd.isna(qs) or float(qs) < 65:
        return False
```
→ change `< 65` to `< 40`. Rationale: 40 still filters genuine junk (weak RVol/EPS/no momentum) but admits pre-cross recoveries whose Q is structurally capped. Update the docstring line 2514 `- Q ≥ 65` → `- Q ≥ 40 (pre-Stage-2 names can't earn the Stage-2 Q bonus; 65 was unreachable)`. The other gates (Stage 0/1, sma50≥15, sma200≥15, sma20>0, PerfQ≥50, RS≥65, ATR≤9, RVol≥1.0, peel-safe, sector exclude) are unchanged and carry the quality bar.

## Fix #2 — RS Rating Perf-Quarter override

`agents/screener/finviz_agent.py`, `_compute_rs_ratings` (line 2828). Today it builds one composite percentile. Add a **quarter-only percentile** and floor the final RS at it **only when the quarter rank is top-quintile**, so explosive 90-day movers aren't dragged by a stale 1-year base, while mid names are untouched.

Replace the single-composite ranking with:
```python
    # composite (existing) + quarter-only series
    comp = []   # (ticker, composite)
    q3   = []   # (ticker, perf_quarter)
    for _, row in df.iterrows():
        ticker = row.get("Ticker", "")
        if not ticker:
            continue
        p3  = _safe(row.get("Perf Quarter"))
        p6  = _safe(row.get("Perf Half Y"))
        p12 = _safe(row.get("Perf Year"))
        comp.append((ticker, p3 * 0.4 + p6 * 0.3 + p12 * 0.3))
        q3.append((ticker, p3))
    if not comp:
        return {}

    def _pctile(records):
        n = len(records)
        records = sorted(records, key=lambda x: x[1])
        return {t: (int(round(rank / (n - 1) * 99)) if n > 1 else 99)
                for rank, (t, _) in enumerate(records)}

    comp_p = _pctile(comp)
    q_p    = _pctile(q3)
    ratings = {}
    for t in comp_p:
        qr = q_p[t]
        # top-quintile quarter movers floor at their quarter rank (OSCR/QBTS fix)
        ratings[t] = max(comp_p[t], qr) if qr >= 80 else comp_p[t]
    return ratings
```
Update the docstring to document the override. Net effect on OSCR: q_p≈90+ (top-quintile +89% quarter), so RS 61→~90.

## Tests — `tests/test_finviz_agent.py` (or test_integration.py)

1. **Recovery Leader admits OSCR-class:** build a row mimicking OSCR 5/18 (Stage 0, sma20 27.9, sma50 58.6, sma200 54.8, PerfQ 88.95, ATR 4.86, RVol 1.87, Q 45, RS 90, sector Healthcare, peel-safe). Assert `_is_recovery_leader(row, set(), set()) is True`. Assert a near-identical row with Q=35 returns False (floor still bites).
2. **Recovery Leader still needs RS:** same OSCR row but RS 61 → False (RS gate unchanged at 65; relies on Fix #2 to lift real names).
3. **RS override floors a hot-quarter name:** craft a universe where one ticker has PerfQ huge / PerfYear small and others uniform; assert its rating == its quarter percentile (≥80) and exceeds its composite percentile. Assert a uniformly-mid ticker's rating is unchanged (quarter rank <80 path).
4. **RS override no-ops when quarter not top-quintile:** a mid PerfQ name keeps composite rating.

## Verify (do all)

1. `python -m unittest discover -s tests -t .` — full suite green (was 969 + cockpit; new tests added).
2. Re-run the daily screener against the 5/18 snapshot if feasible, OR run the live workflow `gh workflow run daily-finviz.yml` + `gh run watch`, and confirm in logs/output that an OSCR-class Stage-0 recovery name now appears in the 🐉 Recovery Leader block and that RS Ratings for known monster-quarter movers rose.
3. Spot-check `data/finviz_screeners_*.csv` next run: a +80%-quarter name should show RS ≥ ~80.

## Docs (mandatory)

- CLAUDE.md: Recovery Leader entry — `Q ≥ 65` → `Q ≥ 40`; RS Rating (Phase 2) — add the Perf-Quarter top-quintile override note.
- SYSTEM_DOCS.md: same two edits in the Recovery Leader + RS Rating sections.
- Memory: update [[project_recovery_leader_block]] and [[project_episodic_pivot_research]] (RS-Rating-broken-for-recovery-names note now has a fix).

## Out of scope / later
Constituent-drill + sector-heat detector ([[project_sector_heat_detector]]) — separate spec. IHF/pharma ETF universe gap — separate.
