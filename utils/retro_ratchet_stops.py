#!/usr/bin/env python3
"""
Retro-ratchet open position stops to where the new sell-rule ladder would put
them given each position's recorded peak.

Why: after switching from the old ATR-trail-then-breakeven engine to the
continuous ATR-tiered ladder (2.0/1.5/1.0 × ATR by peak gain) plus the +5%
loss-cap floor, existing open positions are sitting on stops that may be
lower than the new ladder would set them — particularly cases where the old
hourly snapshot caught a price below the intraday peak (VIK regression).

Strategy: simulate `rules.apply_position_rules` with current_price and
day_high both set to `highest_price_seen`. This forces the engine to evaluate
the trail at the recorded peak. The engine's max-only ratchet guarantees
stops never lower.

Modes:
  --paper   ratchet data/paper_stops.json    (atr_pct stored on each entry)
  --live    ratchet data/positions.json      (atr_pct fetched from latest screener CSV)
  --all     both

By default runs dry: prints proposed changes. Pass --apply to write.
"""

import argparse
import csv
import datetime
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.trading import rules  # noqa: E402


DATA_DIR = "data"
PAPER_STOPS = os.path.join(DATA_DIR, "paper_stops.json")
POSITIONS   = os.path.join(DATA_DIR, "positions.json")


def _latest_screener_atr_map() -> dict:
    """Ticker → ATR% from the most recent finviz_screeners_*.csv."""
    files = sorted(glob.glob(os.path.join(DATA_DIR, "finviz_screeners_*.csv")), reverse=True)
    if not files:
        return {}
    with open(files[0], newline="") as f:
        reader = csv.DictReader(f)
        return {row["Ticker"]: float(row["ATR%"]) for row in reader if row.get("ATR%")}


def _ratchet_one(ticker: str, entry: dict, atr_pct: float) -> tuple[float, float]:
    """Return (old_stop, new_stop). Mutates a copy, not the input."""
    old = float(entry.get("stop_price") or 0)
    peak = float(entry.get("highest_price_seen") or entry.get("entry_price") or 0)
    if peak <= 0 or atr_pct <= 0:
        return old, old
    sim = dict(entry)
    rules.apply_position_rules(ticker, sim, current_price=peak, day_high=peak,
                               atr_pct=atr_pct)
    return old, float(sim.get("stop_price") or old)


def ratchet_paper(apply: bool) -> None:
    if not os.path.exists(PAPER_STOPS):
        print("paper_stops.json not found — skipping paper")
        return
    with open(PAPER_STOPS) as f:
        stops = json.load(f)

    print("=== PAPER ===")
    changes = 0
    for ticker, entry in stops.items():
        atr_pct = float(entry.get("atr_pct") or 0)
        old, new = _ratchet_one(ticker, entry, atr_pct)
        if new > old + 0.005:
            print(f"  {ticker:6} stop {old:>10.2f} → {new:>10.2f}  "
                  f"(peak +{entry.get('peak_gain_pct', 0):.2f}%, "
                  f"ATR% {atr_pct:.2f})")
            if apply:
                entry["stop_price"] = round(new, 2)
            changes += 1
        else:
            print(f"  {ticker:6} stop {old:>10.2f}  (unchanged)")

    if apply and changes:
        with open(PAPER_STOPS, "w") as f:
            json.dump(stops, f, indent=2)
        print(f"  → wrote {changes} updates to {PAPER_STOPS}")
    elif changes:
        print(f"  ({changes} would change — re-run with --apply)")


def ratchet_live(apply: bool) -> None:
    if not os.path.exists(POSITIONS):
        print("positions.json not found — skipping live")
        return
    with open(POSITIONS) as f:
        data = json.load(f)
    open_positions = data.get("open_positions") or []
    if not open_positions:
        print("no open live positions")
        return

    atr_map = _latest_screener_atr_map()

    print("=== LIVE ===")
    changes = 0
    for pos in open_positions:
        ticker = pos["ticker"]
        atr_pct = atr_map.get(ticker, 0.0)
        if atr_pct <= 0:
            print(f"  {ticker:6} SKIPPED — no ATR% in latest screener CSV "
                  f"(next position-monitor tick will ratchet via live Finviz fetch)")
            continue
        old, new = _ratchet_one(ticker, pos, atr_pct)
        if new > old + 0.005:
            print(f"  {ticker:6} stop {old:>10.2f} → {new:>10.2f}  "
                  f"(peak +{pos.get('peak_gain_pct', 0):.2f}%, "
                  f"ATR% {atr_pct:.2f})")
            if apply:
                pos["stop_price"] = round(new, 2)
            changes += 1
        else:
            print(f"  {ticker:6} stop {old:>10.2f}  (unchanged)")

    if apply and changes:
        with open(POSITIONS, "w") as f:
            json.dump(data, f, indent=2)
        print(f"  → wrote {changes} updates to {POSITIONS}")
    elif changes:
        print(f"  ({changes} would change — re-run with --apply)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper", action="store_true")
    ap.add_argument("--live",  action="store_true")
    ap.add_argument("--all",   action="store_true")
    ap.add_argument("--apply", action="store_true",
                    help="write changes (default: dry-run)")
    args = ap.parse_args()

    if not (args.paper or args.live or args.all):
        ap.print_help()
        return

    do_paper = args.paper or args.all
    do_live  = args.live  or args.all

    print(f"retro-ratchet — {'APPLY' if args.apply else 'DRY-RUN'}  "
          f"{datetime.datetime.now().isoformat(timespec='seconds')}")

    if do_paper:
        ratchet_paper(args.apply)
    if do_live:
        ratchet_live(args.apply)


if __name__ == "__main__":
    main()
