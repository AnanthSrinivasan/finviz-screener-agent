#!/usr/bin/env python3
"""
Claude Model Portfolio — HTML dashboard for the Alpaca paper account.

Same dashboard as the live page (utils/generators/generate_live_portfolio.py),
reading from Alpaca instead of SnapTrade. Shared layout/analytics live in
utils/generators/portfolio_common.py; this file is the Alpaca adapter:

  • account header + equity curve  ← Alpaca account + portfolio-history
  • Open Positions                 ← Alpaca positions + Finviz technicals
  • Trade History + Month-over-Month ← Alpaca FILL activities

Writes data/claude_portfolio.html. Invoked from agents/trading/alpaca_monitor.py
so it refreshes hourly during market hours.

Light theme only (see memory/feedback_light_theme.md).
"""

import datetime
import json
import logging
import os
import requests

from utils.generators.portfolio_common import (
    fmt_money, fmt_pct, heat_class, held_days, render_stat_cards,
    render_positions_section, render_trade_history, page_shell,
    closed_trades as _closed_trades_events,
    open_entry_dates as _open_entry_dates_events,
    trade_stats,
)

log = logging.getLogger(__name__)

ALPACA_API_KEY    = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2")
DATA_DIR          = os.environ.get("DATA_DIR", "data")
OUTPUT_PATH       = os.path.join(DATA_DIR, "claude_portfolio.html")

# Only show trades from 2026 — the account has older manual fills (2023) that
# predate the automated screener/executor and would pollute the trade log.
TRADES_SINCE = "2026-01-01"

# Backward-compatible aliases (tests + callers import these names from here).
_fmt_money = fmt_money
_fmt_pct = fmt_pct
_heat_class = heat_class


def _headers() -> dict:
    return {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }


def _get(path: str, params: dict = None) -> dict | list | None:
    try:
        r = requests.get(f"{ALPACA_BASE_URL}{path}", headers=_headers(),
                         params=params or {}, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("Alpaca GET %s failed: %s", path, e)
        return None


# ---------- Alpaca-specific analytics (equity-curve based) ----------

def inception_equity(history: dict) -> float:
    """First non-null positive equity point (account start). 0.0 if unusable."""
    if not history:
        return 0.0
    for eq in (history.get("equity") or []):
        if eq is None:
            continue
        try:
            val = float(eq)
        except (TypeError, ValueError):
            continue
        if val > 0:
            return val
    return 0.0


def compute_total_return(history: dict, current_equity: float) -> tuple:
    start = inception_equity(history)
    if start <= 0:
        return 0.0, 0.0, 0.0
    abs_ret = current_equity - start
    pct_ret = (abs_ret / start * 100) if start else 0.0
    return start, abs_ret, pct_ret


def build_equity_curve_js(history: dict) -> str:
    if not history:
        return "[]"
    timestamps = history.get("timestamp") or []
    equity     = history.get("equity") or []
    points = []
    for ts, eq in zip(timestamps, equity):
        if eq is None:
            continue
        date = datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).strftime("%Y-%m-%d")
        points.append({"x": date, "y": round(float(eq), 2)})
    return json.dumps(points)


def monthly_performance(history: dict) -> list:
    """Month-over-month equity change from the daily equity series.
    [{month, key, start, end, pnl, pct}] oldest→newest."""
    if not history:
        return []
    timestamps = history.get("timestamp") or []
    equity     = history.get("equity") or []
    series = []
    for ts, eq in zip(timestamps, equity):
        if eq is None:
            continue
        try:
            val = float(eq)
        except (TypeError, ValueError):
            continue
        dt = datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc)
        series.append((dt, val))
    if not series:
        return []

    month_end: dict[str, tuple] = {}
    order: list[str] = []
    first_equity = series[0][1]
    for dt, val in series:
        key = dt.strftime("%Y-%m")
        if key not in month_end:
            order.append(key)
        month_end[key] = (dt, val)

    out = []
    prev_end = first_equity
    for key in order:
        dt, end = month_end[key]
        start = prev_end
        pnl = end - start
        pct = (pnl / start * 100) if start else 0.0
        out.append({
            "month": dt.strftime("%b %Y"), "key": key,
            "start": round(start, 2), "end": round(end, 2),
            "pnl": round(pnl, 2), "pct": round(pct, 2),
        })
        prev_end = end
    return out


# ---------- Alpaca fills → common event schema ----------

def fetch_fills() -> list:
    """All FILL activities for the account, oldest→newest (paginated)."""
    fills = []
    token = None
    while True:
        params = {"activity_types": "FILL", "page_size": 100}
        if token:
            params["page_token"] = token
        batch = _get("/account/activities", params)
        if not batch or not isinstance(batch, list):
            break
        fills.extend(batch)
        token = batch[-1].get("id")
        if len(batch) < 100:
            break
    fills.sort(key=lambda f: f.get("transaction_time", ""))
    return fills


def _fills_to_events(fills: list) -> list:
    return [{"symbol": f.get("symbol"), "side": f.get("side"),
             "qty": f.get("qty"), "price": f.get("price"),
             "date": (f.get("transaction_time") or "")[:10]}
            for f in (fills or [])]


def closed_trades(fills: list, since: str = None) -> list:
    """FIFO round-trip trades from Alpaca FILL activities (adapter over the
    shared event-based engine so the paper + live pages share one code path)."""
    return _closed_trades_events(_fills_to_events(fills), since=since)


def open_entry_dates(fills: list) -> dict:
    """Earliest still-open FIFO lot date per symbol, from Alpaca fills."""
    return _open_entry_dates_events(_fills_to_events(fills))


# ---------- HTML ----------

def _alpaca_rows(positions: list, entry_dates: dict, technicals: dict) -> list:
    """Normalize Alpaca position dicts → the common row schema."""
    rows = []
    for p in positions:
        sym = p.get("symbol", "")
        gain = float(p.get("unrealized_plpc", 0) or 0) * 100
        edate = (entry_dates or {}).get(sym) or "—"
        tech = (technicals or {}).get(sym, {})
        rows.append({
            "ticker": sym,
            "shares": float(p.get("qty", 0) or 0),
            "avg":    float(p.get("avg_entry_price", 0) or 0),
            "live":   float(p.get("current_price", 0) or 0),
            "gain":   gain,
            "pl":     float(p.get("unrealized_pl", 0) or 0),
            "mv":     float(p.get("market_value", 0) or 0),
            "entry_date": edate,
            "held":   held_days(edate),
            "atr":    tech.get("atr", 0.0),
            "s20":    tech.get("s20"),
            "stage":  tech.get("stage", "—"),
        })
    return rows


def generate_html(account: dict, positions: list, history: dict,
                  trades: list = None, entry_dates: dict = None,
                  technicals: dict = None) -> str:
    trades = trades or []
    equity      = float(account.get("equity", 0) or 0)
    last_equity = float(account.get("last_equity", 0) or 0)
    cash        = float(account.get("cash", 0) or 0)
    bp          = float(account.get("buying_power", 0) or 0)
    day_pl      = equity - last_equity
    day_pl_pct  = (day_pl / last_equity * 100) if last_equity else 0

    total_unrealized = sum(float(p.get("unrealized_pl", 0) or 0) for p in positions)
    total_cost       = sum(float(p.get("cost_basis", 0) or 0)    for p in positions)
    total_unr_pct    = (total_unrealized / total_cost * 100) if total_cost else 0
    winners = sum(1 for p in positions if float(p.get("unrealized_pl", 0) or 0) > 0)
    losers  = sum(1 for p in positions if float(p.get("unrealized_pl", 0) or 0) < 0)

    start_equity, total_ret, total_ret_pct = compute_total_return(history, equity)

    cards = [
        {"label": "Equity", "value": fmt_money(equity),
         "sub": f"Buying power {fmt_money(bp)}"},
        {"label": "Total Return",
         "value": f"{'+' if total_ret >= 0 else ''}{fmt_money(total_ret)} ({fmt_pct(total_ret_pct)})",
         "sub": ("since " + fmt_money(start_equity) + " start") if start_equity > 0 else "history unavailable",
         "heat": total_ret_pct},
        {"label": "Today P&amp;L",
         "value": f"{'+' if day_pl >= 0 else ''}{fmt_money(day_pl)} ({fmt_pct(day_pl_pct)})",
         "sub": f"vs last close {fmt_money(last_equity)}", "heat": day_pl_pct},
        {"label": "Open P&amp;L",
         "value": f"{'+' if total_unrealized >= 0 else ''}{fmt_money(total_unrealized)} ({fmt_pct(total_unr_pct)})",
         "sub": f"on {fmt_money(total_cost)} cost basis", "heat": total_unr_pct},
        {"label": "Positions", "value": str(len(positions)),
         "sub": f"{winners} winners · {losers} losers"},
        {"label": "Cash", "value": fmt_money(cash),
         "sub": f"{(cash / equity * 100) if equity else 0:.1f}% of equity"},
    ]

    # Month-over-month (equity-series based), drop flat no-activity months.
    months = [m for m in monthly_performance(history) if m["pnl"] != 0]
    mo_labels = json.dumps([m["month"] for m in months])
    mo_pnl    = json.dumps([m["pnl"] for m in months])
    mo_colors = json.dumps(["#16a34a" if m["pnl"] >= 0 else "#dc2626" for m in months])
    mo_keys   = json.dumps([m["key"] for m in months])
    mo_rows = ""
    for m in reversed(months):
        h = heat_class(m["pct"])
        ps = "+" if m["pnl"] >= 0 else ""
        mo_rows += (
            f"<tr class='month-row' onclick=\"filterMonth('{m['key']}')\" title='Click to show only {m['month']} trades'>"
            f"<td class='bold'>{m['month']}</td>"
            f"<td class='mono'>{fmt_money(m['start'])}</td>"
            f"<td class='mono'>{fmt_money(m['end'])}</td>"
            f"<td class='mono heat {h}'>{ps}{fmt_money(m['pnl'])}</td>"
            f"<td class='mono heat {h}'>{fmt_pct(m['pct'])}</td>"
            "</tr>"
        )
    monthly_table = (
        "<table class='pos-table' style='margin-top:16px'><thead><tr><th>Month</th>"
        "<th>Start Equity</th><th>End Equity</th><th>P&amp;L</th><th>Return</th>"
        "</tr></thead><tbody>" + mo_rows + "</tbody></table>"
    ) if months else ""
    monthly_block = (
        "<div class='chart-card' style='margin-top:28px'>"
        "<h2 style='margin-top:0'>Month-over-Month Performance</h2>"
        + ("<div class='chart-wrap' style='height:240px'><canvas id='mochart'></canvas></div>"
           if months else "<div class='empty'>Not enough history yet for monthly performance.</div>")
        + monthly_table + "</div>"
    )

    rows = _alpaca_rows(positions, entry_dates, technicals)

    body = (
        render_stat_cards(cards)
        + "<div class='chart-card'><h2 style='margin-top:0'>Equity Curve</h2>"
          "<div class='chart-wrap'><canvas id='eqchart'></canvas></div></div>"
        + monthly_block
        + "<h2>Open Positions</h2>"
        + render_positions_section(rows, equity)
        + "<h2>Trade History (closed, 2026)</h2>"
        + render_trade_history(trades)
        + "<div class='footer'>Data: Alpaca paper API · updates hourly during US "
          "market hours via position-monitor workflow.</div>"
    )

    updated = datetime.datetime.now(datetime.timezone.utc).strftime("%d %b %Y %H:%M UTC")
    extra_head = '<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>'
    equity_points = build_equity_curve_js(history)
    extra_script = f"""
const points = {equity_points};
if (points.length > 0) {{
  new Chart(document.getElementById('eqchart').getContext('2d'), {{
    type: 'line',
    data: {{ datasets: [{{ label: 'Equity', data: points, borderColor: '#2563eb',
      backgroundColor: 'rgba(37,99,235,0.08)', fill: true, tension: 0.25,
      pointRadius: 0, borderWidth: 2 }}] }},
    options: {{ responsive: true, maintainAspectRatio: false,
      plugins: {{ legend: {{ display: false }} }},
      scales: {{ x: {{ type: 'category', grid: {{ display: false }}, ticks: {{ color: '#6b7280', maxTicksLimit: 8 }} }},
        y: {{ grid: {{ color: '#f3f4f6' }}, ticks: {{ color: '#6b7280',
          callback: function(v){{ return '$' + v.toLocaleString(); }} }} }} }} }}
  }});
}}
const moLabels = {mo_labels};
const moPnl    = {mo_pnl};
const moColors = {mo_colors};
const moKeys   = {mo_keys};
const moCanvas = document.getElementById('mochart');
if (moCanvas && moLabels.length > 0) {{
  new Chart(moCanvas.getContext('2d'), {{
    type: 'bar',
    data: {{ labels: moLabels, datasets: [{{ label: 'Monthly P&L', data: moPnl, backgroundColor: moColors }}] }},
    options: {{ responsive: true, maintainAspectRatio: false,
      onClick: function(evt, els) {{ if (els && els.length) {{ filterMonth(moKeys[els[0].index]); }} }},
      plugins: {{ legend: {{ display: false }} }},
      scales: {{ x: {{ grid: {{ display: false }}, ticks: {{ color: '#6b7280' }} }},
        y: {{ grid: {{ color: '#f3f4f6' }}, ticks: {{ color: '#6b7280',
          callback: function(v){{ return '$' + v.toLocaleString(); }} }} }} }} }}
  }});
}}
"""
    return page_shell("Claude Model Portfolio", "🤖 Claude Model Portfolio",
                      f"Alpaca paper account · auto-executed from daily screener · updated {updated}",
                      body, extra_head=extra_head, extra_script=extra_script)


def main() -> str | None:
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        log.warning("Skipping portfolio page — no Alpaca credentials.")
        return None

    account   = _get("/account") or {}
    positions = _get("/positions") or []
    history = {}
    for period in ("all", "1A", "3M"):
        history = _get("/account/portfolio/history",
                       {"period": period, "timeframe": "1D"}) or {}
        if history.get("equity"):
            break

    if not account:
        log.warning("Skipping portfolio page — account fetch failed.")
        return None

    fills = fetch_fills()
    trades = closed_trades(fills, since=TRADES_SINCE)
    entry_dates = open_entry_dates(fills)

    # Enrich open positions with Finviz technicals so the table matches live
    # (verdict / ATR% / S20% / Stage). Non-fatal per ticker.
    technicals = {}
    try:
        from utils.generators.generate_live_portfolio import _technicals
        for p in positions:
            sym = p.get("symbol")
            if sym:
                technicals[sym] = _technicals(sym)
    except Exception as e:
        log.warning("technicals enrichment failed: %s", e)

    os.makedirs(DATA_DIR, exist_ok=True)
    html = generate_html(account, positions, history, trades=trades,
                         entry_dates=entry_dates, technicals=technicals)
    with open(OUTPUT_PATH, "w") as f:
        f.write(html)
    log.info("claude_portfolio.html written → %s", OUTPUT_PATH)
    return OUTPUT_PATH


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
