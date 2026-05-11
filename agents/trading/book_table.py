"""
Position Book — consolidated 3x daily Slack message.

Replaces per-event hourly Slack spam. One readable table per book run, with a
top-of-message ACTIONS block for trims/stops and an EVENTS DIGEST footnote of
inter-post events that didn't warrant a critical immediate post.

Pure functions only — caller (`position_monitor.py`) supplies positions, live
prices, market_state, sizing_mode, and a digest event log. Critical events
(stop_hit / auto_closed / share-drift / T1/T2) are posted separately and never
through this module.

State map (`compute_state`):
  🔻 STOPPED      stop_hit OR auto_closed THIS RUN
  🚨 STOP NEAR    abs(current - stop) / current < 0.5%
  🚨 ROUND-TRIP   peak_gain >= 15% AND current_pct < (peak_gain - 18)
  ⚠ TRIM          peak_gain >= 25% AND current_pct < (peak_gain - 10)
                  AND target1_hit AND shares_unchanged_since_t1
  ✓ HOLD         default
"""

import json
import os
import re

ROUND_TRIP_PEAK_MIN = 15.0
ROUND_TRIP_GIVEBACK = 18.0
TRIM_PEAK_MIN = 25.0
TRIM_GIVEBACK = 10.0
STOP_NEAR_PCT = 0.005

STATE_HOLD       = "✓ HOLD"
STATE_STOP_NEAR  = "🚨 STOP NEAR"
STATE_ROUND_TRIP = "🚨 ROUND-TRIP"
STATE_TRIM       = "⚠ TRIM"
STATE_STOPPED    = "🔻 STOPPED"


def compute_state(position: dict, live_price: float, stopped_this_run: bool = False) -> str:
    """Return one of the 5 STATE strings for this position row."""
    if stopped_this_run:
        return STATE_STOPPED

    entry = float(position.get("entry_price") or 0)
    stop  = float(position.get("stop_price") or 0)
    peak  = float(position.get("peak_gain_pct") or 0)
    if entry <= 0 or live_price <= 0:
        return STATE_HOLD

    cur_pct = (live_price - entry) / entry * 100

    if stop > 0 and live_price > 0:
        if abs(live_price - stop) / live_price < STOP_NEAR_PCT:
            return STATE_STOP_NEAR

    # TRIM is more specific than ROUND-TRIP — it implies T1 has already
    # locked some gains, so give-back from peak is "lock more profit", not
    # "this round-tripped a winner." Evaluate TRIM first.
    if (
        peak >= TRIM_PEAK_MIN
        and cur_pct < (peak - TRIM_GIVEBACK)
        and position.get("target1_hit")
    ):
        return STATE_TRIM

    if peak >= ROUND_TRIP_PEAK_MIN and cur_pct < (peak - ROUND_TRIP_GIVEBACK):
        return STATE_ROUND_TRIP

    return STATE_HOLD


def _fmt_money(v: float) -> str:
    sign = "+" if v >= 0 else "-"
    return f"{sign}${abs(v):,.0f}"


def _row(position: dict, live_price: float, state: str) -> str:
    tk = position["ticker"]
    entry = float(position.get("entry_price") or 0)
    shares = float(position.get("shares") or 0)
    peak = float(position.get("peak_gain_pct") or 0)
    stop = float(position.get("stop_price") or 0)

    move = (live_price - entry) / entry * 100 if entry else 0
    pnl  = (live_price - entry) * shares

    return (
        f"{tk:<5} {entry:>7.2f}  {live_price:>7.2f}  {move:+5.1f}%  "
        f"{peak:+5.1f}%  {stop:>7.2f}  {_fmt_money(pnl):>8}  {state}"
    )


def build_book_table(positions: list, live_prices: dict, market_state: str,
                     sizing_mode: str, header_label: str = "",
                     stopped_tickers: set | None = None) -> tuple:
    """
    Returns (text, rows_with_state) for the consolidated book post.

    rows_with_state: list of (position, live_price, state) tuples — caller
    feeds this to build_action_block().
    """
    stopped = stopped_tickers or set()
    rows_with_state: list = []
    total_pnl = 0.0

    for p in positions:
        tk = p["ticker"]
        live = float(live_prices.get(tk) or p.get("entry_price") or 0)
        state = compute_state(p, live, stopped_this_run=tk in stopped)
        rows_with_state.append((p, live, state))
        entry = float(p.get("entry_price") or 0)
        shares = float(p.get("shares") or 0)
        if entry > 0:
            total_pnl += (live - entry) * shares

    header_lines = [
        f"📊 POSITION BOOK — {header_label}".rstrip(" —"),
        f"Market: {market_state} · Sizing: {sizing_mode.upper()} · Open P/L: {_fmt_money(total_pnl)}",
        "",
        f"{'TK':<5} {'Avg':>7}  {'Now':>7}  {'Move':>5}  {'Peak%':>5}  {'Stop':>7}  {'$P/L':>8}  STATE",
    ]
    body_lines = [_row(p, lp, st) for p, lp, st in rows_with_state]

    text = "```\n" + "\n".join(header_lines + body_lines) + "\n```"
    return text, rows_with_state


def build_action_block(rows_with_state: list) -> str:
    """Top-of-message actions block. Picks STOPPED / STOP NEAR / ROUND-TRIP /
    TRIM rows and renders a 1-line plain-language command for each.

    Returns empty string when no actionable rows.
    """
    severity = {
        STATE_STOPPED:    0,
        STATE_STOP_NEAR:  1,
        STATE_ROUND_TRIP: 2,
        STATE_TRIM:       3,
    }
    actionable = [
        (sev, p, lp, st)
        for sev, (p, lp, st) in (
            (severity.get(st, 99), (p, lp, st)) for p, lp, st in rows_with_state
        )
        if sev < 99
    ]
    if not actionable:
        return ""
    actionable.sort(key=lambda t: t[0])

    lines = ["🚨 ACTIONS TODAY"]
    for _, p, lp, st in actionable:
        tk = p["ticker"]
        entry = float(p.get("entry_price") or 0)
        peak  = float(p.get("peak_gain_pct") or 0)
        stop  = float(p.get("stop_price") or 0)
        cur_pct = (lp - entry) / entry * 100 if entry else 0
        give    = peak - cur_pct
        if st == STATE_STOPPED:
            lines.append(f"  • {tk}: STOPPED — confirm exit, log result")
        elif st == STATE_STOP_NEAR:
            lines.append(f"  • {tk}: stop ${stop:.2f} ≈ price ${lp:.2f} — likely fires today")
        elif st == STATE_ROUND_TRIP:
            lines.append(f"  • {tk}: round-tripped peak +{peak:.1f}% → +{cur_pct:.1f}% (gave back {give:.1f}pp) — cut half")
        elif st == STATE_TRIM:
            lines.append(f"  • {tk}: trim — peak +{peak:.1f}%, gave back {give:.1f}pp, T1 already hit")
    return "\n".join(lines)


_SLACK_EMOJI_RE = re.compile(r":[a-z0-9_+\-]+:")
_UNICODE_EMOJI_RE = re.compile(
    "[" "\U0001f300-\U0001faff" "☀-➿" "]+",
)
_WS_RE = re.compile(r"\s+")

# Section ordering: most-actionable first.
_SECTION_ORDER = [
    ("stops",      "🔻 Stops"),
    ("warn",       "⚠ Warn / Peel"),
    ("target2",    "🎯🎯 Target 2"),
    ("target1",    "🎯 Target 1"),
    ("new",        "🟢 New positions"),
    ("avg_up",     "🟡 Avg up"),
    ("partial",    "🟠 Partial sell"),
    ("trail",      "🪙 Breakeven / Trail / Fade"),
    ("retro",      "🔄 Retro-patched"),
    ("info",       "ℹ Other"),
]

_KIND_TO_SECTION = {
    "stop_hit":                "stops",
    "hard_stop":               "stops",
    "auto_closed":             "stops",
    "target1":                 "target1",
    "target2":                 "target2",
    "auto_added":              "new",
    "share_drift_avg_up":      "avg_up",
    "share_drift_partial_sell":"partial",
    "breakeven":               "trail",
    "trailing_stop":           "trail",
    "fade":                    "trail",
    "ma_trail":                "trail",
    "sizing_mode":             "info",
}


def _short_ts(ts: str) -> str:
    """Trim ISO ts to HH:MM. Empty string when ts is missing or unparseable."""
    if not ts:
        return ""
    m = re.search(r"T(\d{2}:\d{2})", ts)
    return m.group(1) if m else ""


def _clean_message(msg: str) -> str:
    """Strip Slack emoji codes + unicode emoji, collapse newlines to ' · '."""
    s = msg.replace("\n", " · ")
    s = _SLACK_EMOJI_RE.sub("", s)
    s = _UNICODE_EMOJI_RE.sub("", s)
    s = _WS_RE.sub(" ", s).strip(" ·-")
    return s.strip()


def _classify_event(ev: dict) -> str:
    kind = ev.get("kind") or ""
    alert_type = (ev.get("alert_type") or "").upper()
    msg_upper = (ev.get("message") or "").upper()
    if kind in _KIND_TO_SECTION:
        return _KIND_TO_SECTION[kind]
    if "WARN_STOP" in alert_type or "PEEL_WARN" in alert_type:
        return "warn"
    if "RETRO-PATCHED" in msg_upper or "RETRO_PATCH" in msg_upper:
        return "retro"
    if "WARN_STOP" in msg_upper or "PEEL WARN" in msg_upper:
        return "warn"
    return "info"


def _format_event(ev: dict) -> str:
    ticker = ev.get("ticker") or "—"
    clean = _clean_message(ev.get("message") or ev.get("kind") or "")
    # Drop a leading "TICKER:" / "TICKER —" if it duplicates the ticker we
    # already have, so the bullet reads cleanly.
    clean = re.sub(rf"^{re.escape(ticker)}\s*[:\-—]\s*", "", clean)
    short_ts = _short_ts(ev.get("ts") or "")
    ts_part = f"  [{short_ts}]" if short_ts else ""
    if ticker and ticker != "—" and ticker.upper() not in clean.upper():
        return f"  • {ticker} — {clean}{ts_part}"
    return f"  • {clean}{ts_part}"


def build_events_digest(events_since_last: list) -> str:
    """Render inter-post events as bullets grouped into severity-ordered
    sections. Empty string when no events.
    """
    if not events_since_last:
        return ""

    buckets: dict[str, list[str]] = {}
    for ev in events_since_last:
        section = _classify_event(ev)
        buckets.setdefault(section, []).append(_format_event(ev))

    lines = ["📋 EVENTS SINCE LAST POST"]
    for key, header in _SECTION_ORDER:
        if key in buckets:
            lines.append(header)
            lines.extend(buckets[key])
    return "\n".join(lines)


# --- digest log persistence -----------------------------------------------

def load_digest_log(path: str) -> dict:
    if not os.path.exists(path):
        return {"last_book_post_ts": "", "events_since_last": []}
    try:
        with open(path) as f:
            data = json.load(f)
        data.setdefault("events_since_last", [])
        data.setdefault("last_book_post_ts", "")
        return data
    except (json.JSONDecodeError, OSError):
        return {"last_book_post_ts": "", "events_since_last": []}


def save_digest_log(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def append_digest_event(log: dict, event: dict) -> None:
    log.setdefault("events_since_last", []).append(event)


def clear_digest_log(log: dict, post_ts: str) -> None:
    log["last_book_post_ts"] = post_ts
    log["events_since_last"] = []


# --- composition ----------------------------------------------------------

def compose_book_message(positions: list, live_prices: dict, market_state: str,
                         sizing_mode: str, events_since_last: list,
                         header_label: str = "",
                         stopped_tickers: set | None = None) -> str:
    """Full Slack-ready text body. Caller wraps in `{"text": ...}` payload."""
    table, rows = build_book_table(
        positions, live_prices, market_state, sizing_mode,
        header_label=header_label, stopped_tickers=stopped_tickers,
    )
    actions = build_action_block(rows)
    digest  = build_events_digest(events_since_last)

    parts = [table]
    if actions:
        parts.append(actions)
    if digest:
        parts.append(digest)
    return "\n\n".join(parts)
