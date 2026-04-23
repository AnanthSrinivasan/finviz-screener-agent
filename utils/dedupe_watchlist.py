"""
One-time migration: deduplicate data/watchlist.json.

Historical bug (see SPEC_ENTRY_SIGNALS.md §3c): the add-path dedupe check
excluded archived entries, so aged-out screener_auto entries got re-added as
fresh rows instead of being reactivated. Result: 13 duplicated tickers.

For each ticker with >1 entry, keep the row with highest priority
(entry-ready > focus > watching > archived), preserve earliest `added` and
earliest `focus_promoted_date`, and drop the rest.

After this migration runs, the runtime code (3a reactivate + 3b skip-age-out-
on-focus) prevents new duplicates by construction, so this script should only
ever need to run once.

Usage:
    python utils/dedupe_watchlist.py                 # dry-run
    python utils/dedupe_watchlist.py --apply         # write back to watchlist.json
    python utils/dedupe_watchlist.py --path FILE     # custom path (tests)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PRIORITY_RANK = {"entry-ready": 3, "focus": 2, "watching": 1, "archived": 0}


def _priority_key(entry: dict[str, Any]) -> int:
    """Rank an entry for merge-winner selection. Archived rows rank lowest."""
    if entry.get("status") == "archived":
        return PRIORITY_RANK["archived"]
    return PRIORITY_RANK.get(entry.get("priority", "watching"), 1)


def _merge(winner: dict[str, Any], loser: dict[str, Any]) -> dict[str, Any]:
    """Fold loser's history into winner: earliest added/promoted, keep winner's thesis."""
    merged = dict(winner)

    # Earliest `added` wins (preserves true entry date)
    added_dates = [d for d in (winner.get("added"), loser.get("added")) if d]
    if added_dates:
        merged["added"] = min(added_dates)

    # Earliest `focus_promoted_date` wins (preserves true first-promotion date)
    promo_dates = [
        d for d in (winner.get("focus_promoted_date"), loser.get("focus_promoted_date")) if d
    ]
    if promo_dates:
        merged["focus_promoted_date"] = min(promo_dates)

    # Keep winner's entry_note/thesis (winner is highest-priority, so its thesis is most recent)

    return merged


def dedupe(entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (deduped_list, list_of_tickers_that_were_deduped)."""
    by_ticker: dict[str, list[dict[str, Any]]] = {}
    for e in entries:
        t = e.get("ticker")
        if not t:
            continue
        by_ticker.setdefault(t, []).append(e)

    deduped: list[dict[str, Any]] = []
    touched: list[str] = []
    for ticker, rows in by_ticker.items():
        if len(rows) == 1:
            deduped.append(rows[0])
            continue
        # Sort by priority rank desc; stable on input order for ties
        rows_sorted = sorted(rows, key=_priority_key, reverse=True)
        winner = rows_sorted[0]
        for loser in rows_sorted[1:]:
            winner = _merge(winner, loser)
        deduped.append(winner)
        touched.append(ticker)

    return deduped, touched


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default="data/watchlist.json")
    ap.add_argument("--apply", action="store_true", help="write changes back (else dry-run)")
    args = ap.parse_args()

    path = Path(args.path)
    data = json.loads(path.read_text())
    entries = data.get("watchlist", [])
    before = len(entries)

    deduped, touched = dedupe(entries)
    after = len(deduped)

    if not touched:
        log.info("No duplicates found. %d entries, nothing to do.", before)
        return 0

    log.info("Deduped %d rows: %s", before - after, sorted(touched))
    log.info("Before: %d entries  →  After: %d entries", before, after)

    if args.apply:
        data["watchlist"] = deduped
        path.write_text(json.dumps(data, indent=2))
        log.info("Wrote %s", path)
    else:
        log.info("(dry-run — pass --apply to write)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
