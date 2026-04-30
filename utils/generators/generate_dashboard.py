#!/usr/bin/env python3
# ----------------------------
# Dashboard Generator
# ----------------------------
# Reads JSON state files from data/ and generates dashboard.html
# showing positions, market state, watchlist, alerts, and trading state.
# Run alongside generate_index.py in workflows.
# ----------------------------

import os
import json
import glob
import datetime
import logging
import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DATA_DIR = os.environ.get("DATA_DIR", "data")
GITHUB_PAGES_BASE = os.environ.get("GITHUB_PAGES_BASE", "")
OUTPUT_PATH = "dashboard.html"


def _load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        log.warning(f"Could not load {path}: {e}")
        return default


def _format_date(date_str):
    try:
        d = datetime.date.fromisoformat(date_str)
        return d.strftime("%a %d %b")
    except Exception:
        return date_str


def _format_currency(val):
    if val >= 0:
        return f"${val:,.2f}"
    return f"-${abs(val):,.2f}"


def _format_pct(val):
    sign = "+" if val >= 0 else ""
    return f"{sign}{val:.1f}%"


def _pnl_class(val):
    if val > 0:
        return "pos"
    elif val < 0:
        return "neg"
    return "flat"


def _state_class(state):
    return {
        "GREEN": "state-green",
        "THRUST": "state-green",
        "CAUTION": "state-caution",
        "DANGER": "state-danger",
        "RED": "state-red",
        "BLACKOUT": "state-red",
    }.get(state, "state-red")


def _sizing_class(mode):
    return {
        "normal": "sizing-normal",
        "aggressive": "sizing-aggressive",
        "reduced": "sizing-reduced",
        "suspended": "sizing-suspended",
    }.get(mode, "sizing-normal")


def _fg_label(score):
    if score <= 25:
        return "Extreme Fear"
    elif score <= 45:
        return "Fear"
    elif score <= 55:
        return "Neutral"
    elif score <= 75:
        return "Greed"
    return "Extreme Greed"


def _watchlist_status_class(status):
    return {
        "watching": "ws-watching",
        "entered": "ws-entered",
        "stopped": "ws-stopped",
        "removed": "ws-removed",
    }.get(status, "ws-watching")


INDEX_TICKERS = ["SPY", "QQQ", "IWM", "TNA"]
_FINVIZ_BASE = "https://finviz.com"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36")


def _fetch_index_tiles() -> dict:
    """Fetch price, day-change, week-change, and SMA50% for each index ticker.
    Non-fatal — returns {} on any error so the dashboard still renders."""
    results = {}
    try:
        session = requests.Session()
        session.headers.update({"User-Agent": _UA})
        for ticker in INDEX_TICKERS:
            try:
                resp = session.get(
                    f"{_FINVIZ_BASE}/quote.ashx",
                    params={"t": ticker},
                    timeout=10,
                )
                if not resp.ok:
                    continue
                soup = BeautifulSoup(resp.content, "html.parser")
                table = soup.find("table", class_="snapshot-table2")
                if not table:
                    continue
                raw = {}
                for row in table.find_all("tr"):
                    cells = row.find_all("td")
                    for k, v in zip(cells[0::2], cells[1::2]):
                        raw[k.get_text(strip=True).rstrip(".")] = v.get_text(strip=True)

                def _pct(key):
                    try:
                        return round(float(raw.get(key, "").replace("%", "").replace(",", "")), 2)
                    except (ValueError, TypeError):
                        return None

                def _price(key):
                    try:
                        return round(float(raw.get(key, "").replace(",", "")), 2)
                    except (ValueError, TypeError):
                        return None

                results[ticker] = {
                    "price":      _price("Price"),
                    "change_pct": _pct("Change"),
                    "week_pct":   _pct("Perf Week"),
                    "sma50_pct":  _pct("SMA50"),
                }
                log.info(f"Index tile {ticker}: {results[ticker]}")
            except Exception as e:
                log.warning(f"Index tile {ticker} failed: {e}")
    except Exception as e:
        log.warning(f"_fetch_index_tiles session failed: {e}")
    return results


def _build_index_tiles_html(tiles: dict) -> str:
    if not tiles:
        return ""

    def _tile(ticker):
        d = tiles.get(ticker)
        if not d or d.get("price") is None:
            return f'<div class="idx-tile"><div class="idx-name">{ticker}</div><div class="idx-price">—</div></div>'

        price     = d["price"]
        chg       = d.get("change_pct")
        week      = d.get("week_pct")
        sma50     = d.get("sma50_pct")

        chg_str   = (f'<span class="idx-chg {_pnl_class(chg)}">{_format_pct(chg)}</span>'
                     if chg is not None else "")
        week_str  = (f'<span class="idx-week {_pnl_class(week)}">Wk {_format_pct(week)}</span>'
                     if week is not None else "")
        sma50_str = ""
        if sma50 is not None:
            label = "Above 50d" if sma50 >= 0 else "Below 50d"
            sma50_str = (f'<span class="idx-ma {_pnl_class(sma50)}">'
                         f'{_format_pct(sma50)} {label}</span>')

        return f"""<div class="idx-tile">
          <div class="idx-name">{ticker}</div>
          <div class="idx-price">${price:,.2f} {chg_str}</div>
          <div class="idx-sub">{week_str} {sma50_str}</div>
        </div>"""

    inner = "".join(_tile(t) for t in INDEX_TICKERS)
    return f'<div class="idx-row">{inner}</div>'


def load_data(data_dir):
    """Load all data files needed for the dashboard."""
    positions = _load_json(os.path.join(data_dir, "positions.json"), {"open_positions": [], "closed_positions": []})
    trading_state = _load_json(os.path.join(data_dir, "trading_state.json"), {})
    watchlist = _load_json(os.path.join(data_dir, "watchlist.json"), {"watchlist": []})
    alerts_state = _load_json(os.path.join(data_dir, "alerts_state.json"), {})
    market_history = _load_json(os.path.join(data_dir, "market_monitor_history.json"), [])
    peel_calib = _load_json(os.path.join(data_dir, "peel_calibration.json"), {})
    position_history = _load_json(os.path.join(data_dir, "position_history.json"), {"history": {}})
    recent_events = _load_json(os.path.join(data_dir, "recent_events.json"), {"events": []})

    # Latest market monitor snapshot
    monitor_files = sorted(glob.glob(os.path.join(data_dir, "market_monitor_2*.json")), reverse=True)
    market_latest = _load_json(monitor_files[0], {}) if monitor_files else {}

    # Position snapshots (newest first)
    snap_files = sorted(glob.glob(os.path.join(data_dir, "positions_2*.json")), reverse=True)
    position_snapshots = []
    for f in snap_files[:7]:
        snap = _load_json(f)
        if snap:
            position_snapshots.append(snap)

    return {
        "positions": positions,
        "trading_state": trading_state,
        "watchlist": watchlist,
        "alerts_state": alerts_state,
        "market_latest": market_latest,
        "market_history": market_history,
        "position_snapshots": position_snapshots,
        "peel_calib": peel_calib,
        "position_history": position_history.get("history", {}),
        "recent_events": recent_events,
    }


_PEEL_TIER_FALLBACK = [(4, 3.0, 4.0), (7, 5.0, 6.0), (10, 6.5, 8.0), (999, 8.5, 10.0)]


def _peel_status(ticker, atr_pct, calib):
    """Return (mult_str, p90_str, status_label, css_class) from calibration data.
    atr_pct is the position's stored ATR% (from peel_calibration or fallback).
    mult is stored in calib as current ATR multiple — we only have static calib data here,
    so we show the thresholds for context without live mult calculation.
    """
    c = calib.get(ticker, {})
    if c.get("calibrated"):
        warn  = c.get("warn", 0)
        sig   = c.get("signal", 0)
        p90   = c.get("p90", 0)
        mx    = c.get("max_seen", 0)
        atr_avg = c.get("atr_pct_avg", atr_pct or 8)
    else:
        atr_avg = c.get("atr_pct_avg", atr_pct or 8)
        for threshold, w, s in _PEEL_TIER_FALLBACK:
            if atr_avg <= threshold:
                warn, sig, p90, mx = w, s, 0, 0
                break
        else:
            warn, sig, p90, mx = 8.5, 10.0, 0, 0

    calibrated = c.get("calibrated", False)
    src = "" if calibrated else " ~"
    p90_str = f"{p90:.1f}x" if p90 else "—"
    mx_str  = f"{mx:.1f}x"  if mx  else "—"
    thresholds = f"warn {warn:.1f}x · sig {sig:.1f}x · p90 {p90_str}{src}"
    return thresholds, p90_str, mx_str, "calibrated" if calibrated else "fallback"


def _format_history_date(s):
    """ISO datetime → 'DD MMM' compact."""
    if not s:
        return "—"
    try:
        from datetime import datetime as _dt
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%d"):
            try:
                d = _dt.strptime(s.replace("+00:00", "Z").rstrip("Z") + "Z", fmt)
                return d.strftime("%d %b").lstrip("0")
            except ValueError:
                continue
    except Exception:
        pass
    return s[:10]


SYSTEM_HISTORY_FLOOR = "2026-04-01"  # Don't show pre-system trades.


def _filter_history_for_position(events, entry_date=None, close_date=None):
    """Trim events to the current trade cycle: only those at or after entry_date
    (and not after close_date if given). Always respects the system-wide floor.
    """
    if not events:
        return []
    floor = max(entry_date or SYSTEM_HISTORY_FLOOR, SYSTEM_HISTORY_FLOOR)
    out = []
    for ev in events:
        d = (ev.get("date") or "")[:10]
        if d < floor:
            continue
        if close_date and d > close_date:
            continue
        out.append(ev)
    return out


def _build_history_subrow(ticker, events, colspan, closed_meta=None):
    """Render an expandable transaction timeline as a hidden child row.

    events: list of {date, action, shares, price} ascending.
    closed_meta: optional dict with {result_pct, close_source, close_price} for
                 closed positions — appended as a final summary line.
    """
    if not events:
        msg = f'<em class="history-empty">No SnapTrade activity found for {ticker} in last 90 days.</em>'
        return f'<tr class="history-row" data-ticker="{ticker}" hidden><td colspan="{colspan}">{msg}</td></tr>'

    # Walk forward computing running cost basis.
    running_shares = 0.0
    running_cost = 0.0
    body_rows = ""
    for ev in events:
        sh = float(ev.get("shares", 0))
        px = float(ev.get("price", 0))
        action = ev.get("action", "")
        if action == "BUY":
            running_cost += sh * px
            running_shares += sh
        elif action == "SELL":
            # Reduce running_cost proportionally so avg cost stays unchanged on partial sells.
            if running_shares > 0:
                avg = running_cost / running_shares
                running_cost -= sh * avg
                running_shares = max(0.0, running_shares - sh)
        avg_cost = (running_cost / running_shares) if running_shares > 0 else 0.0
        action_class = "buy" if action == "BUY" else "sell"
        avg_str = f"${avg_cost:.2f}" if running_shares > 0 else "—"
        body_rows += f"""
          <tr>
            <td>{_format_history_date(ev.get("date", ""))}</td>
            <td><span class="action-{action_class}">{action}</span></td>
            <td>{sh:g}</td>
            <td>${px:.2f}</td>
            <td>{avg_str}</td>
            <td>{running_shares:g}</td>
          </tr>"""

    summary_line = ""
    if closed_meta:
        rp = closed_meta.get("result_pct", 0)
        cls = "pos" if rp > 1 else ("neg" if rp < -1 else "neutral")
        verdict = "WIN" if rp > 1 else ("LOSS" if rp < -1 else "BREAKEVEN")
        src = closed_meta.get("close_source", "—")
        cp = closed_meta.get("close_price", 0)
        summary_line = f"""
          <div class="history-summary {cls}">
            <strong>Result:</strong> {rp:+.2f}% ({verdict}) at ${cp:.2f}
            · <span class="history-src">source: {src}</span>
          </div>"""

    return f"""
      <tr class="history-row" data-ticker="{ticker}" hidden>
        <td colspan="{colspan}">
          <table class="history-table">
            <thead>
              <tr>
                <th>Date</th><th>Action</th><th>Shares</th><th>Price</th>
                <th>Avg Cost</th><th>Running Shares</th>
              </tr>
            </thead>
            <tbody>{body_rows}</tbody>
          </table>
          {summary_line}
        </td>
      </tr>"""


def _build_positions_html(positions, peel_calib=None, position_history=None):
    open_pos = positions.get("open_positions", [])
    closed_pos = positions.get("closed_positions", [])
    if peel_calib is None:
        peel_calib = {}
    if position_history is None:
        position_history = {}

    if not open_pos:
        return '<div class="empty-state">No open positions</div>'

    total_cost = 0
    total_pnl = 0

    rows = ""
    for p in open_pos:
        shares = p.get("shares", 0)
        entry = p.get("entry_price", 0)          # current weighted avg (post avg-up)
        first_entry = p.get("first_entry_price", entry)  # original entry, fallback to entry
        gain_pct = p.get("current_gain_pct", 0)
        cost = shares * entry
        pnl = cost * (gain_pct / 100)
        total_cost += cost
        total_pnl += pnl
        stop = p.get("stop_price", 0)
        risk_pct = ((stop - entry) / entry * 100) if entry and stop else 0

        # Target progress
        t1 = p.get("target1", 0)
        t1_hit = p.get("target1_hit", False)
        t2 = p.get("target2", 0)

        target_html = ""
        if t1_hit:
            target_html = f'<span class="target-hit">T1 ${t1:.0f} hit</span>'
            if t2:
                target_html += f' <span class="target-pending">T2 ${t2:.0f}</span>'
        elif t1:
            target_html = f'<span class="target-pending">T1 ${t1:.0f}</span>'
            if t2:
                target_html += f' <span class="target-pending">T2 ${t2:.0f}</span>'

        be_stop = p.get("breakeven_activated", False)
        stop_label = f'${stop:.2f}' if stop else "—"
        if be_stop:
            stop_label += ' <span class="be-badge">BE</span>'

        # Peel thresholds from calibration
        ticker_name = p.get("ticker", "?")
        peel_thresholds, p90_str, mx_str, peel_src = _peel_status(ticker_name, None, peel_calib)
        peel_src_badge = '' if peel_src == 'calibrated' else ' <span class="peel-fallback">~</span>'
        peel_html = f'<span class="peel-thresholds">{peel_thresholds}</span>{peel_src_badge}'

        events = _filter_history_for_position(
            position_history.get(ticker_name, []),
            entry_date=p.get("entry_date"),
        )
        rows += f"""
        <tr class="position-row" data-ticker-row="{ticker_name}">
          <td class="ticker-cell">
            <button type="button" class="hist-toggle" data-target="{ticker_name}" aria-expanded="false">▸</button>
            <span class="ticker">{ticker_name}</span>
            <span class="entry-date">{_format_date(p.get("entry_date", ""))}</span>
          </td>
          <td>{shares}</td>
          <td>${first_entry:.2f}</td>
          <td>${entry:.2f}</td>
          <td>{stop_label}</td>
          <td class="{_pnl_class(gain_pct)}">{_format_pct(gain_pct)}</td>
          <td class="{_pnl_class(pnl)}">{_format_currency(pnl)}</td>
          <td class="risk-cell {_pnl_class(risk_pct)}">{_format_pct(risk_pct)}</td>
          <td class="targets-cell">{target_html}</td>
          <td class="peel-cell">{peel_html}</td>
        </tr>{_build_history_subrow(ticker_name, events, colspan=10)}"""

    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost else 0

    summary = f"""
    <div class="positions-summary">
      <div class="summary-item">
        <span class="summary-label">Exposure</span>
        <span class="summary-val">{_format_currency(total_cost)}</span>
      </div>
      <div class="summary-item">
        <span class="summary-label">Unrealized P&L</span>
        <span class="summary-val {_pnl_class(total_pnl)}">{_format_currency(total_pnl)} ({_format_pct(total_pnl_pct)})</span>
      </div>
      <div class="summary-item">
        <span class="summary-label">Positions</span>
        <span class="summary-val">{len(open_pos)} open</span>
      </div>
    </div>"""

    # --- Closed positions section ---
    closed_html = ""
    if closed_pos:
        closed_rows = ""
        for c in closed_pos[-12:][::-1]:  # last 12, newest first
            ck = c.get("ticker", "?")
            csh = c.get("shares", 0)
            cent = c.get("entry_price", 0)
            ccp = c.get("close_price", 0)
            cres = c.get("result_pct", 0)
            csrc = c.get("close_source", "—")
            cdate = _format_date(c.get("close_date", ""))
            verdict_cls = _pnl_class(cres)
            events = _filter_history_for_position(
                position_history.get(ck, []),
                entry_date=c.get("entry_date"),
                close_date=c.get("close_date"),
            )
            closed_rows += f"""
            <tr class="position-row closed-row" data-ticker-row="{ck}-c">
              <td class="ticker-cell">
                <button type="button" class="hist-toggle" data-target="{ck}-c" aria-expanded="false">▸</button>
                <span class="ticker">{ck}</span>
                <span class="entry-date">{cdate}</span>
              </td>
              <td>{csh:g}</td>
              <td>${cent:.2f}</td>
              <td>${ccp:.2f}</td>
              <td class="{verdict_cls}">{cres:+.2f}%</td>
              <td class="history-src">{csrc}</td>
            </tr>{_build_history_subrow(f"{ck}-c", events, colspan=6, closed_meta=c)}"""
        closed_html = f"""
        <h3 class="closed-heading">Closed Positions <span class="closed-count">({len(closed_pos)} total · last 12 shown)</span></h3>
        <div class="table-wrap">
        <table class="positions-table closed-table">
          <thead>
            <tr>
              <th>Ticker</th><th>Shares</th><th>Entry</th><th>Close</th><th>Result</th><th>Source</th>
            </tr>
          </thead>
          <tbody>{closed_rows}</tbody>
        </table>
        </div>"""

    return f"""{summary}
    <div class="table-wrap">
    <table class="positions-table">
      <thead>
        <tr>
          <th>Ticker</th><th>Shares</th><th>First Entry</th><th>Avg Price</th><th>Stop</th><th>Gain %</th><th>P&L</th><th>Risk</th><th>Targets</th><th>Peel (p90)</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
    </div>
    {closed_html}"""


def _build_market_html(market_latest, market_history):
    if not market_latest:
        return '<div class="empty-state">No market data</div>'

    state = market_latest.get("market_state", "RED")
    msg = market_latest.get("state_message", "")
    fg = market_latest.get("fg", 0)
    spy = market_latest.get("spy_price", 0)
    spy_pct = market_latest.get("spy_sma200_pct", 0)
    spy_above = market_latest.get("spy_above_200d", False)
    r5 = market_latest.get("ratio_5day", 0)
    r10 = market_latest.get("ratio_10day", 0)
    t2108 = market_latest.get("t2108_equiv") or 0
    thrust = market_latest.get("thrust_detected", False)
    blackout = market_latest.get("blackout", False)
    date = market_latest.get("date", "")

    # Build mini history for SPY + F&G
    spy_history = ""
    fg_history = ""
    if market_history:
        for h in market_history[-5:]:
            d = h.get("date", "")[-5:]  # MM-DD
            spy_history += f'<div class="hist-item"><span class="hist-date">{d}</span><span class="hist-val">${h.get("spy_price", 0):.0f}</span></div>'
            fgv = h.get("fg", 0)
            fg_history += f'<div class="hist-item"><span class="hist-date">{d}</span><span class="hist-val {_pnl_class(fgv - 50)}">{fgv:.0f}</span></div>'

    spy_badge = '<span class="badge badge-green">Above 200d</span>' if spy_above else '<span class="badge badge-red">Below 200d</span>'
    thrust_badge = ' <span class="badge badge-green">THRUST</span>' if thrust else ""
    blackout_badge = ' <span class="badge badge-red">BLACKOUT</span>' if blackout else ""

    return f"""
    <div class="market-header">
      <div class="market-state-badge {_state_class(state)}">{state}</div>
      <span class="market-msg">{msg}{blackout_badge}{thrust_badge}</span>
      <span class="market-date">{_format_date(date)}</span>
    </div>
    <div class="market-grid">
      <div class="market-card">
        <div class="mc-label">SPY</div>
        <div class="mc-val">${spy:.2f}</div>
        <div class="mc-sub {_pnl_class(spy_pct)}">{_format_pct(spy_pct)} from 200d {spy_badge}</div>
        <div class="hist-row">{spy_history}</div>
      </div>
      <div class="market-card">
        <div class="mc-label">Fear & Greed</div>
        <div class="mc-val {_pnl_class(fg - 50)}">{fg:.0f}</div>
        <div class="mc-sub">{_fg_label(fg)}</div>
        <div class="hist-row">{fg_history}</div>
      </div>
      <div class="market-card">
        <div class="mc-label">Breadth 5d / 10d</div>
        <div class="mc-val">{r5:.1f} / {r10:.1f}</div>
        <div class="mc-sub">Up/Down ratio</div>
      </div>
      <div class="market-card">
        <div class="mc-label">T2108 Equiv</div>
        <div class="mc-val">{t2108:.0f}%</div>
        <div class="mc-sub">% above 40d MA</div>
      </div>
    </div>"""


def _build_watchlist_html(watchlist):
    items = watchlist.get("watchlist", [])
    if not items:
        return '<div class="empty-state">Watchlist empty</div>'

    rows = ""
    for w in items:
        status = w.get("status", "watching")
        entry = w.get("entry_price")
        entry_str = f"${entry:.2f}" if entry else "—"
        stop = w.get("stop")
        stop_str = f"${stop:.2f}" if stop else "—"

        rows += f"""
        <tr>
          <td class="ticker-cell">
            <span class="ticker">{w.get("ticker", "?")}</span>
            <span class="entry-date">{_format_date(w.get("added", ""))}</span>
          </td>
          <td><span class="ws-badge {_watchlist_status_class(status)}">{status}</span></td>
          <td>{entry_str}</td>
          <td>{stop_str}</td>
          <td class="thesis-cell">{w.get("thesis", "")}</td>
        </tr>"""

    watching = sum(1 for w in items if w.get("status") == "watching")
    entered = sum(1 for w in items if w.get("status") == "entered")

    return f"""
    <div class="watchlist-summary">
      <span class="ws-count">{watching} watching</span>
      <span class="ws-count">{entered} entered</span>
    </div>
    <div class="table-wrap">
    <table class="watchlist-table">
      <thead><tr><th>Ticker</th><th>Status</th><th>Entry</th><th>Stop</th><th>Thesis</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
    </div>"""


def _build_alerts_html(alerts_state, recent_events=None):
    fng_hist = alerts_state.get("fng_history", [])
    slv_hist = alerts_state.get("slv_week_history", [])
    gld_hist = alerts_state.get("gld_week_history", [])
    last_alerts = alerts_state.get("last_alerts_sent", {})
    events = (recent_events or {}).get("events", []) or []

    if not fng_hist and not last_alerts and not events:
        return '<div class="empty-state">No alert data</div>'

    # Merge recent_events + last_alerts_sent into one timeline (events win on
    # category granularity; legacy last_alerts_sent fallback if no events).
    timeline_items = []
    for ev in events[-10:][::-1]:  # newest first, last 10
        timeline_items.append({
            "date": ev.get("date", ""),
            "label": ev.get("title", ev.get("category", "Event")),
            "severity": ev.get("severity", "med"),
        })
    if not timeline_items:
        for alert_type, date in sorted(last_alerts.items(), key=lambda x: x[1], reverse=True):
            timeline_items.append({
                "date": date,
                "label": alert_type.replace("_", " ").title(),
                "severity": "med",
            })

    timeline = ""
    for item in timeline_items:
        sev_cls = f' alert-{item["severity"]}'
        timeline += f"""
        <div class="alert-item{sev_cls}">
          <span class="alert-date">{_format_date(item["date"])}</span>
          <span class="alert-label">{item["label"]}</span>
        </div>"""

    # Build commodity trackers
    commodity_html = ""
    if slv_hist:
        latest_slv = slv_hist[-1]
        commodity_html += f"""
        <div class="commodity-card">
          <span class="comm-label">SLV 1w</span>
          <span class="comm-val {_pnl_class(latest_slv.get('pct', 0))}">{_format_pct(latest_slv.get('pct', 0))}</span>
        </div>"""
    if gld_hist:
        latest_gld = gld_hist[-1]
        commodity_html += f"""
        <div class="commodity-card">
          <span class="comm-label">GLD 1w</span>
          <span class="comm-val {_pnl_class(latest_gld.get('pct', 0))}">{_format_pct(latest_gld.get('pct', 0))}</span>
        </div>"""

    # F&G sparkline
    fg_spark = ""
    for item in fng_hist[-10:]:
        score = item.get("score", 0)
        height = max(4, int(score * 0.6))
        color = "#ef4444" if score <= 25 else "#f59e0b" if score <= 45 else "#22c55e"
        fg_spark += f'<div class="spark-bar" style="height:{height}px;background:{color}" title="{item.get("date","")}: {score}"></div>'

    return f"""
    <div class="alerts-layout">
      <div class="alerts-col">
        <div class="sub-label">Recent Alerts</div>
        <div class="alert-timeline">{timeline if timeline else '<span class="empty-state">No recent alerts</span>'}</div>
      </div>
      <div class="alerts-col">
        <div class="sub-label">Fear & Greed (15d)</div>
        <div class="spark-row">{fg_spark}</div>
        <div class="commodities-row">{commodity_html}</div>
      </div>
    </div>"""


def _build_trading_state_html(trading_state):
    if not trading_state:
        return '<div class="empty-state">No trading state</div>'

    mode = trading_state.get("current_sizing_mode", "normal")
    wins = trading_state.get("consecutive_wins", 0)
    losses = trading_state.get("consecutive_losses", 0)
    total_w = trading_state.get("total_wins", 0)
    total_l = trading_state.get("total_losses", 0)
    override = trading_state.get("sizing_override")
    updated = trading_state.get("last_updated", "")
    recent = trading_state.get("recent_trades", [])

    # Streak visualization
    streak_dots = ""
    for t in recent[-10:]:
        result = t.get("result", "")
        cls = "dot-win" if result == "win" else "dot-loss" if result == "loss" else "dot-flat"
        rp = t.get("result_pct", 0)
        usd = t.get("profit_loss_usd")
        if usd is not None and rp != 0:
            label = f" {usd:+,.0f} ({rp:+.1f}%)"
        elif rp != 0:
            label = f" {rp:+.1f}%"
        else:
            label = ""
        streak_dots += f'<span class="streak-dot {cls}" title="{t.get("ticker", "")} {result}{label}"></span>'

    override_html = f' <span class="override-badge">Override: {override}</span>' if override else ""

    # Sizing mode description
    mode_desc = {
        "normal": "Standard position sizing",
        "aggressive": "2+ wins + GREEN/THRUST — increased sizing",
        "reduced": "2 consecutive losses — max 5% position size",
        "suspended": "3+ consecutive losses — paper trade only",
    }.get(mode, "")

    return f"""
    <div class="ts-grid">
      <div class="ts-card ts-mode">
        <div class="ts-label">Sizing Mode</div>
        <div class="ts-badge {_sizing_class(mode)}">{mode.upper()}</div>
        <div class="ts-desc">{mode_desc}{override_html}</div>
      </div>
      <div class="ts-card">
        <div class="ts-label">Win Streak</div>
        <div class="ts-val pos">{wins}</div>
      </div>
      <div class="ts-card">
        <div class="ts-label">Loss Streak</div>
        <div class="ts-val neg">{losses}</div>
      </div>
      <div class="ts-card">
        <div class="ts-label">Total W / L</div>
        <div class="ts-val">{total_w} / {total_l}</div>
      </div>
    </div>
    <div class="streak-row">
      <span class="sub-label">Recent trades</span>
      <div class="streak-dots">{streak_dots if streak_dots else '<span class="empty-state">No trades yet</span>'}</div>
      <span class="ts-updated">Updated {_format_date(updated)}</span>
    </div>"""


def generate_dashboard(data, base_url):
    generated_at = datetime.datetime.now().strftime("%d %b %Y %H:%M UTC")
    market_state = data["market_latest"].get("market_state", "—")
    sizing_mode = data["trading_state"].get("current_sizing_mode", "normal")

    positions_html = _build_positions_html(
        data["positions"], data.get("peel_calib", {}),
        position_history=data.get("position_history", {}),
    )
    market_html = _build_market_html(data["market_latest"], data["market_history"])
    alerts_html = _build_alerts_html(data["alerts_state"], data.get("recent_events", {}))
    trading_html = _build_trading_state_html(data["trading_state"])
    index_tiles_html = _build_index_tiles_html(_fetch_index_tiles())

    # Watchlist summary counts only — full list lives at watchlist.html
    wl_items = data["watchlist"].get("watchlist", [])
    wl_focus   = sum(1 for w in wl_items if w.get("priority") == "focus" and w.get("status") != "archived")
    wl_watching = sum(1 for w in wl_items if w.get("priority") != "focus" and w.get("status") not in ("archived",))
    wl_url = f"{base_url}/watchlist.html" if base_url else "watchlist.html"
    watchlist_link_html = f"""
    <div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap;">
      <div style="display:flex;gap:20px;">
        <div><span style="font-size:1.4rem;font-weight:700;color:#92400e">{wl_focus}</span><br>
          <span style="font-size:0.7rem;color:#9ca3af;text-transform:uppercase">Focus</span></div>
        <div><span style="font-size:1.4rem;font-weight:700;color:#111827">{wl_watching}</span><br>
          <span style="font-size:0.7rem;color:#9ca3af;text-transform:uppercase">Watching</span></div>
      </div>
      <a href="{wl_url}" style="padding:8px 16px;background:#f0fdf4;color:#15803d;border:1px solid #bbf7d0;
         border-radius:8px;font-size:0.85rem;font-weight:600;text-decoration:none;">
        Open Watchlist →
      </a>
    </div>"""

    index_url = f"{base_url}/index.html" if base_url else "index.html"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>Dashboard — Finviz Screener Agent</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #f8f9fc; color: #111827; min-height: 100vh; }}
  a {{ color: #2563eb; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}

  /* Header */
  .dash-header {{ display: flex; align-items: center; gap: 16px; padding: 24px 32px 20px;
                   border-bottom: 1px solid #e5e7eb; flex-wrap: wrap; background: #fff; }}
  .dash-header h1 {{ font-size: 1.3rem; font-weight: 700; color: #111827; }}
  .dash-meta {{ font-size: 0.75rem; color: #9ca3af; margin-left: auto; }}
  .back-link {{ font-size: 0.82rem; }}

  /* Top badges row */
  .top-badges {{ display: flex; gap: 12px; padding: 16px 32px; border-bottom: 1px solid #e5e7eb;
                  flex-wrap: wrap; align-items: center; background: #fff; }}
  .market-state-pill {{ padding: 6px 16px; border-radius: 6px; font-size: 0.82rem; font-weight: 700;
                         letter-spacing: 0.06em; }}
  .state-green {{ background: #dcfce7; color: #15803d; }}
  .state-caution {{ background: #fef9c3; color: #a16207; }}
  .state-danger {{ background: #fee2e2; color: #b91c1c; }}
  .state-red {{ background: #fee2e2; color: #dc2626; }}
  .sizing-pill {{ padding: 6px 14px; border-radius: 6px; font-size: 0.78rem; font-weight: 600; }}
  .sizing-normal {{ background: #f1f5f9; color: #64748b; }}
  .sizing-aggressive {{ background: #dcfce7; color: #15803d; }}
  .sizing-reduced {{ background: #fef9c3; color: #a16207; }}
  .sizing-suspended {{ background: #fee2e2; color: #dc2626; }}

  /* Sections */
  .section {{ padding: 24px 32px; background: #fff; }}
  .section + .section {{ border-top: 1px solid #e5e7eb; }}
  .section-title {{ font-size: 0.72rem; font-weight: 700; color: #6b7280;
                     text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 16px; }}
  .sub-label {{ font-size: 0.68rem; font-weight: 700; color: #6b7280;
                text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 8px; }}

  /* Positions */
  .positions-summary {{ display: flex; gap: 28px; margin-bottom: 16px; flex-wrap: wrap; }}
  .summary-item {{ display: flex; flex-direction: column; gap: 2px; }}
  .summary-label {{ font-size: 0.68rem; color: #9ca3af; text-transform: uppercase; letter-spacing: 0.05em; }}
  .summary-val {{ font-size: 1.2rem; font-weight: 700; color: #111827; }}

  /* Index tiles (SPY / QQQ / IWM / TNA) */
  .idx-row {{ display: flex; gap: 10px; margin-top: 14px; flex-wrap: wrap; }}
  .idx-tile {{ flex: 1; min-width: 110px; background: #fff; border: 1px solid #e5e7eb;
               border-radius: 10px; padding: 12px 14px; }}
  .idx-name {{ font-size: 0.7rem; font-weight: 700; color: #6b7280;
               text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 4px; }}
  .idx-price {{ font-size: 1.05rem; font-weight: 700; color: #111827; display: flex;
                align-items: baseline; gap: 6px; flex-wrap: wrap; }}
  .idx-chg {{ font-size: 0.8rem; font-weight: 600; }}
  .idx-sub {{ margin-top: 4px; display: flex; gap: 8px; flex-wrap: wrap; }}
  .idx-week {{ font-size: 0.72rem; color: #6b7280; }}
  .idx-ma {{ font-size: 0.72rem; }}

  /* Tables */
  .table-wrap {{ overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; }}
  th {{ text-align: left; padding: 8px 12px; font-size: 0.68rem; color: #6b7280;
       text-transform: uppercase; letter-spacing: 0.06em; border-bottom: 2px solid #e5e7eb; font-weight: 700; }}
  td {{ padding: 10px 12px; border-bottom: 1px solid #f3f4f6; }}
  tr:hover td {{ background: #f9fafb; }}
  .ticker-cell {{ white-space: nowrap; }}
  .ticker {{ font-weight: 700; font-size: 0.88rem; color: #111827; }}
  .entry-date {{ display: block; font-size: 0.68rem; color: #9ca3af; }}
  .thesis-cell {{ font-size: 0.75rem; color: #6b7280; max-width: 280px; }}
  .targets-cell {{ font-size: 0.75rem; white-space: nowrap; }}
  .peel-cell {{ font-size: 0.72rem; color: #6b7280; white-space: nowrap; }}
  .peel-thresholds {{ color: #9ca3af; }}
  .peel-fallback {{ font-size: 0.65rem; color: #4b5563; margin-left: 3px; }}
  .risk-cell {{ font-size: 0.75rem; }}

  .pos {{ color: #16a34a; font-weight: 600; }}
  .neg {{ color: #dc2626; font-weight: 600; }}
  .flat {{ color: #9ca3af; }}

  .target-hit {{ color: #16a34a; font-weight: 600; }}
  .target-pending {{ color: #9ca3af; }}
  .be-badge {{ background: #dcfce7; color: #15803d; font-size: 0.62rem; padding: 1px 5px;
               border-radius: 3px; font-weight: 600; vertical-align: middle; }}

  /* Market grid */
  .market-header {{ display: flex; align-items: center; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }}
  .market-state-badge {{ padding: 8px 20px; border-radius: 8px; font-size: 1rem; font-weight: 800;
                          letter-spacing: 0.08em; }}
  .market-msg {{ font-size: 0.82rem; color: #6b7280; }}
  .market-date {{ font-size: 0.72rem; color: #9ca3af; margin-left: auto; }}
  .market-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }}
  .market-card {{ background: #f8f9fc; border: 1px solid #e5e7eb; border-radius: 10px; padding: 16px; }}
  .mc-label {{ font-size: 0.68rem; color: #9ca3af; text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 4px; }}
  .mc-val {{ font-size: 1.3rem; font-weight: 700; color: #111827; margin-bottom: 2px; }}
  .mc-sub {{ font-size: 0.72rem; color: #6b7280; margin-bottom: 8px; }}
  .hist-row {{ display: flex; gap: 8px; flex-wrap: wrap; }}
  .hist-item {{ display: flex; flex-direction: column; align-items: center; gap: 1px; }}
  .hist-date {{ font-size: 0.58rem; color: #9ca3af; }}
  .hist-val {{ font-size: 0.68rem; font-weight: 600; color: #6b7280; }}

  .badge {{ font-size: 0.62rem; padding: 2px 8px; border-radius: 4px; font-weight: 600; vertical-align: middle; }}
  .badge-green {{ background: #dcfce7; color: #15803d; }}
  .badge-red {{ background: #fee2e2; color: #dc2626; }}

  /* Watchlist */
  .watchlist-summary {{ display: flex; gap: 16px; margin-bottom: 12px; }}
  .ws-count {{ font-size: 0.78rem; color: #6b7280; }}
  .ws-badge {{ font-size: 0.68rem; padding: 2px 10px; border-radius: 4px; font-weight: 600; }}
  .ws-watching {{ background: #f1f5f9; color: #64748b; }}
  .ws-entered {{ background: #dcfce7; color: #15803d; }}
  .ws-stopped {{ background: #fee2e2; color: #dc2626; }}
  .ws-removed {{ background: #f3f4f6; color: #9ca3af; }}

  /* Alerts */
  .alerts-layout {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }}
  @media (max-width: 640px) {{ .alerts-layout {{ grid-template-columns: 1fr; }} }}
  .alerts-col {{ }}
  .alert-timeline {{ display: flex; flex-direction: column; gap: 8px; }}
  .alert-item {{ display: flex; align-items: center; gap: 10px; padding: 8px 12px;
                  background: #fffbeb; border-radius: 6px; border-left: 3px solid #d97706; }}
  .alert-date {{ font-size: 0.72rem; color: #9ca3af; white-space: nowrap; }}
  .alert-label {{ font-size: 0.78rem; font-weight: 600; color: #111827; }}

  .spark-row {{ display: flex; align-items: flex-end; gap: 3px; height: 40px; margin-bottom: 12px; }}
  .spark-bar {{ width: 18px; border-radius: 2px 2px 0 0; min-height: 4px; }}

  .commodities-row {{ display: flex; gap: 16px; }}
  .commodity-card {{ display: flex; align-items: center; gap: 8px; padding: 6px 12px;
                      background: #f8f9fc; border: 1px solid #e5e7eb; border-radius: 6px; }}
  .comm-label {{ font-size: 0.68rem; color: #9ca3af; font-weight: 600; }}
  .comm-val {{ font-size: 0.82rem; font-weight: 700; color: #111827; }}

  /* Trading State */
  .ts-grid {{ display: grid; grid-template-columns: 2fr 1fr 1fr 1fr; gap: 12px; margin-bottom: 16px; }}
  @media (max-width: 640px) {{ .ts-grid {{ grid-template-columns: 1fr 1fr; }} }}
  .ts-card {{ background: #f8f9fc; border: 1px solid #e5e7eb; border-radius: 10px; padding: 16px;
              display: flex; flex-direction: column; gap: 4px; }}
  .ts-label {{ font-size: 0.68rem; color: #9ca3af; text-transform: uppercase; letter-spacing: 0.06em; }}
  .ts-badge {{ font-size: 1.1rem; font-weight: 800; letter-spacing: 0.06em;
               padding: 4px 14px; border-radius: 6px; align-self: flex-start; }}
  .ts-val {{ font-size: 1.3rem; font-weight: 700; color: #111827; }}
  .ts-desc {{ font-size: 0.72rem; color: #6b7280; }}
  .override-badge {{ background: #fef9c3; color: #a16207; font-size: 0.62rem; padding: 2px 8px;
                      border-radius: 4px; font-weight: 600; }}

  .streak-row {{ display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }}
  .streak-dots {{ display: flex; gap: 4px; }}
  .streak-dot {{ width: 12px; height: 12px; border-radius: 50%; }}
  .dot-win {{ background: #16a34a; }}
  .dot-loss {{ background: #dc2626; }}
  .dot-flat {{ background: #9ca3af; }}
  .ts-updated {{ font-size: 0.68rem; color: #9ca3af; margin-left: auto; }}

  .empty-state {{ font-size: 0.82rem; color: #9ca3af; padding: 16px 0; }}

  /* Footer */
  .footer {{ padding: 20px 32px; border-top: 1px solid #e5e7eb;
             font-size: 0.72rem; color: #9ca3af; background: #fff; }}

  /* PDF export */
  .pdf-btn {{ position: fixed; bottom: 24px; right: 24px; background: #2563eb; color: #fff;
              border: none; border-radius: 50%; width: 44px; height: 44px; font-size: 1.1rem;
              cursor: pointer; z-index: 999; box-shadow: 0 2px 8px rgba(0,0,0,.2);
              display: flex; align-items: center; justify-content: center; }}
  .pdf-btn:hover {{ background: #1d4ed8; }}
  @media print {{
    .pdf-btn {{ display: none; }}
    .top-badges, .dash-header {{ break-inside: avoid; }}
    .positions-table tr {{ break-inside: avoid; }}
    .history-row {{ display: table-row !important; }}
    .history-row[hidden] {{ display: table-row !important; }}
  }}
  /* Expandable transaction history */
  .hist-toggle {{
    background: none; border: none; color: #2563eb; font-size: 13px;
    cursor: pointer; padding: 0 4px 0 0; transition: transform .15s ease;
    font-weight: bold;
  }}
  .hist-toggle[aria-expanded="true"] {{ transform: rotate(90deg); }}
  .history-row {{ background: #f9fafb; }}
  .history-row td {{ padding: 8px 16px !important; }}
  .history-table {{
    width: 100%; border-collapse: collapse; margin: 4px 0;
    font-size: 12px; background: #fff; border: 1px solid #e5e7eb; border-radius: 6px;
  }}
  .history-table thead th {{
    background: #f3f4f6; color: #374151; font-weight: 600; text-align: left;
    padding: 6px 10px; border-bottom: 1px solid #e5e7eb;
  }}
  .history-table tbody td {{ padding: 6px 10px; border-bottom: 1px solid #f3f4f6; }}
  .history-table tbody tr:last-child td {{ border-bottom: none; }}
  .action-buy {{ color: #16a34a; font-weight: 600; }}
  .action-sell {{ color: #dc2626; font-weight: 600; }}
  .history-summary {{
    margin-top: 8px; padding: 6px 10px; background: #fff; border-radius: 6px;
    border-left: 3px solid #6b7280; color: #111827; font-size: 12px;
  }}
  .history-summary.pos {{ border-left-color: #16a34a; }}
  .history-summary.neg {{ border-left-color: #dc2626; }}
  .history-summary.neutral {{ border-left-color: #6b7280; }}
  .history-empty {{ color: #6b7280; font-size: 12px; }}
  .history-src {{ color: #6b7280; font-size: 11px; font-family: ui-monospace, monospace; }}
  .closed-heading {{
    margin: 24px 0 8px 0; font-size: 14px; color: #374151;
    text-transform: uppercase; letter-spacing: 0.5px;
  }}
  .closed-count {{ color: #6b7280; font-weight: normal; text-transform: none; letter-spacing: 0; }}
  .closed-table {{ font-size: 13px; }}
  .alert-item.alert-high {{ border-left: 3px solid #dc2626; padding-left: 8px; }}
  .alert-item.alert-med  {{ border-left: 3px solid #f59e0b; padding-left: 8px; }}
  .alert-item.alert-low  {{ border-left: 3px solid #16a34a; padding-left: 8px; }}
</style>
</head>
<body>
<button class="pdf-btn" onclick="window.print()" title="Export PDF">⬇</button>

<div class="dash-header">
  <h1>Dashboard</h1>
  <a href="{index_url}" class="back-link">Reports Index</a>
  <div class="dash-meta">Updated {generated_at}</div>
</div>

<div class="top-badges">
  <div class="market-state-pill {_state_class(market_state)}">Market: {market_state}</div>
  <div class="sizing-pill {_sizing_class(sizing_mode)}">Sizing: {sizing_mode.upper()}</div>
</div>

<div class="section">
  <div class="section-title">Open Positions & P&L</div>
  {positions_html}
</div>

<div class="section">
  <div class="section-title">Market State</div>
  {market_html}
  {index_tiles_html}
</div>

<div class="section">
  <div class="section-title">Watchlist</div>
  {watchlist_link_html}
</div>

<div class="section">
  <div class="section-title">Alerts & Sentiment</div>
  {alerts_html}
</div>

<div class="section">
  <div class="section-title">Trading State</div>
  {trading_html}
</div>

<div class="footer">
  Generated {generated_at} &middot;
  <a href="https://github.com/AnanthSrinivasan/finviz-screener-agent"
     style="color:#334155">github.com/AnanthSrinivasan/finviz-screener-agent</a>
</div>

<script>
(function() {{
  document.querySelectorAll('.hist-toggle').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      var key = btn.getAttribute('data-target');
      var row = document.querySelector('.history-row[data-ticker="' + key + '"]');
      if (!row) return;
      var open = row.hasAttribute('hidden') ? false : true;
      if (open) {{ row.setAttribute('hidden', ''); btn.setAttribute('aria-expanded', 'false'); }}
      else      {{ row.removeAttribute('hidden');  btn.setAttribute('aria-expanded', 'true');  }}
    }});
  }});
}})();
</script>

</body>
</html>"""


def main():
    log.info("=== Dashboard generator starting ===")

    data = load_data(DATA_DIR)
    log.info(f"Loaded: {len(data['positions'].get('open_positions', []))} open positions, "
             f"{len(data['market_history'])} market history entries, "
             f"{len(data['watchlist'].get('watchlist', []))} watchlist items")

    base_url = GITHUB_PAGES_BASE.rstrip("/") if GITHUB_PAGES_BASE else ""
    html = generate_dashboard(data, base_url)

    with open(OUTPUT_PATH, "w") as f:
        f.write(html)

    log.info(f"dashboard.html written -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
