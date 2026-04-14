#!/usr/bin/env python3
"""Winners → Watchlist — Monday evening auto-add.

Reads the latest weekly persistence CSV, takes the top 5 by signal score,
filters out tickers already on the watchlist, and appends survivors.
Sends a Slack summary of any additions.  No AI calls.
"""

import csv, glob, json, logging, os, sys
from datetime import date

import requests

log = logging.getLogger("winners_watchlist")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DATA_DIR = os.environ.get("DATA_DIR", "data")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
WATCHLIST_PATH = os.path.join(DATA_DIR, "watchlist.json")


def latest_file(prefix: str, ext: str) -> str | None:
    files = sorted(glob.glob(os.path.join(DATA_DIR, f"{prefix}*{ext}")))
    return files[-1] if files else None


def load_watchlist() -> dict:
    if os.path.exists(WATCHLIST_PATH):
        with open(WATCHLIST_PATH) as f:
            return json.load(f)
    return {"watchlist": []}


def save_watchlist(wl: dict):
    with open(WATCHLIST_PATH, "w") as f:
        json.dump(wl, f, indent=2)


def main():
    # 1. Load weekly persistence CSV (top rows are already sorted by signal score)
    csv_path = latest_file("finviz_weekly_persistence_", ".csv")
    if not csv_path:
        log.error("No weekly persistence CSV found."); return

    with open(csv_path) as f:
        rows = list(csv.DictReader(f))
    top5 = rows[:5]
    log.info("Weekly top 5: %s", [r["Ticker"] for r in top5])

    # 2. Load quality data for stage/q-rank
    quality = {}
    qpath = latest_file("daily_quality_", ".json")
    if qpath:
        with open(qpath) as f:
            quality = json.load(f)

    # 3. Filter against existing watchlist
    wl = load_watchlist()
    existing = {w["ticker"] for w in wl["watchlist"]}
    new_tickers = [r for r in top5 if r["Ticker"] not in existing]

    if not new_tickers:
        log.info("All top 5 already on watchlist — nothing to add."); return

    # 4. Append to watchlist
    today = date.today().isoformat()
    added_names = []
    for r in new_tickers:
        ticker = r["Ticker"]
        q = quality.get(ticker, {})
        wl["watchlist"].append({
            "ticker": ticker,
            "entry_note": f"Auto-added from weekly top 5 (signal {r.get('Signal Score', '?')})",
            "entry_price": None,
            "stop": None,
            "thesis": f"{r.get('Sector', '')} — {r.get('Industry', '')}",
            "added": today,
            "status": "watching",
            "priority": "watching",
            "source": "weekly_auto",
        })
        added_names.append(f"{ticker} (Q{q.get('q_rank', '?')}, {q.get('stage_label', r.get('Stage', '?'))})")

    save_watchlist(wl)
    log.info("Added %d tickers to watchlist: %s", len(added_names), added_names)

    # 5. Slack summary
    msg = f"📋 *Weekly Winners → Watchlist*\nAdded {len(added_names)} names:\n"
    msg += "\n".join(f"  • {n}" for n in added_names)
    if SLACK_WEBHOOK_URL:
        requests.post(SLACK_WEBHOOK_URL, json={"text": msg}, timeout=10)
    else:
        print(msg)


if __name__ == "__main__":
    main()
