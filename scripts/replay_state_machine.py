#!/usr/bin/env python3
"""Replay the v2 market state machine against historical breadth snapshots.

Loads data/market_monitor_*.json for the last N days, recomputes SPY+QQQ
extension metrics from Alpaca daily bars as-of each historical date, and
re-classifies the day using the new rules (EXTENDED + STEADY-UPTREND).
Prints a table: date | old_state | new_state | spy_atr_mult | spy_sma50% | qqq_atr_mult.

Usage:  python scripts/replay_state_machine.py [--days 60]
"""

import argparse
import datetime
import glob
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from agents.market import market_monitor as mm  # noqa: E402
from utils.calibrate_peel import wilder_atr, compute_sma  # noqa: E402

ALPACA_DATA_URL = "https://data.alpaca.markets/v2"


def fetch_bars(ticker: str, days: int = 400):
    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY missing")
    end = datetime.date.today()
    start = end - datetime.timedelta(days=days)
    resp = requests.get(
        f"{ALPACA_DATA_URL}/stocks/{ticker}/bars",
        headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
        params={
            "timeframe": "1Day",
            "start": start.isoformat() + "T00:00:00Z",
            "end":   end.isoformat()   + "T23:59:59Z",
            "limit": 1000,
            "adjustment": "raw",
            "feed": "iex",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("bars", [])


def compute_asof(bars, date_str: str):
    """Return (atr_mult_50, sma50_pct, sma200_pct) using bars up to and including date_str."""
    idx = None
    for i, b in enumerate(bars):
        if b["t"][:10] <= date_str:
            idx = i
        else:
            break
    if idx is None or idx < 49:
        return (None, None, None)
    sub = bars[: idx + 1]
    closes = [b["c"] for b in sub]
    sma50 = compute_sma(closes, 50)[-1]
    sma200 = compute_sma(closes, 200)[-1] if len(closes) >= 200 else None
    atr14 = wilder_atr(sub)[-1]
    close = closes[-1]
    if not (sma50 and atr14):
        return (None, None, None)
    atr_mult_50 = round((close - sma50) * close / (sma50 * atr14), 2)
    sma50_pct = round((close - sma50) / sma50 * 100, 2)
    sma200_pct = round((close - sma200) / sma200 * 100, 2) if sma200 else None
    return (atr_mult_50, sma50_pct, sma200_pct)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    args = ap.parse_args()

    files = sorted(glob.glob(str(ROOT / "data" / "market_monitor_2*.json")))
    files = [f for f in files if "history" not in f][-args.days:]

    print("Fetching SPY + QQQ daily bars (Alpaca) ...", file=sys.stderr)
    spy_bars = fetch_bars("SPY")
    qqq_bars = fetch_bars("QQQ")
    print(f"  SPY bars: {len(spy_bars)}  QQQ bars: {len(qqq_bars)}", file=sys.stderr)

    print(f"{'date':<12}{'old':<10}{'new':<18}{'spy_mult':<10}{'spy_50%':<10}{'qqq_mult':<10}")
    print("-" * 70)

    prev_state = None
    flips_to_extended = 0
    flips_to_steady = 0
    rescued_from_red = 0
    bullish_at_top = 0
    for f in files:
        with open(f) as fh:
            rec = json.load(fh)
        date_str = rec["date"]
        spy_mult, spy_50, _ = compute_asof(spy_bars, date_str)
        qqq_mult, _, _ = compute_asof(qqq_bars, date_str)

        today_data = {
            "up_4_today":       rec["up_4_today"],
            "down_4_today":     rec["down_4_today"],
            "spy_atr_mult_50":  spy_mult,
            "spy_sma50_pct":    spy_50,
            "qqq_atr_mult_50":  qqq_mult,
        }
        metrics = {
            "ratio_today":   rec["ratio_today"],
            "ratio_5day":    rec["ratio_5day"],
            "ratio_10day":   rec["ratio_10day"],
            "thrust":        rec["thrust_detected"],
            "spy_above_200d": rec["spy_above_200d"],
        }
        date = datetime.date.fromisoformat(date_str)
        new_state, _, _ = mm.classify_market_state(
            metrics,
            fg=rec.get("fg"),
            spy_price=rec.get("spy_price"),
            spy_above_200d=rec.get("spy_above_200d", False),
            today_data=today_data,
            date=date,
            prev_state=prev_state,
            last_thrust_date=None,  # ignored for backtest noise
            consecutive_weak_days=0,
        )
        old = rec["market_state"]
        if new_state == "EXTENDED" and old != "EXTENDED":
            flips_to_extended += 1
            if old in ("GREEN", "THRUST"):
                bullish_at_top += 1
        if new_state == "STEADY-UPTREND":
            flips_to_steady += 1
            if old == "RED":
                rescued_from_red += 1
        print(f"{date_str:<12}{old:<10}{new_state:<18}"
              f"{(str(spy_mult) if spy_mult is not None else '-'):<10}"
              f"{(str(spy_50)+'%' if spy_50 is not None else '-'):<10}"
              f"{(str(qqq_mult) if qqq_mult is not None else '-'):<10}")
        prev_state = new_state

    print("-" * 70)
    print(f"flips → EXTENDED:        {flips_to_extended}")
    print(f"  ... at a GREEN/THRUST top: {bullish_at_top}  "
          f"(safety: blocks chase)")
    print(f"flips → STEADY-UPTREND:  {flips_to_steady}  "
          f"(rescued from RED: {rescued_from_red})")


if __name__ == "__main__":
    main()
