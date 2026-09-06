"""
EU screener EXPERIMENT — technical criteria only.

Deliberately standalone: imports nothing from agents/ or utils/, writes nothing
into data/. Delete this folder and the production system is untouched.

Why decoupled rather than reusing agents.screener.finviz_agent: this is an
experiment on a market the production universe does not cover, and coupling it
would let an experimental change reach the live screener. The cost is that the
thresholds below are a COPY of the production ones and can drift — if the
experiment graduates, the fix is to delete these and import the real predicates.

Thresholds copied from CLAUDE.md (Ready-to-Enter gate) on 2026-09-06.
"""

ATR_TIER_WARN = [(4.0, 3.0), (7.0, 5.0), (10.0, 6.5), (999.0, 8.5)]

DIST_HIGH_MIN, DIST_HIGH_MAX = -12.0, -1.0
ATR_MAX = 7.0
RVOL_MAX = 1.2


def pct_from(price: float, ma: float) -> float:
    """Percent distance of price from a moving average. +ve = price above."""
    if not ma:
        return 0.0
    return (price - ma) / ma * 100.0


def atr_multiple(sma50_pct: float, atr_pct: float) -> float:
    """ATR% Multiple from the 50 MA — the TradingView 'ATR% Multiple' reading.

    Algebraically identical to the production formula
    (close - sma50) * close / (sma50 * atr14), since atr_pct = atr14/close.
    """
    if not atr_pct:
        return 0.0
    return sma50_pct / atr_pct


def tier_warn(atr_pct: float) -> float:
    """Peel-warn threshold for an ATR% tier. Extension past this = not safe."""
    for ceiling, warn in ATR_TIER_WARN:
        if atr_pct <= ceiling:
            return warn
    return ATR_TIER_WARN[-1][1]


def stage(sma20_pct: float, sma50_pct: float, sma200_pct: float) -> dict:
    """Weinstein stage from MA distances, mirroring production compute_stage.

    sma200_pct > sma50_pct means the 50 MA sits ABOVE the 200 MA (price is
    farther above the 200 than the 50) — correct uptrend stacking.
    """
    stacked = sma200_pct > sma50_pct
    above_50 = sma50_pct > -10.0
    if stacked and above_50:
        return {"stage": 2, "perfect": sma20_pct > 0, "label": "Stage 2"}
    if not stacked and sma50_pct < 0 and sma200_pct < 0:
        return {"stage": 4, "perfect": False, "label": "Stage 4 — markdown"}
    if stacked and not above_50:
        return {"stage": 3, "perfect": False, "label": "Stage 3 — topping"}
    return {"stage": 1, "perfect": False, "label": "Stage 1 — base"}


def evaluate(row: dict) -> dict:
    """Apply the technical gate to one normalized row.

    Required keys: ticker, close, sma20, sma50, sma200, atr_pct, high_52w, rvol
    Returns the row plus computed fields and a list of failed gates.
    """
    c = float(row["close"])
    s20 = pct_from(c, float(row["sma20"]))
    s50 = pct_from(c, float(row["sma50"]))
    s200 = pct_from(c, float(row["sma200"]))
    atr = float(row["atr_pct"])
    dist = pct_from(c, float(row["high_52w"]))
    rvol = float(row.get("rvol") or 0)
    st = stage(s20, s50, s200)
    mult = atr_multiple(s50, atr)
    warn = tier_warn(atr)

    fails = []
    if st["stage"] != 2:
        fails.append(f"stage {st['stage']}")
    elif not st["perfect"] and dist > -10.0:
        fails.append("not stage-2 perfect")
    if not (DIST_HIGH_MIN <= dist <= DIST_HIGH_MAX):
        fails.append(f"dist {dist:.1f}%")
    if atr > ATR_MAX:
        fails.append(f"ATR {atr:.1f}%")
    if rvol > RVOL_MAX:
        fails.append(f"RVol {rvol:.2f}")
    if mult > warn:
        fails.append(f"extended {mult:.2f}x > {warn:.1f}x")

    return {**row, "sma20_pct": round(s20, 2), "sma50_pct": round(s50, 2),
            "sma200_pct": round(s200, 2), "dist_high_pct": round(dist, 2),
            "atr_multiple": round(mult, 2), "peel_warn": warn,
            "stage": st, "passes": not fails, "fails": fails}
