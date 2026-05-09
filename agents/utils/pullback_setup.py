"""
Weekly Pullback Re-entry classifier — 21 EMA lane.

For a list of recurring high-signal tickers, pull last 30 daily bars and bucket
each name as 'entry_zone' / 'watching' / 'mid_flight' / 'extended' based on
distance from the 21 EMA, peel-warn calibration, and a hard quality bar.
"""

from __future__ import annotations

from typing import Callable, Iterable

from agents.trading.rules import _ema


def compute_21ema(closes: list) -> float | None:
    """Return the 21-period EMA from a list of closes (oldest first).

    Returns None when fewer than 22 bars are supplied — EMA needs a warmup
    window before it stabilises.
    """
    if not closes or len(closes) < 22:
        return None
    series = _ema(list(closes), span=21)
    return float(series[-1]) if series else None


def classify_pullback_setup(
    price: float,
    ema21: float | None,
    sma50_pct: float,
    peel_warn: float,
    q: float,
    rs: float,
    atr_pct: float,
    dist_from_high: float,
) -> str:
    """Bucket a single ticker.

    Returns one of: 'entry_zone', 'watching', 'mid_flight', 'extended',
    'below_ema', 'skip'.
    """
    if q < 80 or rs < 60 or atr_pct < 3 or atr_pct > 6 or dist_from_high > 0:
        return "skip"
    if dist_from_high < -12:
        return "skip"

    peel_mult = (sma50_pct / atr_pct) if atr_pct > 0 else 0.0
    if peel_mult > peel_warn:
        return "extended"

    if ema21 is None or price <= 0 or ema21 <= 0:
        return "skip"

    gap_pct = (price - ema21) / ema21 * 100.0
    if -1.5 <= gap_pct <= 1.5:
        return "entry_zone"
    if 1.5 < gap_pct <= 4.0:
        return "watching"
    if gap_pct > 4.0:
        return "mid_flight"
    return "below_ema"


def _bar_close(bar: dict) -> float | None:
    for k in ("c", "close", "Close"):
        if k in bar:
            try:
                return float(bar[k])
            except (TypeError, ValueError):
                return None
    return None


def build_pullback_rows(
    persistence_df,
    latest_daily_df,
    fetch_bars_fn: Callable[[str, int], list],
    peel_loader_fn: Callable[[float, str], tuple],
    max_tickers: int = 35,
) -> dict:
    """Bucket the recurring-names list.

    persistence_df:   DataFrame from build_persistence_scores (already filtered
                       to recurring names — caller passes in the leaderboard).
    latest_daily_df:  Most recent day's screener CSV — provides current ATR%,
                       SMA50%, RS Rating, Dist From High%, Quality Score, price
                       (Finviz doesn't give price, so we use the last Alpaca
                       bar close).
    fetch_bars_fn:    callable(ticker, limit) -> list[bar dicts with 'c'].
    peel_loader_fn:   callable(atr_pct, ticker) -> (warn_multiple, source).

    Returns: {"entry_zone": [...], "watching": [...], "mid_flight": [...],
              "extended": [...]}. Each row is a dict.
    """
    buckets: dict = {"entry_zone": [], "watching": [], "mid_flight": [], "extended": []}

    if persistence_df is None or len(persistence_df) == 0:
        return buckets

    daily_idx = {}
    if latest_daily_df is not None and len(latest_daily_df) > 0:
        for _, drow in latest_daily_df.iterrows():
            t = str(drow.get("Ticker", "")).strip().upper()
            if t and t not in daily_idx:
                daily_idx[t] = drow

    tickers: Iterable = persistence_df["Ticker"].head(max_tickers).tolist()

    for ticker in tickers:
        ticker = str(ticker).strip().upper()
        drow = daily_idx.get(ticker)
        if drow is None:
            continue

        try:
            atr_pct = float(drow.get("ATR%") or 0)
            sma50_pct = float(drow.get("SMA50%") or 0)
            q = float(drow.get("Quality Score") or 0)
            rs = float(drow.get("RS Rating") or 0)
            dist_high = float(drow.get("Dist From High%") or 0)
        except (TypeError, ValueError):
            continue

        peel_warn, peel_src = peel_loader_fn(atr_pct, ticker)

        bars = fetch_bars_fn(ticker, 30) or []
        closes = [c for c in (_bar_close(b) for b in bars) if c is not None]
        ema21 = compute_21ema(closes)
        price = closes[-1] if closes else 0.0

        bucket = classify_pullback_setup(
            price=price, ema21=ema21, sma50_pct=sma50_pct, peel_warn=peel_warn,
            q=q, rs=rs, atr_pct=atr_pct, dist_from_high=dist_high,
        )
        if bucket in ("skip", "below_ema"):
            continue

        peel_mult = (sma50_pct / atr_pct) if atr_pct > 0 else 0.0
        gap_pct = ((price - ema21) / ema21 * 100.0) if (ema21 and ema21 > 0) else 0.0

        buckets[bucket].append({
            "ticker": ticker,
            "company": drow.get("Company", ""),
            "sector": drow.get("Sector", ""),
            "price": round(price, 2),
            "ema21": round(ema21, 2) if ema21 else None,
            "gap_pct": round(gap_pct, 2),
            "q": int(q) if q else 0,
            "rs": int(rs) if rs else 0,
            "atr_pct": round(atr_pct, 1),
            "sma50_pct": round(sma50_pct, 1),
            "peel_mult": round(peel_mult, 1),
            "peel_warn": round(peel_warn, 1),
            "peel_src": peel_src,
            "dist_from_high": round(dist_high, 1),
        })

    buckets["entry_zone"].sort(key=lambda r: -r["q"])
    buckets["watching"].sort(key=lambda r: r["gap_pct"])
    buckets["mid_flight"].sort(key=lambda r: r["gap_pct"])
    buckets["extended"].sort(key=lambda r: -r["peel_mult"])

    return buckets
