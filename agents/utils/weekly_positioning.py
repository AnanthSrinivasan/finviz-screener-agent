"""Weekly Review §1 — Positioning & Book Risk.

Opens the Saturday weekly with the USER's own state, not the market's:
  - regime (market_state + ETF rotation regime)
  - position count vs cap (🚨 when over)
  - realized P&L this week (FIFO from data/position_history.json — the
    proven-correct source, NOT trading_state.json. See
    memory/feedback_pnl_source_of_truth)
  - book health counts: green / underwater / past-stop-held
  - biggest leak: losers held past their stop and the $ they're bleeding

Pure functions + html/slack renderers. No network. The weekly agent loads
positions.json / position_history.json / market state and passes them in.

Light theme only (see memory/feedback_light_theme). Plain English — no Δ
symbols, no HOT/STABLE/FADING categories.
"""

from __future__ import annotations

import datetime

from utils.pnl_walk import compute_pnl_from_events


# ----------------------------
# Realized P&L this week (FIFO)
# ----------------------------

def _event_date(ev: dict) -> str:
    """Return the YYYY-MM-DD date prefix of a position_history event."""
    raw = str(ev.get("date", "") or "")
    return raw[:10]


def realized_pnl_for_week(history: dict, since: str,
                          until: str | None = None) -> dict:
    """Compute realized P&L for SELLs dated in [since, until] (ISO YYYY-MM-DD).

    history: data/position_history.json `history` dict {TICKER: [events]} where
             each event is {date, action BUY/SELL, shares, price}.
    since:   inclusive start date (ISO). until: inclusive end (defaults today).

    Walks every ticker's events ascending to maintain a weighted-avg cost
    basis (so the realized leg uses the correct basis even when buys predate
    the window), but only counts realized P&L from SELLs whose date falls
    inside the window. Mirrors utils/pnl_walk semantics — single source of
    truth for the avg-cost walk.

    Returns {total, wins, losses, per_ticker, biggest_winner, biggest_loser}.
    """
    if until is None:
        until = datetime.date.today().isoformat()

    per_ticker: dict[str, float] = {}
    for ticker, events in (history or {}).items():
        evs = sorted(events or [], key=_event_date)
        running_shares = 0.0
        running_cost = 0.0
        realized = 0.0
        for ev in evs:
            sh = float(ev.get("shares", 0) or 0)
            px = float(ev.get("price", 0) or 0)
            action = str(ev.get("action", "")).upper()
            if sh <= 0 or px <= 0:
                continue
            if action == "BUY":
                running_cost += sh * px
                running_shares += sh
            elif action == "SELL":
                if running_shares <= 0:
                    continue
                avg = running_cost / running_shares
                sold = min(sh, running_shares)
                pnl = sold * (px - avg)
                running_cost -= sold * avg
                running_shares = max(0.0, running_shares - sold)
                if since <= _event_date(ev) <= until:
                    realized += pnl
        if abs(realized) > 1e-9:
            per_ticker[ticker] = round(realized, 2)

    total = round(sum(per_ticker.values()), 2)
    wins = sum(1 for v in per_ticker.values() if v > 0)
    losses = sum(1 for v in per_ticker.values() if v < 0)
    biggest_winner = max(per_ticker.items(), key=lambda kv: kv[1], default=None)
    biggest_loser = min(per_ticker.items(), key=lambda kv: kv[1], default=None)

    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "per_ticker": per_ticker,
        "biggest_winner": biggest_winner,   # (ticker, $) or None
        "biggest_loser": biggest_loser,     # (ticker, $) or None
    }


# ----------------------------
# Book health
# ----------------------------

def _current_price(pos: dict) -> float:
    """Best-effort current price for an open position from positions.json."""
    cp = pos.get("current_price")
    if cp:
        try:
            return float(cp)
        except (TypeError, ValueError):
            pass
    entry = float(pos.get("entry_price", 0) or 0)
    gain = pos.get("current_gain_pct")
    if entry > 0 and gain is not None:
        try:
            return entry * (1 + float(gain) / 100.0)
        except (TypeError, ValueError):
            pass
    return entry


def _gain_pct(pos: dict) -> float:
    g = pos.get("current_gain_pct")
    if g is not None:
        try:
            return float(g)
        except (TypeError, ValueError):
            pass
    entry = float(pos.get("entry_price", 0) or 0)
    cp = _current_price(pos)
    return ((cp - entry) / entry * 100.0) if entry > 0 else 0.0


def book_health(positions: list) -> dict:
    """Bucket open positions into green / underwater / past-stop-held.

    A position is `past_stop_held` when its current price is at/below its
    recorded stop_price (the user is holding through the stop — the core
    hold-in-hope failure mode). Those are excluded from `underwater` to avoid
    double-counting. `green` = gain > 0. `leak_usd` = summed unrealized loss
    across past-stop-held names (the bleeding the book is taking by not
    cutting).

    Returns {green, underwater, past_stop_held, leak_usd, leak_names}.
    """
    green = underwater = past_stop = 0
    leak_usd = 0.0
    leak_names: list[str] = []

    for pos in positions or []:
        gain = _gain_pct(pos)
        cp = _current_price(pos)
        stop = float(pos.get("stop_price", 0) or 0)
        shares = float(pos.get("shares", 0) or 0)
        entry = float(pos.get("entry_price", 0) or 0)
        is_past_stop = bool(stop > 0 and cp > 0 and cp <= stop)

        if is_past_stop:
            past_stop += 1
            leak_names.append(pos.get("ticker", "?"))
            if entry > 0 and shares > 0:
                leak_usd += (cp - entry) * shares
        elif gain > 0:
            green += 1
        else:
            underwater += 1

    return {
        "green": green,
        "underwater": underwater,
        "past_stop_held": past_stop,
        "leak_usd": round(leak_usd, 2),
        "leak_names": leak_names,
    }


# ----------------------------
# Summary assembly
# ----------------------------

def build_positioning_summary(positions: list, history: dict,
                              market_state: str, etf_regime: str | None,
                              position_cap: int, week_start: str,
                              week_end: str | None = None) -> dict:
    """Assemble the full §1 payload from already-loaded data."""
    realized = realized_pnl_for_week(history, week_start, week_end)
    health = book_health(positions)
    n_positions = len(positions or [])
    return {
        "market_state": market_state or "UNKNOWN",
        "etf_regime": etf_regime,
        "n_positions": n_positions,
        "position_cap": position_cap,
        "over_cap": n_positions > position_cap if position_cap else False,
        "realized": realized,
        "health": health,
        "week_start": week_start,
        "week_end": week_end or datetime.date.today().isoformat(),
    }


# ----------------------------
# Formatters
# ----------------------------

def _money(v: float) -> str:
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.0f}"


def _money_signed(v: float) -> str:
    return ("+" if v >= 0 else "-") + f"${abs(v):,.0f}"


# ----------------------------
# HTML render (light theme)
# ----------------------------

POSITIONING_CSS = """
.pos1-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px,1fr));
             gap: 12px; margin-bottom: 14px; }
.pos1-card { background: #ffffff; border: 1px solid #e5e7eb; border-radius: 10px;
             padding: 14px 16px; box-shadow: 0 1px 3px rgba(0,0,0,.04); }
.pos1-card.alert { border-left: 3px solid #dc2626; }
.pos1-label { font-size: 0.66rem; color: #9ca3af; text-transform: uppercase;
              letter-spacing: .06em; margin-bottom: 6px; }
.pos1-val   { font-size: 1.3rem; font-weight: 700; color: #111827; }
.pos1-sub   { font-size: 0.72rem; color: #6b7280; margin-top: 4px; }
.pos1-health { display: flex; gap: 18px; flex-wrap: wrap; font-size: 0.9rem; }
.pos1-health b { font-size: 1.15rem; }
.pos1-leak  { font-size: 0.8rem; color: #b91c1c; margin-top: 8px; line-height: 1.5; }
"""


def render_positioning_html(summary: dict) -> str:
    """Render §1 Positioning & Book Risk as a light-theme HTML block."""
    if not summary:
        return ""
    r = summary["realized"]
    h = summary["health"]
    cap = summary["position_cap"]
    n = summary["n_positions"]

    regime_line = summary["market_state"]
    if summary.get("etf_regime"):
        regime_line += f" · rotation {summary['etf_regime']}"

    cap_alert = " alert" if summary.get("over_cap") else ""
    cap_sub = "🚨 over cap — trim before adding" if summary.get("over_cap") else "within cap"

    tot = r["total"]
    tot_color = "#16a34a" if tot >= 0 else "#dc2626"
    wl = f"{r['wins']}W / {r['losses']}L"
    realized_sub = wl
    bw, bl = r.get("biggest_winner"), r.get("biggest_loser")
    extras = []
    if bw and bw[1] > 0:
        extras.append(f"best {bw[0]} {_money_signed(bw[1])}")
    if bl and bl[1] < 0:
        extras.append(f"worst {bl[0]} {_money_signed(bl[1])}")
    if extras:
        realized_sub += " · " + " · ".join(extras)

    leak_html = ""
    if h["past_stop_held"]:
        names = ", ".join(h["leak_names"][:6])
        leak_html = (
            f"<div class='pos1-leak'>⚠️ {h['past_stop_held']} held past stop "
            f"({names}) — bleeding {_money(h['leak_usd'])} unrealized. "
            f"This is the leak. Cut or re-justify.</div>"
        )

    return (
        "<h2>🎛️ Positioning &amp; Book Risk</h2>"
        "<div class='pos1-grid'>"
        "<div class='pos1-card'>"
        "<div class='pos1-label'>Regime</div>"
        f"<div class='pos1-val' style='font-size:1.05rem'>{regime_line}</div>"
        "<div class='pos1-sub'>market state · ETF rotation</div>"
        "</div>"
        f"<div class='pos1-card{cap_alert}'>"
        "<div class='pos1-label'>Positions vs cap</div>"
        f"<div class='pos1-val'>{n} / {cap}</div>"
        f"<div class='pos1-sub'>{cap_sub}</div>"
        "</div>"
        "<div class='pos1-card'>"
        "<div class='pos1-label'>Realized this week</div>"
        f"<div class='pos1-val' style='color:{tot_color}'>{_money_signed(tot)}</div>"
        f"<div class='pos1-sub'>{realized_sub}</div>"
        "</div>"
        "<div class='pos1-card'>"
        "<div class='pos1-label'>Book health</div>"
        "<div class='pos1-health'>"
        f"<span style='color:#16a34a'><b>{h['green']}</b> green</span>"
        f"<span style='color:#b45309'><b>{h['underwater']}</b> underwater</span>"
        f"<span style='color:#dc2626'><b>{h['past_stop_held']}</b> past stop</span>"
        "</div>"
        f"{leak_html}"
        "</div>"
        "</div>"
    )


# ----------------------------
# Slack render
# ----------------------------

def render_positioning_slack(summary: dict) -> str:
    """Render §1 as Slack mrkdwn. Returns empty string when summary missing."""
    if not summary:
        return ""
    r = summary["realized"]
    h = summary["health"]
    n = summary["n_positions"]
    cap = summary["position_cap"]

    regime_line = summary["market_state"]
    if summary.get("etf_regime"):
        regime_line += f" · rotation {summary['etf_regime']}"

    cap_str = f"{n}/{cap}"
    if summary.get("over_cap"):
        cap_str += " 🚨 over cap"

    tot = r["total"]
    tot_emoji = "🟢" if tot >= 0 else "🔴"
    realized_line = f"{tot_emoji} *Realized this week:* {_money_signed(tot)} ({r['wins']}W/{r['losses']}L)"
    bl = r.get("biggest_loser")
    if bl and bl[1] < 0:
        realized_line += f" · worst {bl[0]} {_money_signed(bl[1])}"

    lines = [
        "🎛️ *Positioning & Book Risk*",
        f"*Regime:* {regime_line}  ·  *Positions:* {cap_str}",
        realized_line,
        (f"*Book:* {h['green']} green · {h['underwater']} underwater · "
         f"{h['past_stop_held']} past stop"),
    ]
    if h["past_stop_held"]:
        names = ", ".join(h["leak_names"][:6])
        lines.append(
            f"⚠️ *Leak:* {h['past_stop_held']} held past stop ({names}) — "
            f"{_money(h['leak_usd'])} unrealized. Cut or re-justify."
        )
    return "\n".join(lines)
