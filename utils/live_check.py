#!/usr/bin/env python3
"""Live ticker check — fetch CURRENT Finviz data, never a cached snapshot.

Why this exists
---------------
Every JSON/CSV under data/ is a snapshot written by the last workflow run.
`etf_rotation.json` is yesterday's close. `finviz_screeners_*.csv` is
yesterday's close. Quoting those as if they were live is how XBI got called
"RS 51, rank 22, mid-pack" on 2026-08-19 while it was actually mid-breakout to
RS 86 / rank 6 — a stale read presented as a current fact, which cost a real
trade.

Rule: if the question is about what a ticker is doing NOW, run this. Never
read a data/ snapshot and call it current.

Every number here is fetched live at call time. Cached values are shown only
in the CONTEXT block, each one explicitly stamped with its age in days, so a
stale figure can never be mistaken for a fresh one.

    python -m utils.live_check XBI ARKG MRNA
"""
import datetime
import json
import os
import sys

DATA_DIR = os.environ.get("DATA_DIR", "data")

# ATR% tier -> (peel warn, peel signal). Mirrors PEEL_THRESHOLDS in
# position_monitor / the screener tier table.
TIERS = ((4.0, 3.0, 4.0), (7.0, 5.0, 6.0), (10.0, 6.5, 8.0), (999.0, 8.5, 10.0))


def tier_for(atr_pct: float) -> tuple:
    for max_atr, warn, signal in TIERS:
        if atr_pct <= max_atr:
            return warn, signal
    return 8.5, 10.0


def atr_multiple(price: float, sma50_pct: float, atr_pct: float) -> float:
    """TradingView 'ATR% Multiple from 50-MA'.

    (close - sma50) * close / (sma50 * atr14), expressed from the percentage
    fields Finviz gives us. Matches utils/calibrate_peel.py.
    """
    if not price or not atr_pct or sma50_pct is None:
        return 0.0
    return sma50_pct / atr_pct


def _age_days(stamp: str) -> str:
    try:
        d = datetime.date.fromisoformat(str(stamp)[:10])
        n = (datetime.date.today() - d).days
        return f"{stamp} ({n}d old)" + ("  <-- STALE" if n >= 1 else "  (today)")
    except Exception:
        return f"{stamp} (age unknown)"


def cached_context(ticker: str) -> list:
    """Cached values, each stamped with its age. Never presented as live."""
    out = []
    rot = os.path.join(DATA_DIR, "etf_rotation.json")
    if os.path.exists(rot):
        try:
            with open(rot) as f:
                d = json.load(f)
            for e in d.get("etfs", []):
                if e.get("ticker") == ticker:
                    out.append(
                        f"  etf_rotation.json  RS {e.get('rs_score')} rank {e.get('rs_rank')} "
                        f"d5 {e.get('rank_delta_5d')}  ·  {_age_days(d.get('date'))}"
                    )
        except Exception:
            pass
    return out


def check(ticker: str) -> dict:
    from agents.screener.finviz_agent import get_snapshot_metrics

    m = get_snapshot_metrics(ticker)
    if not m or m[0] is None:
        return {"ticker": ticker, "error": "no live data returned"}
    (atr_pct, _eps, _sales, dist_high, rel_vol, _avg_vol,
     sma20, sma50, sma200, _eps_qq, _io, _it,
     perf_month, perf_quarter, _perf_half, perf_year) = m

    mult = atr_multiple(1.0, sma50, atr_pct)
    warn, signal = tier_for(atr_pct or 0.0)
    if mult >= signal:
        status = "PAST SIGNAL — trim territory, not an entry"
    elif mult >= warn:
        status = "past warn — extended, wait for a pullback"
    else:
        status = "peel-safe — entry allowed by the extension gate"
    return {
        "ticker": ticker, "atr_pct": atr_pct, "dist_high": dist_high,
        "rel_vol": rel_vol, "sma20": sma20, "sma50": sma50, "sma200": sma200,
        "perf_month": perf_month, "perf_quarter": perf_quarter,
        "perf_year": perf_year, "mult": mult, "warn": warn,
        "signal": signal, "status": status,
    }


def render(r: dict) -> str:
    if r.get("error"):
        return f"{r['ticker']}: {r['error']}"
    L = [
        f"{r['ticker']}   LIVE @ {datetime.datetime.now(datetime.timezone.utc):%Y-%m-%d %H:%M UTC}",
        f"  ATR% {r['atr_pct']:.2f}   RVol {r['rel_vol']:.2f}   dist from 52w high {r['dist_high']:+.1f}%",
        f"  vs SMA20 {r['sma20']:+.1f}%   SMA50 {r['sma50']:+.1f}%   SMA200 {r['sma200']:+.1f}%",
        f"  Perf: month {r['perf_month']:+.1f}%   quarter {r['perf_quarter']:+.1f}%   year {r['perf_year']:+.1f}%",
        f"  ATR mult from 50MA = {r['mult']:.2f}   (tier warn {r['warn']} / signal {r['signal']})",
        f"  -> {r['status']}",
    ]
    ctx = cached_context(r["ticker"])
    if ctx:
        L.append("  CONTEXT (cached — NOT live):")
        L.extend(ctx)
    return "\n".join(L)


def main(argv: list) -> int:
    if not argv:
        print(__doc__)
        return 1
    for t in argv:
        print(render(check(t.upper().strip())))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
