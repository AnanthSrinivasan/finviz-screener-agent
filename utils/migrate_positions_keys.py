"""
One-shot migration. Rename persisted keys in data/positions.json:
  - stop -> stop_price
  - breakeven_stop_activated -> breakeven_activated

Also: clear stale status="stop_hit" -> "active" (system never sets it now).
Drops stop_type / current_gain_pct (recomputable, no longer written by the engine).

Idempotent: re-running is a no-op. Run once, commit, delete this file.
"""
import json
import sys
from pathlib import Path

POSITIONS_PATH = Path(__file__).resolve().parent.parent / "data" / "positions.json"


def migrate_position(p: dict) -> bool:
    changed = False
    if "stop" in p and "stop_price" not in p:
        p["stop_price"] = p.pop("stop")
        changed = True
    elif "stop" in p:  # both present (shouldn't happen) — drop legacy
        p.pop("stop")
        changed = True
    if "breakeven_stop_activated" in p and "breakeven_activated" not in p:
        p["breakeven_activated"] = p.pop("breakeven_stop_activated")
        changed = True
    elif "breakeven_stop_activated" in p:
        p.pop("breakeven_stop_activated")
        changed = True
    if p.get("status") == "stop_hit":
        p["status"] = "active"
        changed = True
    if "stop_type" in p:
        p.pop("stop_type")
        changed = True
    if "current_gain_pct" in p:
        p.pop("current_gain_pct")
        changed = True
    return changed


def main(apply: bool) -> int:
    data = json.loads(POSITIONS_PATH.read_text())
    counts = {"open": 0, "closed": 0}
    for pos in data.get("open_positions", []):
        if migrate_position(pos):
            counts["open"] += 1
    for pos in data.get("closed_positions", []):
        if migrate_position(pos):
            counts["closed"] += 1

    print(f"Open positions migrated: {counts['open']}")
    print(f"Closed positions migrated: {counts['closed']}")

    if apply:
        POSITIONS_PATH.write_text(json.dumps(data, indent=2) + "\n")
        print(f"Wrote {POSITIONS_PATH}")
    else:
        print("Dry-run only. Pass --apply to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main(apply="--apply" in sys.argv))
