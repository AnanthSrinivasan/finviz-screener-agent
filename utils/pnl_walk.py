"""Shared realized + unrealized P/L walker.

Consumed by both:
  - utils/generators/generate_dashboard.py — open/closed position $P/L cells
  - utils/generate_performance.py — partial-realized trades from
    data/position_history.json so still-open positions with prior SELLs
    (AAOI/GLW class) surface on the performance dashboard.

Single source of truth — do NOT duplicate this walk elsewhere.
"""


def compute_pnl_from_events(events, current_price=0.0, current_shares=0.0):
    """Walk BUY/SELL events ascending, return realized + unrealized P/L.

    Returns {realized, unrealized, avg_cost, total_bought_units, total_sold_units,
    final_shares}. SELLs use weighted-avg cost basis at time of sale.
    current_shares/current_price drive the unrealized leg; when current_shares is
    falsy, unrealized falls back to final running shares from the walk.
    """
    realized = 0.0
    running_shares = 0.0
    running_cost = 0.0
    total_bought = 0.0
    total_sold = 0.0
    proceeds_sold = 0.0       # gross $ received from SELLs
    cost_basis_sold = 0.0     # cost basis of shares actually sold (at avg-at-time)
    for ev in events or []:
        sh = float(ev.get("shares", 0) or 0)
        px = float(ev.get("price", 0) or 0)
        action = ev.get("action", "")
        if sh <= 0 or px <= 0:
            continue
        if action == "BUY":
            running_cost += sh * px
            running_shares += sh
            total_bought += sh
        elif action == "SELL":
            if running_shares > 0:
                avg = running_cost / running_shares
                sold = min(sh, running_shares)
                realized += sold * (px - avg)
                proceeds_sold += sold * px
                cost_basis_sold += sold * avg
                running_cost -= sold * avg
                running_shares = max(0.0, running_shares - sold)
                total_sold += sold
    avg_cost = (running_cost / running_shares) if running_shares > 0 else 0.0
    shares_for_unreal = float(current_shares) if current_shares else running_shares
    unrealized = shares_for_unreal * (current_price - avg_cost) if (shares_for_unreal > 0 and current_price > 0 and avg_cost > 0) else 0.0
    return {
        "realized": realized,
        "unrealized": unrealized,
        "avg_cost": avg_cost,
        "total_bought_units": total_bought,
        "total_sold_units": total_sold,
        "final_shares": running_shares,
        "proceeds_sold": proceeds_sold,
        "cost_basis_sold": cost_basis_sold,
    }
