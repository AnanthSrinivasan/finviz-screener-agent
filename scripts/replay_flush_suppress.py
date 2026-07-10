#!/usr/bin/env python3
"""
Replay the flush-suppress stop filter over recent closed positions.

Part of the live retrofit gate (docs/specs/flush-suppress-stop-filter.md §Scope):
live enablement requires a replay over the last 90 days of stop-outs showing
saves ≥ 2× damage in $ terms.

For every closed position whose exit landed inside an active flush window, the
script asks: would the filter have suppressed the exit? If yes, it simulates
holding to the filter's own exit (first close below the structure EMA, or the
window-expiry close if price is still below the recorded exit) and reports the
$ delta vs the actual exit. Positive = save, negative = damage.

Data gaps (breadth snapshot missing for a date, no Alpaca bars) skip the trade
with a note — the report says how many trades were evaluable.

Usage: python scripts/replay_flush_suppress.py [--days 90]
Needs ALPACA_API_KEY / ALPACA_SECRET_KEY in the environment (.env).
"""

import argparse
import datetime
import glob
import json
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.trading import rules  # noqa: E402

DATA_DIR = os.environ.get("DATA_DIR", "data")


def load_breadth_by_date() -> dict:
    """date → market monitor record, merged from history + daily snapshots."""
    by_date = {}
    for path in sorted(glob.glob(os.path.join(DATA_DIR, "market_monitor_*.json"))):
        if "history" in path:
            continue
        try:
            with open(path) as f:
                rec = json.load(f)
            if rec.get("date"):
                by_date[rec["date"]] = rec
        except Exception:
            pass
    try:
        with open(os.path.join(DATA_DIR, "market_monitor_history.json")) as f:
            for rec in json.load(f):
                if rec.get("date"):
                    by_date[rec["date"]] = rec
    except Exception:
        pass
    return by_date


def flush_ctx_for_date(by_date: dict, date_iso: str) -> dict | None:
    dates = sorted(d for d in by_date if d <= date_iso)
    if not dates:
        return None
    window = [by_date[d] for d in dates[-rules.FLUSH_MAX_SUPPRESS_SESSIONS:]]
    if window[-1].get("date") != date_iso:
        return None  # no breadth snapshot for the exit day itself
    return rules.flush_window_active(window)


def fetch_bars(ticker: str, limit: int = 250) -> list:
    key = os.environ.get("ALPACA_API_KEY", "")
    sec = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not sec:
        raise SystemExit("ALPACA_API_KEY / ALPACA_SECRET_KEY required (source .env)")
    start = (datetime.date.today() - datetime.timedelta(days=limit * 2)).isoformat()
    resp = requests.get(
        "https://data.alpaca.markets/v2/stocks/" + ticker + "/bars",
        params={"timeframe": "1Day", "start": start, "limit": 10000,
                "feed": "iex", "adjustment": "raw"},
        headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec},
        timeout=10,
    )
    if not resp.ok:
        return []
    return (resp.json().get("bars", []) or [])[-limit:]


def replay_trade(pos: dict, by_date: dict) -> dict:
    """Returns {status, ...} — status ∈ evaluated / skipped."""
    ticker = pos.get("ticker", "?")
    exit_date = pos.get("close_date") or ""
    exit_px = float(pos.get("close_price") or 0)
    entry_px = float(pos.get("entry_price") or 0)
    shares = float(pos.get("shares") or 0)
    if not exit_date or exit_px <= 0 or entry_px <= 0:
        return {"status": "skipped", "ticker": ticker, "note": "incomplete close record"}

    # Only stop-driven exits are in scope — the filter never gates discretionary
    # sells (a sale near highs during a flush window is not a whipsaw stop-out).
    stop_px = float(pos.get("stop_price") or 0)
    if stop_px <= 0 or exit_px > stop_px * 1.02:
        return {"status": "evaluated", "ticker": ticker, "affected": False,
                "note": "not a stop-driven exit"}

    ctx = flush_ctx_for_date(by_date, exit_date)
    if not ctx:
        return {"status": "evaluated", "ticker": ticker, "affected": False,
                "note": "no flush window on " + exit_date}

    bars = fetch_bars(ticker)
    dated = [(b["t"][:10], float(b["c"])) for b in bars if b.get("c") is not None]
    idx = next((i for i, (d, _) in enumerate(dated) if d >= exit_date), None)
    if idx is None or idx < 25:
        return {"status": "skipped", "ticker": ticker, "note": "insufficient bars"}

    closes_to_exit = [c for _, c in dated[: idx + 1]]
    exit_close = closes_to_exit[-1]
    # Perf Month proxy: 21-session return into the exit day.
    perf_month = ((exit_close / closes_to_exit[-22]) - 1) * 100 \
        if len(closes_to_exit) >= 22 else 0.0
    suppress, reason = rules.should_suppress_stop_exit(
        closes_to_exit, exit_close, float(pos.get("atr_pct") or 0), entry_px,
        perf_month, ctx)
    if not suppress:
        return {"status": "evaluated", "ticker": ticker, "affected": False,
                "note": "not suppressed — " + reason}

    # Simulate the hold: from the day after the exit, leave at the first close
    # below the structure EMA; at window expiry leave at that day's close if
    # still below the recorded stop-out price; else keep riding until either
    # trigger (cap 10 sessions so the sim always terminates).
    span = rules.flush_structure_ema_span(perf_month)
    remaining = ctx["max_days"] - ctx["day"]
    sim_exit_px, sim_exit_date, sim_reason = exit_close, exit_date, "no forward bars"
    series = list(closes_to_exit)
    for step, (d, c) in enumerate(dated[idx + 1: idx + 11], start=1):
        series.append(c)
        ema_val = rules._ema(series, span)[-1]
        if c < ema_val:
            sim_exit_px, sim_exit_date, sim_reason = c, d, "close below " + str(span) + " EMA"
            break
        if step >= remaining and c <= exit_px:
            sim_exit_px, sim_exit_date, sim_reason = c, d, "window expired below stop"
            break
        sim_exit_px, sim_exit_date, sim_reason = c, d, "riding (sim cap)"

    delta_usd = (sim_exit_px - exit_px) * shares
    return {"status": "evaluated", "ticker": ticker, "affected": True,
            "exit_date": exit_date, "actual_exit": exit_px,
            "sim_exit": sim_exit_px, "sim_exit_date": sim_exit_date,
            "sim_reason": sim_reason, "delta_usd": round(delta_usd, 2),
            "hold_reason": reason}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    args = ap.parse_args()

    cutoff = (datetime.date.today() - datetime.timedelta(days=args.days)).isoformat()
    with open(os.path.join(DATA_DIR, "positions.json")) as f:
        closed = json.load(f).get("closed_positions", [])
    recent = [p for p in closed if (p.get("close_date") or "") >= cutoff]

    by_date = load_breadth_by_date()
    print(f"Replaying {len(recent)} closes since {cutoff} "
          f"({len(by_date)} breadth snapshots available)\n")

    saves = damage = 0.0
    affected = skipped = 0
    for pos in recent:
        r = replay_trade(pos, by_date)
        if r["status"] == "skipped":
            skipped += 1
            print(f"  SKIP {r['ticker']}: {r['note']}")
            continue
        if not r.get("affected"):
            continue
        affected += 1
        d = r["delta_usd"]
        saves += max(d, 0.0)
        damage += max(-d, 0.0)
        print(f"  {r['ticker']}: suppressed ({r['hold_reason']}) — actual exit "
              f"${r['actual_exit']:.2f} {r['exit_date']} → sim exit "
              f"${r['sim_exit']:.2f} {r['sim_exit_date']} ({r['sim_reason']}) "
              f"→ Δ ${d:+,.2f}")

    print(f"\nAffected trades: {affected} · skipped (data gaps): {skipped}")
    print(f"Saves:  ${saves:,.2f}")
    print(f"Damage: ${damage:,.2f}")
    if damage > 0:
        print(f"Save/damage ratio: {saves / damage:.2f}x (live gate needs ≥ 2x)")
    elif affected:
        print("Save/damage ratio: ∞ (no damage)")
    print("\nLive gate also requires ≥3 real paper suppression events with net "
          "positive outcome over ≥4 weeks — this replay covers only half the gate.")


if __name__ == "__main__":
    main()
