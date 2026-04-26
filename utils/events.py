"""Shared event-feed helper used by all agents.

Writes to data/recent_events.json — the rolling feed that powers the
dashboard "Recent Alerts" widget.

category values: market_state | position_close | target_hit | breakeven
                 | stop_hit | peel_signal | retro_patch
severity values: low | med | high
"""
import os
import json
import datetime

DATA_DIR = os.environ.get("DATA_DIR", "data")
RECENT_EVENTS_FILE = os.path.join(DATA_DIR, "recent_events.json")


def _append_recent_event(
    category: str,
    title: str,
    date: str | None = None,
    severity: str = "med",
    detail: str | None = None,
    max_keep: int = 50,
) -> None:
    """Append one event to the rolling recent_events.json feed.

    Never raises — write failures are logged as warnings so they never
    block the calling agent.
    """
    import logging
    rec = {
        "ts": datetime.datetime.utcnow().isoformat() + "Z",
        "date": date or datetime.date.today().isoformat(),
        "category": category,
        "title": title,
        "severity": severity,
    }
    if detail:
        rec["detail"] = detail
    try:
        events_file = os.path.join(os.environ.get("DATA_DIR", "data"), "recent_events.json")
        if os.path.exists(events_file):
            with open(events_file) as f:
                data = json.load(f)
            events = data.get("events", []) if isinstance(data, dict) else []
        else:
            events = []
        events.append(rec)
        events = events[-max_keep:]
        with open(events_file, "w") as f:
            json.dump({"updated": rec["ts"], "events": events}, f, indent=2)
    except Exception as e:
        logging.getLogger(__name__).warning(f"recent_events write failed: {e}")
