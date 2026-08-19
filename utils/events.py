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
    log = logging.getLogger(__name__)
    try:
        events_file = os.path.join(os.environ.get("DATA_DIR", "data"), "recent_events.json")
        events = []
        if os.path.exists(events_file):
            try:
                with open(events_file) as f:
                    data = json.load(f)
                events = data.get("events", []) if isinstance(data, dict) else []
            except (ValueError, OSError) as e:
                # Recover instead of giving up. A corrupt file used to fall to
                # the outer handler, so the append was dropped with only a
                # warning and the feed could never heal itself: a single stray
                # byte froze this file from 2026-04-27 to 2026-08-19, silently
                # discarding every market-state transition in between.
                log.warning("recent_events unreadable (%s) — salvaging and rewriting", e)
                events = _salvage_events(events_file)
        events.append(rec)
        events = events[-max_keep:]
        # Atomic write: a torn concurrent write is what produced the trailing
        # byte in the first place. Rename is atomic on POSIX.
        tmp = events_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"updated": rec["ts"], "events": events}, f, indent=2)
        os.replace(tmp, events_file)
    except Exception as e:
        log.warning(f"recent_events write failed: {e}")


def _salvage_events(path: str) -> list:
    """Best-effort recovery of the events list from a damaged feed file.
    Returns [] when nothing is readable — a reset feed beats a dead one."""
    try:
        with open(path) as f:
            raw = f.read()
        obj, _ = json.JSONDecoder().raw_decode(raw)
        if isinstance(obj, dict) and isinstance(obj.get("events"), list):
            return obj["events"]
    except Exception:
        pass
    return []
