"""Plain-English sector rotation label.

Maps an ETF's (rank, rank_delta_5d, rs_score) to a single decision tag so the
ETF rotation dashboard and Rotation Catalyst Slack block can read at a glance
without juggling 3 numeric concepts. Spec: docs/specs/rotation-catalyst-block.md §3.

Bands (first match wins):
  🔥 HOT     — rank ≤ 5  AND delta ≤ 0
  ↗ RISING  — rank 6-15 AND delta ≤ -3
  ↘ FADING  — delta ≥ +5 AND rs_score < 60
  ❄ COLD    — rank ≥ 20 AND delta ≥ +3
  → STABLE  — default (no meaningful rotation move)
"""

from __future__ import annotations


HOT = "🔥 HOT"
RISING = "↗ RISING"
STABLE = "→ STABLE"
FADING = "↘ FADING"
COLD = "❄ COLD"


def rotation_label(rank: int, delta: int, rs: int) -> str:
    """Return the rotation label for a single ETF.

    Args:
        rank: current rank in the rotation universe (1 = top).
        delta: rank_delta_5d (negative = rank improving).
        rs: RS score 0-99.
    """
    if rank <= 5 and delta <= 0:
        return HOT
    if 6 <= rank <= 15 and delta <= -3:
        return RISING
    if delta >= 5 and rs < 60:
        return FADING
    if rank >= 20 and delta >= 3:
        return COLD
    return STABLE
