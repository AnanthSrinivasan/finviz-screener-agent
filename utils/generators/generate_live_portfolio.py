#!/usr/bin/env python3
"""
Live SnapTrade Portfolio — HTML dashboard for the real-money book.

Same dashboard as the paper page (utils/generators/generate_portfolio.py),
reading from SnapTrade instead of Alpaca. All shared layout/analytics live in
utils/generators/portfolio_common.py; this file is the SnapTrade adapter:

  • account header  ← SnapTrade balances
  • Open Positions  ← SnapTrade positions + Finviz technicals
  • Trade History + Month-over-Month  ← SnapTrade BUY/SELL activities cached in
    data/position_history.json (same FIFO engine the paper page uses)

Writes data/live_portfolio.html. Invoked from agents/trading/position_monitor.py
on every monitor run (3x daily book runs + every 30 min during market hours).

Light theme only (see memory/feedback_light_theme.md).
"""

import datetime
import json
import logging
import os
import re
import urllib.request

from utils.generators.portfolio_common import (
    fmt_money, fmt_pct, heat_class, held_days, verdict_for, classify_action,
    closed_trades, trade_stats, monthly_realized, render_stat_cards,
    render_positions_section, render_trade_history, page_shell,
)

log = logging.getLogger(__name__)

DATA_DIR    = os.environ.get("DATA_DIR", "data")
OUTPUT_PATH = os.path.join(DATA_DIR, "live_portfolio.html")

# Backward-compatible aliases (tests + callers import these names from here).
_fmt_money = fmt_money
_fmt_pct = fmt_pct
_heat_class = heat_class
_held_days = held_days


# ---------- Finviz live quote (no API key — html parse) ----------

_QUOTE_RE = re.compile(r'class="quote-price[^"]*"[^>]*>([\d,.]+)')


def fetch_live_price(ticker: str) -> float:
    try:
        req = urllib.request.Request(
            f"https://finviz.com/quote.ashx?t={ticker}",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        html = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", errors="ignore")
        m = _QUOTE_RE.search(html)
        return float(m.group(1).replace(",", "")) if m else 0.0
    except Exception as e:
        log.warning("Live quote fetch failed for %s: %s", ticker, e)
        return 0.0


# ---------- snapshot row build ----------

def _technicals(ticker: str) -> dict:
    """Pull ATR%, SMA20%, stage from Finviz once per ticker. Empty on failure."""
    try:
        from agents.screener.finviz_agent import get_snapshot_metrics, compute_stage
    except Exception as e:
        log.warning("Cannot import finviz_agent: %s", e)
        return {}
    m = get_snapshot_metrics(ticker)
    if not m or m[0] is None:
        return {}
    try:
        atr, eps, sales, dist52, rvol, av, s20, s50, s200, eq, io, it, pm, pq, ph, py = m
    except Exception:
        return {}
    try:
        si = compute_stage({"SMA20%": s20, "SMA50%": s50, "SMA200%": s200})
        stage = f"{si['stage']}{'P' if si['perfect'] else ''}"
    except Exception:
        stage = "?"
    return {
        "atr": float(atr or 0), "s20": float(s20 or 0),
        "s50": float(s50 or 0), "dist52": float(dist52 or 0),
        "rvol": float(rvol or 0), "stage": stage,
    }


def build_row(pos: dict, tech_lookup=None, entry_date: str = None,
              stop_price: float = None) -> dict:
    ticker = pos["ticker"]
    shares = float(pos.get("shares", 0) or 0)
    avg    = float(pos.get("avg_cost", 0) or 0)
    live = fetch_live_price(ticker) or float(pos.get("current_price", 0) or 0)
    if live <= 0:
        live = avg  # last resort so the row still renders
    gain   = ((live - avg) / avg * 100) if avg > 0 else 0
    pl     = (live - avg) * shares
    mv     = live * shares
    tech = (tech_lookup(ticker) if tech_lookup else _technicals(ticker)) or {}
    return {
        "ticker": ticker, "shares": shares, "avg": avg, "live": live,
        "gain": gain, "pl": pl, "mv": mv,
        "entry_date": entry_date or "—",
        "held":       held_days(entry_date),
        "atr":    tech.get("atr", 0.0),
        "s20":    tech.get("s20", 0.0),
        "stage":  tech.get("stage", "?"),
        "stop":   stop_price,
    }


# ---------- HTML ----------

def render_html(account: dict, rows: list, trades: list = None) -> str:
    trades = trades or []
    equity = float(account.get("equity", 0) or 0)
    cash   = float(account.get("cash", 0) or 0)
    bp     = float(account.get("buying_power", 0) or 0)
    total_mv = sum(r["mv"] for r in rows)
    total_pl = sum(r["pl"] for r in rows)
    leverage_pct = ((-cash) / equity * 100) if (equity > 0 and cash < 0) else 0.0
    total_unr_pct = (total_pl / (total_mv - total_pl) * 100) if (total_mv - total_pl) > 0 else 0.0

    stats = trade_stats(trades)
    realized_sign = "+" if stats["net"] >= 0 else ""

    cards = [
        {"label": "Equity", "value": fmt_money(equity),
         "sub": f"Buying power {fmt_money(bp)}"},
        {"label": "Cash", "value": fmt_money(cash),
         "sub": "margin debt" if cash < 0 else "free cash"},
        {"label": "Position MV", "value": fmt_money(total_mv),
         "sub": f"{len(rows)} positions"},
        {"label": "Open P&amp;L",
         "value": f"{'+' if total_pl >= 0 else ''}{fmt_money(total_pl)} ({fmt_pct(total_unr_pct)})",
         "sub": "across all positions", "heat": total_unr_pct},
        {"label": "Realized P&amp;L",
         "value": f"{realized_sign}{fmt_money(stats['net'])}",
         "sub": f"{stats['count']} trades: {stats['wins']}W / {stats['losses']}L · {stats['win_rate']:.0f}% win"
                if stats["count"] else "no closed trades", "heat": stats["net"]},
        {"label": "Leverage", "value": f"{leverage_pct:.0f}%", "sub": "debt / equity"},
    ]

    months = monthly_realized(trades)
    mo_labels = json.dumps([m["month"] for m in months])
    mo_pnl    = json.dumps([m["pnl"] for m in months])
    mo_colors = json.dumps(["#16a34a" if m["pnl"] >= 0 else "#dc2626" for m in months])
    mo_keys   = json.dumps([m["key"] for m in months])

    # Monthly realized P&L table (no equity walk — SnapTrade has no equity series)
    mo_rows = ""
    for m in reversed(months):
        h = heat_class(m["pnl"])
        ps = "+" if m["pnl"] >= 0 else ""
        mo_rows += (
            f"<tr class='month-row' onclick=\"filterMonth('{m['key']}')\" "
            f"title='Click to show only {m['month']} trades'>"
            f"<td class='bold'>{m['month']}</td>"
            f"<td class='mono'>{m['count']}</td>"
            f"<td class='mono heat {h}'>{ps}{fmt_money(m['pnl'])}</td>"
            "</tr>"
        )
    monthly_table = (
        "<table class='pos-table' style='margin-top:16px'><thead><tr><th>Month</th>"
        "<th>Trades</th><th>Realized P&amp;L</th>"
        "</tr></thead><tbody>" + mo_rows + "</tbody></table>"
    ) if months else ""

    chart_block = (
        "<div class='chart-card' style='margin-top:28px'>"
        "<h2 style='margin-top:0'>Monthly Realized P&amp;L</h2>"
        "<div class='chart-wrap' style='height:240px'><canvas id='mochart'></canvas></div>"
        + monthly_table + "</div>" if months else ""
    )

    body = (
        render_stat_cards(cards)
        + chart_block
        + "<h2>Open Positions — sorted action-first</h2>"
        + render_positions_section(rows, equity)
        + "<h2>Trade History (closed, from SnapTrade)</h2>"
        + render_trade_history(trades)
        + "<div class='footer'>Data: SnapTrade (balances · positions · activities) "
          "+ Finviz (live quotes &amp; technicals) · refreshes on every position "
          "monitor run.</div>"
    )

    updated = datetime.datetime.now(datetime.timezone.utc).strftime("%d %b %Y %H:%M UTC")
    extra_head = '<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>'
    extra_script = f"""
const moLabels = {mo_labels};
const moPnl    = {mo_pnl};
const moColors = {mo_colors};
const moKeys   = {mo_keys};
const moCanvas = document.getElementById('mochart');
if (moCanvas && moLabels.length > 0) {{
  new Chart(moCanvas.getContext('2d'), {{
    type: 'bar',
    data: {{ labels: moLabels, datasets: [{{ label: 'Realized P&L', data: moPnl, backgroundColor: moColors }}] }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      onClick: function(evt, els) {{ if (els && els.length) {{ filterMonth(moKeys[els[0].index]); }} }},
      plugins: {{ legend: {{ display: false }} }},
      scales: {{
        x: {{ grid: {{ display: false }}, ticks: {{ color: '#6b7280' }} }},
        y: {{ grid: {{ color: '#f3f4f6' }}, ticks: {{ color: '#6b7280',
               callback: function(v){{ return '$' + v.toLocaleString(); }} }} }}
      }}
    }}
  }});
}}
"""
    return page_shell("Live SnapTrade Portfolio", "📈 Live SnapTrade Portfolio",
                      f"Real-money book · SnapTrade + Finviz · refreshed {updated}",
                      body, extra_head=extra_head, extra_script=extra_script)


# ---------- placeholder + main ----------

def _placeholder_html(reason: str) -> str:
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%d %b %Y %H:%M UTC")
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Live Portfolio — refresh failed</title>
<style>body {{ font-family: -apple-system, sans-serif; background:#f8f9fc; color:#111827; padding:48px; max-width:800px; margin:0 auto; }}
.note {{ background:#fff; border:1px solid #e5e7eb; border-radius:10px; padding:20px; }}
</style></head><body>
<h1>📈 Live SnapTrade Portfolio</h1>
<div class="note"><strong>Last refresh failed</strong> at {ts}<br/>{reason}</div>
</body></html>"""


def _fetch_account_balances() -> dict:
    """Aggregate equity / cash / buying_power across SnapTrade accounts."""
    try:
        from agents.trading.position_monitor import snaptrade_get
    except Exception as e:
        log.warning("position_monitor import failed: %s", e)
        return {}
    accounts = snaptrade_get("/accounts") or []
    equity = cash = bp = 0.0
    for a in accounts:
        bal_obj = a.get("balance") or {}
        total = bal_obj.get("total") if isinstance(bal_obj, dict) else None
        if isinstance(total, dict):
            equity += float(total.get("amount") or 0)
        else:
            equity += float(bal_obj.get("amount", 0) or 0) if isinstance(bal_obj, dict) else 0
        bals = snaptrade_get(f"/accounts/{a.get('id')}/balances") or []
        if isinstance(bals, list):
            for b in bals:
                cash += float(b.get("cash", 0) or 0)
                bp   += float(b.get("buying_power", 0) or 0)
    return {"equity": equity, "cash": cash, "buying_power": bp}


def _load_entry_dates() -> dict:
    """ticker -> entry_date from the rules-engine book (positions.json)."""
    try:
        with open(os.path.join(DATA_DIR, "positions.json")) as f:
            d = json.load(f)
        return {p["ticker"]: p.get("entry_date")
                for p in d.get("open_positions", []) if p.get("ticker")}
    except Exception as e:
        log.warning("entry-date lookup failed: %s", e)
        return {}


def _load_live_stops() -> dict:
    """ticker -> stop_price from rules-engine (positions.json open_positions)."""
    try:
        with open(os.path.join(DATA_DIR, "positions.json")) as f:
            d = json.load(f)
        return {p["ticker"]: p.get("stop_price")
                for p in d.get("open_positions", [])
                if p.get("ticker") and p.get("stop_price")}
    except Exception as e:
        log.warning("live stops lookup failed: %s", e)
        return {}


def _load_live_events() -> list:
    """Flatten the cached SnapTrade BUY/SELL activities (position_history.json)
    into the common event schema for the FIFO trade engine."""
    try:
        with open(os.path.join(DATA_DIR, "position_history.json")) as f:
            d = json.load(f)
    except Exception as e:
        log.warning("position_history load failed: %s", e)
        return []
    history = d.get("history", d) if isinstance(d, dict) else {}
    events = []
    for ticker, acts in (history.items() if isinstance(history, dict) else []):
        for a in (acts or []):
            events.append({
                "symbol": ticker,
                "side": (a.get("action") or "").lower(),
                "qty": a.get("shares", 0),
                "price": a.get("price", 0),
                "date": (a.get("date") or "")[:10],
            })
    return events


def write_page() -> str | None:
    """Fetch live data, render HTML, write to OUTPUT_PATH. Non-fatal."""
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        from agents.trading.position_monitor import fetch_positions
    except Exception as e:
        log.warning("position_monitor import failed: %s", e)
        with open(OUTPUT_PATH, "w") as f:
            f.write(_placeholder_html(f"position_monitor import failed: {e}"))
        return OUTPUT_PATH
    try:
        positions = fetch_positions() or []
        account   = _fetch_account_balances()
        entry_dates = _load_entry_dates()
        live_stops = _load_live_stops()
        rows = [build_row(p, entry_date=entry_dates.get(p["ticker"]),
                          stop_price=live_stops.get(p["ticker"]))
                for p in positions]
        trades = closed_trades(_load_live_events())
        html = render_html(account, rows, trades=trades)
    except Exception as e:
        log.warning("Live portfolio render failed: %s", e)
        html = _placeholder_html(str(e))
    with open(OUTPUT_PATH, "w") as f:
        f.write(html)
    log.info("live_portfolio.html written → %s", OUTPUT_PATH)
    return OUTPUT_PATH


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    write_page()
