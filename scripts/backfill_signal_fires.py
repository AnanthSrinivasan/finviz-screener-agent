"""Backfill historical signal fires from CURRENT state files only.

One-off script for the Signal Scorecard (docs/specs/signal-scorecard.md §2.4).
Reconstructs fires where recoverable — watchlist rows with source=*_auto and
their `added` dates, rs_leaders.json trigger/reacquired dates,
episodic_pivots.json last-fire dates, and today's hidden_growth.json snapshot.
Git-history archaeology is out of scope.

Every reconstructed record is tagged `backfilled: true` (lower confidence —
the weekly scorer excludes backfilled fires from lifetime stats by default).

Dry-run by default — prints what WOULD be written. Pass --apply to merge into
data/signal_fires.json (existing (date, block, ticker) records are never
overwritten; the 400-day rolling window is respected).

Usage:
    python scripts/backfill_signal_fires.py            # dry-run
    python scripts/backfill_signal_fires.py --apply    # write
"""
import argparse
import datetime
import json
import os
from collections import Counter

DATA_DIR = os.environ.get("DATA_DIR", "data")
FIRES_PATH = os.path.join(DATA_DIR, "signal_fires.json")
ROLLING_DAYS = 400

# watchlist `source` → scorecard block. `screener_auto` (the generic technical
# watchlist path) is deliberately unmapped — it is not attributable to a single
# callout block. `manual` entries are not signal fires.
WATCHLIST_SOURCE_TO_BLOCK = {
    "hidden_growth_auto":     "hidden_growth",
    "breakout_auto":          "fresh_breakout",
    "rs_leader_auto":         "rs_leader",
    "htf_base_reclaim_auto":  "htf_base_reclaim",
    "ema21_pb_auto":          "ema21_pullback",
    "stage_transition_auto":  "stage_transition",
    "recovery_leader_auto":   "recovery_leader",
    "rotation_catalyst_auto": "rotation_catalyst",
    "episodic_pivot_auto":    "episodic_pivot",
}


def _load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _valid_date(s) -> bool:
    try:
        datetime.date.fromisoformat(str(s))
        return True
    except (TypeError, ValueError):
        return False


def _record(date, block, ticker, q=None):
    return {
        "date": str(date),
        "block": block,
        "ticker": str(ticker).strip().upper(),
        "price": None,
        "q": round(float(q), 4) if q is not None else None,
        "atr_pct": None,
        "rank_in_block": None,
        "backfilled": True,
    }


def collect_backfill_records(data_dir: str = DATA_DIR) -> list:
    """Reconstruct fires from the four current state files. Deduped within
    the batch on (date, block, ticker)."""
    records = {}

    def _add(rec):
        key = (rec["date"], rec["block"], rec["ticker"])
        if rec["ticker"] and _valid_date(rec["date"]) and key not in records:
            records[key] = rec

    # 1) watchlist.json — source=*_auto rows, `added` date = fire date
    wl = _load_json(os.path.join(data_dir, "watchlist.json"), {})
    rows = wl.get("watchlist", []) if isinstance(wl, dict) else (wl or [])
    for row in rows:
        if not isinstance(row, dict):
            continue
        block = WATCHLIST_SOURCE_TO_BLOCK.get(str(row.get("source", "") or ""))
        if block and row.get("ticker") and row.get("added"):
            _add(_record(row["added"], block, row["ticker"]))

    # 2) rs_leaders.json — first_triggered + reacquired_dates
    rsl = _load_json(os.path.join(data_dir, "rs_leaders.json"), {})
    if isinstance(rsl, dict):
        for ticker, st in rsl.items():
            if not isinstance(st, dict):
                continue
            if st.get("first_triggered"):
                _add(_record(st["first_triggered"], "rs_leader", ticker,
                             q=st.get("trigger_q")))
            for d in st.get("reacquired_dates", []) or []:
                _add(_record(d, "rs_leader", ticker))

    # 3) episodic_pivots.json — {ticker: {last_fire_date, ...}}
    ep = _load_json(os.path.join(data_dir, "episodic_pivots.json"), {})
    if isinstance(ep, dict):
        for ticker, st in ep.items():
            if isinstance(st, dict) and st.get("last_fire_date"):
                _add(_record(st["last_fire_date"], "episodic_pivot", ticker))

    # 4) hidden_growth.json — today's snapshot {date, candidates: [...]}
    hg = _load_json(os.path.join(data_dir, "hidden_growth.json"), {})
    if isinstance(hg, dict) and hg.get("date"):
        for cand in hg.get("candidates", []) or []:
            if isinstance(cand, dict) and cand.get("ticker"):
                _add(_record(hg["date"], "hidden_growth", cand["ticker"]))

    return sorted(records.values(), key=lambda r: (r["date"], r["block"], r["ticker"]))


def merge_backfill(new_records: list, fires_path: str = FIRES_PATH,
                   today: str = None, rolling_days: int = ROLLING_DAYS) -> tuple:
    """Merge without overwriting existing (date, block, ticker) records and
    without violating the rolling window. Returns (merged_list, n_added,
    n_dupe, n_too_old) — caller decides whether to write."""
    today = today or datetime.date.today().isoformat()
    cutoff = (datetime.date.fromisoformat(today)
              - datetime.timedelta(days=rolling_days)).isoformat()
    existing = _load_json(fires_path, [])
    if isinstance(existing, dict):
        existing = existing.get("fires", []) or []
    existing = [r for r in existing if isinstance(r, dict)]
    seen = {(r.get("date"), r.get("block"), r.get("ticker")) for r in existing}

    merged = list(existing)
    n_added = n_dupe = n_too_old = 0
    for rec in new_records:
        key = (rec["date"], rec["block"], rec["ticker"])
        if key in seen:
            n_dupe += 1
            continue
        if rec["date"] < cutoff:
            n_too_old += 1
            continue
        merged.append(rec)
        seen.add(key)
        n_added += 1
    merged.sort(key=lambda r: (str(r.get("date", "")), str(r.get("block", "")),
                               str(r.get("ticker", ""))))
    return merged, n_added, n_dupe, n_too_old


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true",
                    help="write to data/signal_fires.json (default: dry-run)")
    ap.add_argument("--data-dir", default=DATA_DIR)
    args = ap.parse_args()

    fires_path = os.path.join(args.data_dir, "signal_fires.json")
    records = collect_backfill_records(args.data_dir)
    merged, n_added, n_dupe, n_too_old = merge_backfill(records, fires_path)

    by_block = Counter(r["block"] for r in records)
    print(f"Reconstructed {len(records)} backfill candidate(s):")
    for block, n in sorted(by_block.items()):
        print(f"  {block:<20} {n}")
    print(f"Would add {n_added} (skipped: {n_dupe} already present, "
          f"{n_too_old} older than {ROLLING_DAYS}d window) → {fires_path}")

    if not args.apply:
        print("DRY-RUN — nothing written. Re-run with --apply to write.")
        return

    os.makedirs(os.path.dirname(fires_path) or ".", exist_ok=True)
    with open(fires_path, "w") as f:
        json.dump(merged, f, indent=2)
    print(f"Wrote {len(merged)} record(s) to {fires_path} "
          "(all backfill records tagged backfilled: true).")


if __name__ == "__main__":
    main()
