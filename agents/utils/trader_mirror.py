"""Trader Mirror — monthly You-vs-System scorecard, dollar-quantified.

Spec: docs/specs/trader-mirror.md

For each MANUAL-BOOK closed trade in the scored month (positions.json
closed_positions + real fills in data/position_history.json), replay the
shared rules engine (agents/trading/rules.apply_position_rules) day-by-day
over daily bars from actual entry to close + 10 sessions, and compare the
system's exit against the user's actual exit:

    delta_usd = (system_exit - actual_exit) x shares sold
    (positive means the system would have kept more)

Leak classification (mutually exclusive, first match wins):
    hold_in_hope  — actual exit below the rules stop that fired >= 2
                    sessions earlier (system said out; user held on)
    round_trip    — peak >= +15% during the hold, exited < +5%
    early_exit    — exited while the rules engine was still long AND price
                    went >= +5% higher within 10 sessions
    disciplined   — none of the above (credit it explicitly)

Honesty constraints (spec §2.4):
  - Causal replay: the engine sees only bars up to each session; the stop
    checked on day t is the stop that was armed entering day t.
  - System exit fills at the CLOSE of the signal day (conservative — no
    intraday fill optimism, no look-ahead).
  - The replay starts the session AFTER the entry date: the entry-day high
    and low may predate the actual intraday purchase, so neither the trail
    nor the stop may consume them.
  - Initial stop = the manual book's dynamic-stop formula (5% base +
    0.5 x ATR%) at entry — the stop Layer 1 would have armed on day one.
  - Trades without recoverable fills or bars are listed as `unscored`,
    never guessed.

Pure/testable core: build_mirror_summary takes an injectable bars_fetcher
(no network in tests). fetch_daily_bars is the only network function and
always passes an explicit `start` (CLAUDE.md 2026-07-10 Alpaca rule).

Called monthly (first Saturday, today.day <= 7) from finviz_weekly_agent —
the hook is non-fatal by contract: any failure here must never break the
weekly run.
"""

from __future__ import annotations

import datetime
import json
import logging
import os

import requests

from agents.trading import rules
from utils.pnl_walk import compute_pnl_from_events

log = logging.getLogger(__name__)

# --- Tunables (spec §2.2) ---------------------------------------------------

HOLD_IN_HOPE_MIN_EXTRA_SESSIONS = 2   # sessions held past the system stop signal
ROUND_TRIP_PEAK_MIN_PCT = 15.0        # peak gain qualifying a round-trip
ROUND_TRIP_EXIT_MAX_PCT = 5.0         # exit gain below this = round-tripped
EARLY_EXIT_UPSIDE_PCT = 5.0           # post-exit upside qualifying early_exit
EARLY_EXIT_LOOKAHEAD_SESSIONS = 10    # sessions after actual exit to look
REPLAY_TAIL_SESSIONS = 10             # replay horizon past the actual close
NEUTRAL_BAND_USD = 200.0              # |month total| under this = "a wash"
INITIAL_STOP_BASE_PCT = 5.0           # manual-book dynamic stop: 5% + 0.5xATR%
EPISODE_MATCH_TOLERANCE_DAYS = 5      # fill-episode close vs positions.json close_date

LEAK_ORDER = ("hold_in_hope", "round_trip", "early_exit", "disciplined")
LEAK_LABELS = {
    "hold_in_hope": "hold-in-hope",
    "round_trip": "round-trip",
    "early_exit": "early-exit",
    "disciplined": "disciplined",
}


# --- Month window -------------------------------------------------------------

def month_window(today: datetime.date) -> tuple:
    """(label 'YYYY-MM', start_iso, end_iso) for the month BEFORE `today`.

    The hook fires on the first Saturday of a month (day <= 7) and scores the
    month that just ended.
    """
    first_of_this = today.replace(day=1)
    last_prev = first_of_this - datetime.timedelta(days=1)
    start = last_prev.replace(day=1)
    return start.strftime("%Y-%m"), start.isoformat(), last_prev.isoformat()


# --- Bars ---------------------------------------------------------------------

def normalize_bars(raw_bars: list) -> list:
    """Alpaca raw bars → [{date, open, high, low, close}] oldest-first."""
    out = []
    for b in raw_bars or []:
        t = str(b.get("t", "") or "")[:10]
        if not t or b.get("c") is None:
            continue
        out.append({
            "date": t,
            "open": float(b.get("o") or 0),
            "high": float(b.get("h") or 0),
            "low": float(b.get("l") or 0),
            "close": float(b.get("c") or 0),
        })
    return out


def fetch_daily_bars(ticker: str, start_iso: str, end_iso: str | None = None) -> list:
    """Fetch daily bars from Alpaca with an EXPLICIT start (mandatory —
    CLAUDE.md 2026-07-10: no-start defaults to today and returns bars: null).

    Returns normalized bars, [] on any failure (caller marks trade unscored).
    """
    key = os.environ.get("ALPACA_API_KEY", "")
    sec = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not sec:
        log.warning("Trader Mirror: ALPACA_API_KEY/SECRET missing — cannot fetch bars")
        return []
    params = {"timeframe": "1Day", "start": start_iso, "limit": 10000,
              "feed": "iex", "adjustment": "raw"}
    today_iso = datetime.date.today().isoformat()
    if end_iso and end_iso < today_iso:
        params["end"] = end_iso
    try:
        resp = requests.get(
            "https://data.alpaca.markets/v2/stocks/" + ticker + "/bars",
            params=params,
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec},
            timeout=15,
        )
        if not resp.ok:
            return []
        return normalize_bars(resp.json().get("bars", []) or [])
    except Exception as e:
        log.warning("Trader Mirror: bars fetch failed for %s: %s", ticker, e)
        return []


def wilder_atr_pct_series(bars: list) -> list:
    """Wilder-smoothed ATR(14) as a %-of-close series (None until seeded).

    Matches utils/calibrate_peel.wilder_atr semantics — same seed, same
    smoothing — expressed as ATR% so it feeds apply_position_rules directly.
    """
    n = len(bars)
    out = [None] * n
    if n < 14:
        return out
    trs = []
    for i, b in enumerate(bars):
        if i == 0:
            tr = b["high"] - b["low"]
        else:
            prev_close = bars[i - 1]["close"]
            tr = max(b["high"] - b["low"], abs(b["high"] - prev_close),
                     abs(b["low"] - prev_close))
        trs.append(tr)
    atr = sum(trs[:14]) / 14.0
    if bars[13]["close"] > 0:
        out[13] = atr / bars[13]["close"] * 100.0
    for i in range(14, n):
        atr = (atr * 13.0 + trs[i]) / 14.0
        if bars[i]["close"] > 0:
            out[i] = atr / bars[i]["close"] * 100.0
    return out


def _bar_index_for_date(bars: list, date_iso: str, direction: str = "on_or_after") -> int | None:
    """Index of the bar on date_iso; 'on_or_after' → first bar >= date,
    'on_or_before' → last bar <= date. None when out of range."""
    if direction == "on_or_after":
        for i, b in enumerate(bars):
            if b["date"] >= date_iso:
                return i
        return None
    idx = None
    for i, b in enumerate(bars):
        if b["date"] <= date_iso:
            idx = i
        else:
            break
    return idx


def _sessions_between(bars: list, from_date: str, to_date: str) -> int:
    """Trading sessions strictly after from_date, up to and incl. to_date."""
    return sum(1 for b in bars if from_date < b["date"] <= to_date)


# --- Counterfactual replay ------------------------------------------------------

def replay_system_exit(bars: list, entry_date: str, entry_price: float,
                       ticker: str = "?", target1: float = 0.0,
                       target2: float = 0.0) -> dict | None:
    """Day-by-day causal replay of rules.apply_position_rules from entry.

    Per session after the entry day:
      1. Check the day's LOW against the stop armed entering the day —
         breach = system exit at that day's CLOSE (fill after the signal,
         conservative, no look-ahead).
      2. Otherwise feed the day's close/high into apply_position_rules so
         the trail/floors ratchet exactly as the live engine would have.

    Returns None when the entry bar can't be located or no sessions follow
    (caller marks the trade unscored). Otherwise:
      {exit_kind: "stop"|"open", exit_date, exit_price, stop_at_signal,
       entry_index, sessions_replayed}
    exit_kind "open" = engine still long at the end of the bars (the
    close+10-session horizon) — exit_price is that final close.
    """
    if entry_price <= 0 or not bars:
        return None
    entry_idx = _bar_index_for_date(bars, entry_date, "on_or_after")
    if entry_idx is None or entry_idx >= len(bars) - 1:
        return None

    atr_pcts = wilder_atr_pct_series(bars)
    atr_entry = atr_pcts[entry_idx] or 0.0
    initial_stop = round(
        entry_price * (1 - (INITIAL_STOP_BASE_PCT + 0.5 * atr_entry) / 100.0), 2)
    if not target1 or not target2:
        target1, target2 = rules.compute_targets(entry_price, atr_entry)

    state = {
        "entry_price": entry_price,
        "stop_price": initial_stop,
        "atr_pct": atr_entry,
        "entry_date": entry_date,
        "highest_price_seen": entry_price,
        "peak_gain_pct": 0.0,
        "breakeven_activated": False,
        "target1": target1, "target1_hit": False,
        "target2": target2, "target2_hit": False,
    }

    sessions = 0
    last_close = bars[entry_idx]["close"]
    last_date = bars[entry_idx]["date"]
    for i in range(entry_idx + 1, len(bars)):
        b = bars[i]
        sessions += 1
        stop_armed = float(state.get("stop_price") or 0)
        if stop_armed > 0 and b["low"] <= stop_armed:
            return {
                "exit_kind": "stop",
                "exit_date": b["date"],
                "exit_price": round(b["close"], 2),
                "stop_at_signal": round(stop_armed, 2),
                "entry_index": entry_idx,
                "sessions_replayed": sessions,
            }
        atr_i = atr_pcts[i] if atr_pcts[i] is not None else atr_entry
        rules.apply_position_rules(ticker, state, b["close"], b["high"], atr_i)
        last_close, last_date = b["close"], b["date"]

    return {
        "exit_kind": "open",
        "exit_date": last_date,
        "exit_price": round(last_close, 2),
        "stop_at_signal": round(float(state.get("stop_price") or 0), 2),
        "entry_index": entry_idx,
        "sessions_replayed": sessions,
    }


# --- Fill extraction (position_history.json) -----------------------------------

def _event_date(ev: dict) -> str:
    return str(ev.get("date", "") or "")[:10]


def extract_trade_events(events: list, close_date: str,
                         tolerance_days: int = EPISODE_MATCH_TOLERANCE_DAYS) -> list | None:
    """Isolate the fill episode for one round trip from a ticker's flat
    BUY/SELL event list (position_history keeps ONE list per ticker across
    re-entries).

    Episodes are segmented by share zero-crossings (0 → long → 0). The
    returned episode is the one whose final SELL lands within
    `tolerance_days` of the positions.json close_date. None = no
    recoverable fills for this trade (caller marks unscored).
    """
    if not events or not close_date:
        return None
    try:
        close_d = datetime.date.fromisoformat(close_date[:10])
    except ValueError:
        return None

    episodes = []
    current: list = []
    running = 0.0
    for ev in sorted(events, key=_event_date):
        action = ev.get("action", "")
        sh = float(ev.get("shares", 0) or 0)
        if action not in ("BUY", "SELL") or sh <= 0:
            continue
        current.append(ev)
        if action == "BUY":
            running += sh
        else:
            running = max(0.0, running - sh)
            if running <= 1e-6:
                episodes.append(current)
                current = []
    if current:
        episodes.append(current)  # still-open tail (broker-lag partial data)

    best, best_gap = None, None
    for ep in episodes:
        sell_dates = [_event_date(ev) for ev in ep if ev.get("action") == "SELL"]
        if not sell_dates:
            continue
        try:
            last_sell = datetime.date.fromisoformat(max(sell_dates))
        except ValueError:
            continue
        gap = abs((last_sell - close_d).days)
        if gap <= tolerance_days and (best_gap is None or gap < best_gap):
            best, best_gap = ep, gap
    return best


# --- Per-trade scoring ------------------------------------------------------------

def _fmt_usd(x: float) -> str:
    sign = "-" if x < 0 else ""
    return sign + "$" + format(abs(round(x)), ",.0f")


def classify_trade(entry_price: float, actual_exit_price: float,
                   actual_exit_date: str, shares_sold: float,
                   replay: dict, bars: list, delta_usd: float) -> tuple:
    """(leak, note) per spec §2.2 — mutually exclusive, first match wins.

    Notes are plain-language and dollar-quantified per the directional-
    guidance memory.
    """
    exit_gain_pct = (actual_exit_price - entry_price) / entry_price * 100 \
        if entry_price > 0 else 0.0

    # Peak during the actual hold — sessions after entry through the actual
    # exit day (entry-day high excluded: it can predate the purchase).
    entry_idx = replay["entry_index"]
    exit_idx = _bar_index_for_date(bars, actual_exit_date, "on_or_before")
    peak_price = entry_price
    if exit_idx is not None:
        for b in bars[entry_idx + 1: exit_idx + 1]:
            peak_price = max(peak_price, b["high"])
    peak_gain_pct = (peak_price - entry_price) / entry_price * 100 \
        if entry_price > 0 else 0.0

    # 1. hold_in_hope — system stop fired >= 2 sessions before the actual
    #    exit AND the actual exit landed below that stop.
    if replay["exit_kind"] == "stop":
        extra = _sessions_between(bars, replay["exit_date"], actual_exit_date)
        if (extra >= HOLD_IN_HOPE_MIN_EXTRA_SESSIONS
                and actual_exit_price < replay["stop_at_signal"]):
            note = ("stop said out at $" + format(replay["stop_at_signal"], ".2f")
                    + " on " + replay["exit_date"] + "; actual exit $"
                    + format(actual_exit_price, ".2f") + " — held "
                    + str(extra) + " extra days, cost " + _fmt_usd(max(delta_usd, 0.0)))
            return "hold_in_hope", note

    # 2. round_trip — peaked >= +15% during the hold, exited < +5%.
    if peak_gain_pct >= ROUND_TRIP_PEAK_MIN_PCT and exit_gain_pct < ROUND_TRIP_EXIT_MAX_PCT:
        gave_back = (peak_price - actual_exit_price) * shares_sold
        note = ("peaked +" + format(peak_gain_pct, ".1f") + "%, exited "
                + ("+" if exit_gain_pct >= 0 else "") + format(exit_gain_pct, ".1f")
                + "% — gave back " + _fmt_usd(gave_back) + " from the peak")
        return "round_trip", note

    # 3. early_exit — system still long at the actual exit AND price went
    #    >= +5% above the exit within the next 10 sessions (closes — the
    #    same fill-at-close honesty as the replay).
    system_still_long = (replay["exit_kind"] == "open"
                         or replay["exit_date"] > actual_exit_date)
    if system_still_long and exit_idx is not None:
        forward = bars[exit_idx + 1: exit_idx + 1 + EARLY_EXIT_LOOKAHEAD_SESSIONS]
        max_fwd_close = max((b["close"] for b in forward), default=0.0)
        if actual_exit_price > 0 and max_fwd_close >= actual_exit_price * (1 + EARLY_EXIT_UPSIDE_PCT / 100.0):
            note = ("sold " + _fmt_usd(max(delta_usd, 0.0)) + " early — system exit was "
                    + replay["exit_date"] + " at $" + format(replay["exit_price"], ".2f"))
            return "early_exit", note

    # 4. disciplined — credit it explicitly.
    if delta_usd <= 0:
        note = "beat the system by " + _fmt_usd(-delta_usd) + " — good exit"
    else:
        note = "matched the system within " + _fmt_usd(delta_usd)
    return "disciplined", note


def score_trade(pos: dict, events: list | None, bars_fetcher) -> dict:
    """Score one closed manual-book trade. Never guesses: any missing input
    (fills, bars, entry bar) returns status='unscored' with a reason."""
    ticker = str(pos.get("ticker", "?") or "?")
    entry_price = float(pos.get("entry_price") or 0)
    entry_date = str(pos.get("entry_date") or "")[:10]
    close_date = str(pos.get("close_date") or "")[:10]
    base = {"ticker": ticker, "entry_price": entry_price, "entry_date": entry_date,
            "close_date": close_date, "status": "unscored", "leak": None,
            "delta_usd": 0.0, "note": ""}

    if entry_price <= 0 or not entry_date or not close_date:
        base["note"] = "incomplete close record"
        return base

    episode = extract_trade_events(events or [], close_date)
    if not episode:
        base["note"] = "no recoverable fills in position_history"
        return base
    walk = compute_pnl_from_events(episode)
    shares_sold = walk["total_sold_units"]
    if shares_sold <= 0 or walk["proceeds_sold"] <= 0:
        base["note"] = "no SELL fills recoverable"
        return base
    actual_avg_exit = walk["proceeds_sold"] / shares_sold
    sell_dates = [_event_date(ev) for ev in episode if ev.get("action") == "SELL"]
    actual_exit_date = max(sell_dates)

    # Bars: entry - 90 calendar days (ATR runway) → close + 25 calendar days
    # (>= 10 sessions of replay tail + early-exit lookahead).
    try:
        start = (datetime.date.fromisoformat(entry_date)
                 - datetime.timedelta(days=90)).isoformat()
        end = (datetime.date.fromisoformat(close_date)
               + datetime.timedelta(days=25)).isoformat()
    except ValueError:
        base["note"] = "unparseable dates"
        return base
    bars = bars_fetcher(ticker, start, end)
    if not bars:
        base["note"] = "no daily bars available"
        return base

    replay = replay_system_exit(
        bars, entry_date, entry_price, ticker=ticker,
        target1=float(pos.get("target1") or 0), target2=float(pos.get("target2") or 0))
    if replay is None:
        base["note"] = "entry bar not found in bars"
        return base

    delta_usd = replay["exit_price"] * shares_sold - walk["proceeds_sold"]
    leak, note = classify_trade(entry_price, actual_avg_exit, actual_exit_date,
                                shares_sold, replay, bars, delta_usd)
    base.update({
        "status": "scored",
        "shares_sold": round(shares_sold, 4),
        "actual_exit_price": round(actual_avg_exit, 2),
        "actual_exit_date": actual_exit_date,
        "system_exit_price": replay["exit_price"],
        "system_exit_date": replay["exit_date"],
        "system_exit_kind": replay["exit_kind"],
        "delta_usd": round(delta_usd, 2),
        "leak": leak,
        "note": note,
    })
    return base


# --- Month summary ----------------------------------------------------------------

def build_mirror_summary(closed_positions: list, history: dict,
                         start_iso: str, end_iso: str, bars_fetcher,
                         month_label: str = "") -> dict:
    """Score every manual-book trade closed in [start_iso, end_iso]."""
    in_window = [p for p in closed_positions or []
                 if start_iso <= str(p.get("close_date") or "")[:10] <= end_iso]
    trades = []
    for pos in in_window:
        ticker = str(pos.get("ticker", "") or "")
        trades.append(score_trade(pos, (history or {}).get(ticker), bars_fetcher))

    buckets = {}
    for leak in LEAK_ORDER:
        rows = [t for t in trades if t["status"] == "scored" and t["leak"] == leak]
        buckets[leak] = {
            "count": len(rows),
            "delta_usd": round(sum(t["delta_usd"] for t in rows), 2),
            "tickers": [t["ticker"] for t in rows],
        }
    scored = [t for t in trades if t["status"] == "scored"]
    unscored = [t for t in trades if t["status"] == "unscored"]
    total_left = round(sum(buckets[k]["delta_usd"]
                           for k in ("hold_in_hope", "round_trip", "early_exit")), 2)
    return {
        "month": month_label,
        "window": {"start": start_iso, "end": end_iso},
        "generated": datetime.date.today().isoformat(),
        "trades": trades,
        "buckets": buckets,
        "total_left_usd": total_left,
        "scored_count": len(scored),
        "unscored_count": len(unscored),
        "disciplined_count": buckets["disciplined"]["count"],
    }


# --- Renderers ---------------------------------------------------------------------

def _month_name(label: str) -> str:
    try:
        return datetime.date.fromisoformat(label + "-01").strftime("%B")
    except ValueError:
        return label


def render_mirror_slack(summary: dict, prior_summaries: list | None = None,
                        html_url: str = "") -> str:
    """5-line max, verdict-first Slack text per spec §2.3. Neutral months are
    stated plainly — no manufactured drama."""
    month = _month_name(summary.get("month", ""))
    total = summary.get("total_left_usd", 0.0)
    b = summary.get("buckets", {})
    scored = summary.get("scored_count", 0)
    disciplined = summary.get("disciplined_count", 0)
    unscored = summary.get("unscored_count", 0)

    if scored == 0:
        lines = ["\U0001fa9e Trader Mirror — " + month + ": no scored trades this month."]
        if unscored:
            lines.append("unscored: " + str(unscored) + " (no recoverable fills or bars)")
        if html_url:
            lines.append("<" + html_url + "|Full mirror report>")
        return "\n".join(lines)

    if abs(total) < NEUTRAL_BAND_USD:
        verdict = ("system and you finished within "
                   + _fmt_usd(NEUTRAL_BAND_USD) + " — a wash.")
    elif total > 0:
        verdict = "you left " + _fmt_usd(total) + " on the table."
    else:
        verdict = "you beat the system by " + _fmt_usd(-total) + "."
    line1 = "\U0001fa9e Trader Mirror — " + month + ": " + verdict

    parts = []
    for leak in ("hold_in_hope", "round_trip", "early_exit"):
        info = b.get(leak, {})
        seg = LEAK_LABELS[leak] + " " + _fmt_usd(info.get("delta_usd", 0.0))
        tickers = info.get("tickers", [])
        if tickers:
            seg += " (" + ", ".join(tickers[:4]) + ")"
        parts.append(seg)
    line2 = " · ".join(parts)

    line3 = "disciplined: " + str(disciplined) + " of " + str(scored) + " trades"
    good = b.get("disciplined", {}).get("tickers", [])
    if good:
        line3 += " (" + ", ".join(good[:4]) + ")"
    # Comparative note only when real prior-month data exists — never invented.
    priors = [p for p in prior_summaries or [] if p.get("scored_count", 0) > 0]
    if priors and total < min(p.get("total_left_usd", 0.0) for p in priors):
        line3 += " — lowest leak in " + str(len(priors) + 1) + " months"
    if unscored:
        line3 += " · unscored: " + str(unscored)

    lines = [line1, line2, line3]
    if html_url:
        lines.append("<" + html_url + "|Full mirror report>")
    return "\n".join(lines)


_MIRROR_CSS = """
body { font-family: -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
       background: #f8fafc; color: #111827; margin: 0; padding: 24px; }
.wrap { max-width: 960px; margin: 0 auto; }
h1 { font-size: 1.4rem; margin: 0 0 4px; }
.sub { color: #6b7280; font-size: 0.85rem; margin-bottom: 18px; }
.verdict { background: #ffffff; border: 1px solid #e5e7eb; border-left: 4px solid #2563eb;
           border-radius: 10px; padding: 14px 18px; font-size: 1.05rem; font-weight: 600;
           margin-bottom: 18px; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px,1fr));
         gap: 12px; margin-bottom: 20px; }
.card { background: #ffffff; border: 1px solid #e5e7eb; border-radius: 10px;
        padding: 12px 14px; }
.card .label { font-size: 0.66rem; color: #9ca3af; text-transform: uppercase;
               letter-spacing: .06em; margin-bottom: 5px; }
.card .val { font-size: 1.2rem; font-weight: 700; }
.card .sub2 { font-size: 0.72rem; color: #6b7280; margin-top: 3px; }
table { border-collapse: collapse; width: 100%; background: #ffffff;
        border: 1px solid #e5e7eb; border-radius: 10px; overflow: hidden;
        font-size: 0.85rem; margin-bottom: 22px; }
th { background: #f3f4f6; text-align: left; padding: 8px 10px; font-size: 0.7rem;
     text-transform: uppercase; letter-spacing: .05em; color: #6b7280; }
td { padding: 8px 10px; border-top: 1px solid #f3f4f6; }
.pos { color: #b91c1c; } .neg { color: #15803d; }
.tag { display: inline-block; padding: 2px 8px; border-radius: 999px;
       font-size: 0.7rem; font-weight: 600; }
.tag-hold_in_hope { background: #fee2e2; color: #b91c1c; }
.tag-round_trip { background: #ffedd5; color: #c2410c; }
.tag-early_exit { background: #fef9c3; color: #a16207; }
.tag-disciplined { background: #dcfce7; color: #15803d; }
.tag-unscored { background: #f3f4f6; color: #6b7280; }
.note { color: #6b7280; font-size: 0.78rem; }
h2 { font-size: 1.0rem; margin: 20px 0 10px; }
"""


def render_mirror_html(summary: dict, prior_summaries: list | None = None) -> str:
    """Standalone light-theme page: verdict, bucket totals, per-trade table,
    3-month bucket trend."""
    month = summary.get("month", "")
    total = summary.get("total_left_usd", 0.0)
    b = summary.get("buckets", {})
    scored = summary.get("scored_count", 0)
    disciplined = summary.get("disciplined_count", 0)
    unscored = summary.get("unscored_count", 0)

    if scored == 0:
        verdict = "No scored trades in " + _month_name(month) + "."
    elif abs(total) < NEUTRAL_BAND_USD:
        verdict = ("System and you finished within " + _fmt_usd(NEUTRAL_BAND_USD)
                   + " in " + _month_name(month) + " — a wash.")
    elif total > 0:
        verdict = ("You left " + _fmt_usd(total) + " on the table in "
                   + _month_name(month) + ".")
    else:
        verdict = ("You beat the system by " + _fmt_usd(-total) + " in "
                   + _month_name(month) + ".")

    cards = []
    for leak in LEAK_ORDER:
        info = b.get(leak, {"count": 0, "delta_usd": 0.0, "tickers": []})
        tick = ", ".join(info.get("tickers", [])[:5]) or "—"
        cards.append(
            '<div class="card"><div class="label">' + LEAK_LABELS[leak]
            + '</div><div class="val">' + _fmt_usd(info.get("delta_usd", 0.0))
            + '</div><div class="sub2">' + str(info.get("count", 0))
            + " trade(s) · " + tick + "</div></div>")
    cards.append(
        '<div class="card"><div class="label">discipline rate</div><div class="val">'
        + str(disciplined) + " / " + str(scored)
        + '</div><div class="sub2">unscored: ' + str(unscored) + "</div></div>")

    rows = []
    for t in summary.get("trades", []):
        if t["status"] == "scored":
            d = t["delta_usd"]
            cls = "pos" if d > 0 else "neg"
            rows.append(
                "<tr><td><b>" + t["ticker"] + "</b></td>"
                + "<td>$" + format(t["entry_price"], ".2f") + " · " + t["entry_date"] + "</td>"
                + "<td>$" + format(t["actual_exit_price"], ".2f") + " · " + t["actual_exit_date"] + "</td>"
                + "<td>$" + format(t["system_exit_price"], ".2f") + " · " + t["system_exit_date"]
                + (" (still long)" if t["system_exit_kind"] == "open" else "") + "</td>"
                + '<td class="' + cls + '">' + _fmt_usd(d) + "</td>"
                + '<td><span class="tag tag-' + t["leak"] + '">'
                + LEAK_LABELS[t["leak"]] + "</span></td>"
                + '<td class="note">' + t["note"] + "</td></tr>")
        else:
            rows.append(
                "<tr><td><b>" + t["ticker"] + "</b></td>"
                + "<td>" + (t.get("entry_date") or "—") + "</td>"
                + "<td>" + (t.get("close_date") or "—") + "</td><td>—</td><td>—</td>"
                + '<td><span class="tag tag-unscored">unscored</span></td>'
                + '<td class="note">' + t["note"] + "</td></tr>")
    if not rows:
        rows.append('<tr><td colspan="7" class="note">No closed manual-book '
                    "trades in this window.</td></tr>")

    trend_rows = []
    trend_srcs = [summary] + list(prior_summaries or [])
    for s in trend_srcs[:3]:
        tb = s.get("buckets", {})
        cells = "".join(
            "<td>" + _fmt_usd(tb.get(k, {}).get("delta_usd", 0.0)) + " ("
            + str(tb.get(k, {}).get("count", 0)) + ")</td>"
            for k in LEAK_ORDER)
        trend_rows.append("<tr><td><b>" + s.get("month", "?") + "</b></td>" + cells
                          + "<td>" + _fmt_usd(s.get("total_left_usd", 0.0)) + "</td></tr>")
    if len(trend_srcs) < 2:
        trend_rows.append('<tr><td colspan="6" class="note">Prior months build '
                          "up as the mirror runs monthly.</td></tr>")

    return ("<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            "<title>Trader Mirror — " + month + "</title>\n<style>" + _MIRROR_CSS
            + "</style>\n</head>\n<body>\n<div class=\"wrap\">\n"
            "<h1>\U0001fa9e Trader Mirror — " + month + "</h1>\n"
            "<div class=\"sub\">You vs the rules engine — manual book, closed trades "
            + summary.get("window", {}).get("start", "") + " → "
            + summary.get("window", {}).get("end", "")
            + ". System exits fill at the close after the signal (causal replay, no look-ahead)."
            "</div>\n"
            "<div class=\"verdict\">" + verdict + "</div>\n"
            "<div class=\"cards\">" + "".join(cards) + "</div>\n"
            "<h2>Per-trade scorecard</h2>\n<table>\n<tr><th>Ticker</th><th>Entry</th>"
            "<th>Your exit</th><th>System exit</th><th>Delta $</th><th>Leak</th>"
            "<th>Detail</th></tr>\n" + "\n".join(rows) + "\n</table>\n"
            "<h2>3-month trend</h2>\n<table>\n<tr><th>Month</th><th>hold-in-hope</th>"
            "<th>round-trip</th><th>early-exit</th><th>disciplined</th><th>Total left</th></tr>\n"
            + "\n".join(trend_rows) + "\n</table>\n</div>\n</body>\n</html>\n")


# --- Orchestrator -------------------------------------------------------------------

def _load_prior_summaries(data_dir: str, month_label: str, n: int = 2) -> list:
    """Load up to n prior months' saved mirror JSONs (newest first). Months
    never computed simply don't appear — no back-filling, no guessing."""
    out = []
    try:
        d = datetime.date.fromisoformat(month_label + "-01")
    except ValueError:
        return out
    for _ in range(n):
        d = (d.replace(day=1) - datetime.timedelta(days=1)).replace(day=1)
        path = os.path.join(data_dir, "trader_mirror_" + d.strftime("%Y-%m") + ".json")
        try:
            with open(path) as f:
                out.append(json.load(f))
        except Exception:
            pass
    return out


def run_trader_mirror(data_dir: str = "data", slack_webhook: str = "",
                      pages_base: str = "", today: datetime.date | None = None,
                      bars_fetcher=None) -> dict | None:
    """Generate the monthly mirror: score last month's manual-book closes,
    write data/trader_mirror_YYYY-MM.{html,json}, post the 5-line Slack block.

    Returns the summary dict (None on missing inputs). Callers wrap this in
    try/except — a mirror failure must never break the weekly run."""
    today = today or datetime.date.today()
    month_label, start_iso, end_iso = month_window(today)
    fetcher = bars_fetcher or fetch_daily_bars

    with open(os.path.join(data_dir, "positions.json")) as f:
        closed = json.load(f).get("closed_positions", [])
    try:
        with open(os.path.join(data_dir, "position_history.json")) as f:
            history = json.load(f).get("history", {})
    except Exception as e:
        log.warning("Trader Mirror: position_history.json unavailable (%s) — "
                    "all trades will be unscored", e)
        history = {}

    summary = build_mirror_summary(closed, history, start_iso, end_iso,
                                   fetcher, month_label=month_label)
    prior = _load_prior_summaries(data_dir, month_label)

    html_name = "trader_mirror_" + month_label + ".html"
    html_path = os.path.join(data_dir, html_name)
    with open(html_path, "w") as f:
        f.write(render_mirror_html(summary, prior))
    json_path = os.path.join(data_dir, "trader_mirror_" + month_label + ".json")
    # Persisted JSON keeps totals + buckets + trades — next months' trend reads it.
    with open(json_path, "w") as f:
        json.dump(summary, f, indent=2)
    log.info("Trader Mirror: wrote %s (%d scored, %d unscored, total left %s)",
             html_path, summary["scored_count"], summary["unscored_count"],
             _fmt_usd(summary["total_left_usd"]))

    html_url = (pages_base.rstrip("/") + "/data/" + html_name) if pages_base else ""
    text = render_mirror_slack(summary, prior, html_url=html_url)
    if slack_webhook:
        try:
            resp = requests.post(slack_webhook, json={"text": text}, timeout=10)
            log.info("Trader Mirror: Slack post status %s", resp.status_code)
        except Exception as e:
            log.warning("Trader Mirror: Slack post failed (non-fatal): %s", e)
    else:
        log.info("Trader Mirror: no Slack webhook — skipping post.\n%s", text)
    return summary
