"""Swing-pivot detector — distance from recent (non-52w-high) swing high.

Used by HTF Base Reclaim signal to catch RKLB-class names where dist-from-52w-high
disqualifies but dist-from-recent-swing-high is tight (price has reclaimed the
prior local pivot from a deeper drawdown).
"""
from __future__ import annotations

import os
import logging
from typing import Optional

import requests

log = logging.getLogger(__name__)

ALPACA_BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"


def _alpaca_headers() -> dict:
    key = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        return {}
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def fetch_swing_pivots_batch(
    tickers: list[str], days: int = 90, exclude_last: int = 5
) -> dict[str, dict]:
    """Multi-symbol bars fetch. Returns {ticker: {swing_high, swing_high_date,
    dist_from_swing_high_pct, last_close}} for each ticker that returned bars.

    Skips hyphen/dot tickers (BF-B etc) — Alpaca rejects.
    Batches 100/call. Uses feed=iex, adjustment=split (matches retro caveats).
    """
    headers = _alpaca_headers()
    if not headers:
        log.warning("swing_pivot: missing Alpaca creds, returning empty")
        return {}

    from datetime import datetime, timedelta, timezone
    end = datetime.now(timezone.utc) - timedelta(days=1)
    start = end - timedelta(days=int(days * 1.6) + 10)  # buffer for weekends/holidays
    start_iso = start.strftime("%Y-%m-%d")

    out: dict[str, dict] = {}
    BATCH = 100
    clean = [t for t in tickers if t and "-" not in t and "." not in t]
    for i in range(0, len(clean), BATCH):
        batch = clean[i : i + BATCH]
        params = {
            "symbols": ",".join(batch),
            "timeframe": "1Day",
            "start": start_iso,
            "limit": 10000,
            "adjustment": "split",
            "feed": "iex",
        }
        try:
            r = requests.get(ALPACA_BARS_URL, params=params, headers=headers, timeout=30)
        except Exception as e:
            log.warning("swing_pivot: request error %s", e)
            continue
        if r.status_code != 200:
            log.warning("swing_pivot: HTTP %s — %s", r.status_code, r.text[:200])
            continue
        bars_by_t = r.json().get("bars", {}) or {}
        for tk, rows in bars_by_t.items():
            res = _compute_from_rows(rows, days=days, exclude_last=exclude_last)
            if res is not None:
                out[tk] = res
    return out


def compute_swing_pivot(
    ticker: str, days: int = 90, exclude_last: int = 5
) -> Optional[dict]:
    """Single-ticker convenience wrapper. Returns the same dict shape as the
    batch version, or None on no-data / error / hyphen-ticker.
    """
    if not ticker or "-" in ticker or "." in ticker:
        return None
    res = fetch_swing_pivots_batch([ticker], days=days, exclude_last=exclude_last)
    return res.get(ticker)


def _compute_from_rows(rows: list[dict], days: int, exclude_last: int) -> Optional[dict]:
    """Compute swing-pivot dict from raw Alpaca bar rows."""
    if not rows:
        return None
    rows = sorted(rows, key=lambda b: b.get("t", ""))
    rows = rows[-days:]  # last N bars
    if len(rows) < exclude_last + 5:
        return None
    pivot_window = rows[:-exclude_last] if exclude_last > 0 else rows
    if not pivot_window:
        return None
    swing_bar = max(pivot_window, key=lambda b: b.get("h", 0))
    swing_high = float(swing_bar.get("h") or 0)
    if swing_high <= 0:
        return None
    last_close = float(rows[-1].get("c") or 0)
    if last_close <= 0:
        return None
    dist_pct = (last_close - swing_high) / swing_high * 100.0
    return {
        "swing_high":                swing_high,
        "swing_high_date":           (swing_bar.get("t") or "")[:10],
        "dist_from_swing_high_pct":  dist_pct,
        "last_close":                last_close,
    }
