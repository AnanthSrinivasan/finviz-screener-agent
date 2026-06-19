#!/usr/bin/env python3
"""
Claude Model Portfolio — HTML dashboard for the Alpaca paper account.

Fetches account, positions, and portfolio history from Alpaca and writes
data/claude_portfolio.html. Invoked from agents/trading/alpaca_monitor.py
so it refreshes hourly during market hours.

Light theme only (see memory/feedback_light_theme.md).
"""

import datetime
import json
import logging
import os
import requests

log = logging.getLogger(__name__)

ALPACA_API_KEY    = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2")
DATA_DIR          = os.environ.get("DATA_DIR", "data")
OUTPUT_PATH       = os.path.join(DATA_DIR, "claude_portfolio.html")

# Only show trades from 2026 — the account has older manual fills (2023) that
# predate the automated screener/executor and would pollute the trade log.
TRADES_SINCE = "2026-01-01"


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


def _fmt_money(v: float) -> str:
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.0f}"


def _fmt_pct(v: float) -> str:
    return f"{v:+.2f}%"


def _heat_class(pct: float) -> str:
    if pct >= 5:    return "heat-pos-strong"
    if pct > 0:     return "heat-pos"
    if pct == 0:    return "heat-zero"
    if pct > -5:    return "heat-neg"
    return "heat-neg-strong"


def inception_equity(history: dict) -> float:
    """First non-null equity point in the portfolio history (account start).

    Alpaca seeds a paper account at a known starting equity; the first history
    point is that starting value. Returns 0.0 when history is empty/unusable.
    """
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
    """(start_equity, abs_return, pct_return) since the account's first history
    point. abs/pct are 0 when no usable starting equity exists."""
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
    """Month-over-month equity change derived from the daily equity series.

    Returns [{"month": "Jan 2026", "start": .., "end": .., "pnl": .., "pct": ..}, ...]
    oldest→newest. Each month's pnl is end-of-month equity minus the prior
    month's end (the first month uses its own first equity point as the start).
    Mirrors the Monthly P&L bar on the Performance 2026 page, but for the paper
    account where the source of truth is Alpaca's portfolio-history equity curve.
    """
    if not history:
        return []
    timestamps = history.get("timestamp") or []
    equity     = history.get("equity") or []
    # collect (date, equity) skipping nulls
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

    # last equity seen within each calendar month, in chronological order
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
            "month": dt.strftime("%b %Y"),
            "key": key,                       # YYYY-MM — used to filter trades
            "start": round(start, 2),
            "end": round(end, 2),
            "pnl": round(pnl, 2),
            "pct": round(pct, 2),
        })
        prev_end = end
    return out


def open_entry_dates(fills: list) -> dict:
    """Earliest still-open FIFO lot date per symbol, from FILL activities.

    Alpaca's /positions payload carries no entry date, so we replay buys/sells
    FIFO and report the oldest date among the lots that remain open. Used to
    show an Entry Date on the open-positions table.
    """
    lots: dict[str, list] = {}   # symbol -> FIFO list of [qty, date]
    for f in sorted(fills or [], key=lambda f: f.get("transaction_time", "")):
        sym  = f.get("symbol")
        side = f.get("side")
        try:
            qty = float(f.get("qty", 0) or 0)
        except (TypeError, ValueError):
            continue
        if not sym or qty <= 0:
            continue
        date = (f.get("transaction_time") or "")[:10]
        if side == "buy":
            lots.setdefault(sym, []).append([qty, date])
            continue
        remaining = qty
        while remaining > 1e-9 and lots.get(sym):
            lot = lots[sym][0]
            take = min(lot[0], remaining)
            lot[0] -= take
            remaining -= take
            if lot[0] <= 1e-9:
                lots[sym].pop(0)
    out: dict[str, str] = {}
    for sym, ls in lots.items():
        dates = [d for q, d in ls if q > 1e-9 and d]
        if dates:
            out[sym] = min(dates)
    return out


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


def closed_trades(fills: list, since: str = None) -> list:
    """FIFO round-trip trades from FILL activities, aggregated to one row per
    (symbol, exit date) so partial fills of the same order collapse together.

    Returns newest-first: [{symbol, entry_date, exit_date, qty, avg_entry,
    avg_exit, pnl, pct, hold_days}]. `since` (YYYY-MM-DD) drops trades whose
    exit predates it.
    """
    lots: dict[str, list] = {}   # symbol -> FIFO list of [qty, price, date]
    legs = []
    for f in sorted(fills or [], key=lambda f: f.get("transaction_time", "")):
        sym  = f.get("symbol")
        side = f.get("side")
        try:
            qty   = float(f.get("qty", 0) or 0)
            price = float(f.get("price", 0) or 0)
        except (TypeError, ValueError):
            continue
        if not sym or qty <= 0:
            continue
        date = (f.get("transaction_time") or "")[:10]
        if side == "buy":
            lots.setdefault(sym, []).append([qty, price, date])
            continue
        # sell: consume FIFO lots; a sell with no recorded buy lot is skipped
        remaining = qty
        while remaining > 1e-9 and lots.get(sym):
            lot = lots[sym][0]
            take = min(lot[0], remaining)
            legs.append((sym, lot[2], date, take, lot[1], price))
            lot[0] -= take
            remaining -= take
            if lot[0] <= 1e-9:
                lots[sym].pop(0)

    agg: dict[tuple, dict] = {}
    for sym, entry_date, exit_date, qty, entry_px, exit_px in legs:
        if since and exit_date < since:
            continue
        row = agg.setdefault((sym, exit_date), {
            "symbol": sym, "entry_date": entry_date, "exit_date": exit_date,
            "qty": 0.0, "entry_cost": 0.0, "exit_value": 0.0,
        })
        row["entry_date"] = min(row["entry_date"], entry_date)
        row["qty"]        += qty
        row["entry_cost"] += qty * entry_px
        row["exit_value"] += qty * exit_px

    out = []
    for row in agg.values():
        qty       = row["qty"]
        avg_entry = row["entry_cost"] / qty if qty else 0.0
        avg_exit  = row["exit_value"] / qty if qty else 0.0
        pnl       = row["exit_value"] - row["entry_cost"]
        pct       = ((avg_exit / avg_entry - 1) * 100) if avg_entry else 0.0
        d0 = datetime.date.fromisoformat(row["entry_date"])
        d1 = datetime.date.fromisoformat(row["exit_date"])
        out.append({
            "symbol": row["symbol"], "entry_date": row["entry_date"],
            "exit_date": row["exit_date"], "qty": round(qty),
            "avg_entry": round(avg_entry, 2), "avg_exit": round(avg_exit, 2),
            "pnl": round(pnl, 2), "pct": round(pct, 2),
            "hold_days": (d1 - d0).days,
        })
    out.sort(key=lambda t: (t["exit_date"], t["symbol"]), reverse=True)
    return out


def trade_stats(trades: list) -> dict:
    """Win/loss summary over a closed-trades list."""
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    n = len(trades)
    gross_w = sum(t["pnl"] for t in wins)
    gross_l = sum(t["pnl"] for t in losses)
    avg_w = (gross_w / len(wins)) if wins else 0.0
    avg_l = (gross_l / len(losses)) if losses else 0.0
    return {
        "count": n, "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / n * 100, 1) if n else 0.0,
        "net": round(gross_w + gross_l, 2),
        "avg_win": round(avg_w, 2), "avg_loss": round(avg_l, 2),
        "payoff": round(abs(avg_w / avg_l), 2) if avg_l else 0.0,
    }


def generate_html(account: dict, positions: list, history: dict,
                  trades: list = None, entry_dates: dict = None) -> str:
    entry_dates = entry_dates or {}
    today = datetime.date.today()
    equity      = float(account.get("equity", 0) or 0)
    last_equity = float(account.get("last_equity", 0) or 0)
    cash        = float(account.get("cash", 0) or 0)
    bp          = float(account.get("buying_power", 0) or 0)
    day_pl      = equity - last_equity
    day_pl_pct  = (day_pl / last_equity * 100) if last_equity else 0

    total_unrealized = sum(float(p.get("unrealized_pl", 0) or 0) for p in positions)
    total_cost       = sum(float(p.get("cost_basis", 0) or 0)    for p in positions)
    total_mv         = sum(float(p.get("market_value", 0) or 0)  for p in positions)
    total_unr_pct    = (total_unrealized / total_cost * 100) if total_cost else 0

    winners = sum(1 for p in positions if float(p.get("unrealized_pl", 0) or 0) > 0)
    losers  = sum(1 for p in positions if float(p.get("unrealized_pl", 0) or 0) < 0)

    # Sort positions by market value desc
    positions_sorted = sorted(
        positions,
        key=lambda p: float(p.get("market_value", 0) or 0),
        reverse=True,
    )

    pos_rows = ""
    for p in positions_sorted:
        sym        = p.get("symbol", "")
        qty        = p.get("qty", "0")
        entry      = float(p.get("avg_entry_price", 0) or 0)
        price      = float(p.get("current_price", 0) or 0)
        mv         = float(p.get("market_value", 0) or 0)
        unr        = float(p.get("unrealized_pl", 0) or 0)
        unr_pct    = float(p.get("unrealized_plpc", 0) or 0) * 100
        alloc      = (mv / equity * 100) if equity else 0
        heat       = _heat_class(unr_pct)
        pl_sign    = "+" if unr >= 0 else ""
        pct_sign   = "+" if unr_pct >= 0 else ""
        edate      = entry_dates.get(sym)
        if edate:
            try:
                held = f"{(today - datetime.date.fromisoformat(edate)).days}d"
            except ValueError:
                held = "—"
        else:
            edate, held = "—", "—"
        pos_rows += (
            "<tr>"
            f"<td class='bold'><a href='https://finviz.com/quote.ashx?t={sym}' target='_blank'>{sym}</a></td>"
            f"<td class='mono'>{edate}</td>"
            f"<td class='mono'>{held}</td>"
            f"<td class='mono'>{qty}</td>"
            f"<td class='mono'>${entry:.2f}</td>"
            f"<td class='mono'>${price:.2f}</td>"
            f"<td class='mono'>{_fmt_money(mv)}</td>"
            f"<td class='mono'>{alloc:.1f}%</td>"
            f"<td class='mono heat {heat}'>{pl_sign}{_fmt_money(unr)}</td>"
            f"<td class='mono heat {heat}'>{pct_sign}{unr_pct:.2f}%</td>"
            "</tr>"
        )

    start_equity, total_ret, total_ret_pct = compute_total_return(history, equity)

    # Drop fully-flat (no-activity) months so a long dormant stretch doesn't
    # bury the active trading months — mirrors the active-period view on the
    # Performance 2026 page.
    months = [m for m in monthly_performance(history) if m["pnl"] != 0]
    mo_labels = json.dumps([m["month"] for m in months])
    mo_pnl    = json.dumps([m["pnl"] for m in months])
    mo_colors = json.dumps(["#16a34a" if m["pnl"] >= 0 else "#dc2626" for m in months])
    mo_keys   = json.dumps([m["key"] for m in months])
    mo_rows = ""
    for m in reversed(months):  # newest first in the table
        h = _heat_class(m["pct"])
        ps = "+" if m["pnl"] >= 0 else ""
        mo_rows += (
            f"<tr class='month-row' onclick=\"filterMonth('{m['key']}')\" title='Click to show only {m['month']} trades'>"
            f"<td class='bold'>{m['month']}</td>"
            f"<td class='mono'>{_fmt_money(m['start'])}</td>"
            f"<td class='mono'>{_fmt_money(m['end'])}</td>"
            f"<td class='mono heat {h}'>{ps}{_fmt_money(m['pnl'])}</td>"
            f"<td class='mono heat {h}'>{_fmt_pct(m['pct'])}</td>"
            "</tr>"
        )

    trades = trades or []
    stats = trade_stats(trades)
    trade_rows = ""
    for t in trades:
        h  = _heat_class(t["pct"])
        ps = "+" if t["pnl"] >= 0 else ""
        exit_month = t["exit_date"][:7]
        trade_rows += (
            f"<tr class='trade-row' data-month='{exit_month}'>"
            f"<td class='bold' data-sort='{t['symbol']}'><a href='https://finviz.com/quote.ashx?t={t['symbol']}' target='_blank'>{t['symbol']}</a></td>"
            f"<td class='mono' data-sort='{t['entry_date']}'>{t['entry_date']}</td>"
            f"<td class='mono' data-sort='{t['exit_date']}'>{t['exit_date']}</td>"
            f"<td class='mono' data-sort='{t['hold_days']}'>{t['hold_days']}d</td>"
            f"<td class='mono' data-sort='{t['qty']}'>{t['qty']}</td>"
            f"<td class='mono' data-sort='{t['avg_entry']}'>${t['avg_entry']:,.2f}</td>"
            f"<td class='mono' data-sort='{t['avg_exit']}'>${t['avg_exit']:,.2f}</td>"
            f"<td class='mono heat {h}' data-sort='{t['pnl']}'>{ps}{_fmt_money(t['pnl'])}</td>"
            f"<td class='mono heat {h}' data-sort='{t['pct']}'>{_fmt_pct(t['pct'])}</td>"
            "</tr>"
        )
    stats_line = ""
    if stats["count"]:
        net_sign = "+" if stats["net"] >= 0 else ""
        stats_line = (
            f"{stats['count']} closed trades · {stats['wins']}W / {stats['losses']}L "
            f"({stats['win_rate']:.0f}% win) · net {net_sign}{_fmt_money(stats['net'])} · "
            f"avg win +{_fmt_money(stats['avg_win'])} / avg loss {_fmt_money(stats['avg_loss'])} · "
            f"payoff {stats['payoff']:.1f}"
        )

    if trade_rows:
        _cols = [("Ticker", "str"), ("Entry Date", "date"), ("Exit Date", "date"),
                 ("Held", "num"), ("Qty", "num"), ("Avg Entry", "num"),
                 ("Avg Exit", "num"), ("P&amp;L", "num"), ("Return", "num")]
        _ths = "".join(
            f"<th class='sortable' onclick=\"sortTable('tradeTable',{i},'{ty}',this)\">"
            f"{lbl}<span class='arrow'></span></th>"
            for i, (lbl, ty) in enumerate(_cols)
        )
        trade_table_html = (
            f"<table class='pos-table' id='tradeTable'><thead><tr>{_ths}</tr></thead>"
            f"<tbody>{trade_rows}</tbody></table>"
        )
        filter_bar_html = (
            "<div class='filter-bar'>Showing <strong id='monthLabel'>all months</strong> · "
            "<a href='#' onclick=\"filterMonth('all');return false;\">show all</a> "
            "<span class='hint'>— click a month in the chart/table above, or a column header to sort</span></div>"
        )
    else:
        trade_table_html = "<div class='empty'>No closed trades yet.</div>"
        filter_bar_html = ""

    equity_points = build_equity_curve_js(history)
    day_heat  = _heat_class(day_pl_pct)
    unr_heat  = _heat_class(total_unr_pct)
    tot_heat  = _heat_class(total_ret_pct)
    updated   = datetime.datetime.now(datetime.timezone.utc).strftime("%d %b %Y %H:%M UTC")

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>Claude Model Portfolio</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #f8f9fc; color: #111827; padding: 32px; max-width: 1400px; margin: 0 auto; }}
h1 {{ font-size: 1.5rem; font-weight: 700; margin-bottom: 4px; }}
.subtitle {{ color: #6b7280; font-size: 0.82rem; margin-bottom: 24px; }}
h2 {{ font-size: 0.8rem; font-weight: 700; color: #6b7280; text-transform: uppercase;
     letter-spacing: .08em; margin: 28px 0 14px; }}
.stat-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
              gap: 12px; margin-bottom: 24px; }}
.stat-card {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 10px;
              padding: 16px 18px; box-shadow: 0 1px 3px rgba(0,0,0,0.04); }}
.stat-label {{ font-size: 0.68rem; color: #9ca3af; text-transform: uppercase;
               letter-spacing: .06em; margin-bottom: 6px; }}
.stat-val {{ font-size: 1.35rem; font-weight: 700; color: #111827; }}
.stat-sub {{ font-size: 0.73rem; color: #6b7280; margin-top: 4px; }}
.pos-table {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; background: #fff;
              border: 1px solid #e5e7eb; border-radius: 10px; overflow: hidden; }}
.pos-table th {{ text-align: left; padding: 10px 12px; color: #6b7280; font-weight: 500;
                 border-bottom: 1px solid #e5e7eb; text-transform: uppercase;
                 font-size: 0.66rem; letter-spacing: .05em; background: #f9fafb; }}
.pos-table td {{ padding: 10px 12px; border-bottom: 1px solid #f3f4f6; color: #111827; }}
.pos-table tr:last-child td {{ border-bottom: none; }}
.pos-table tr:hover td {{ background: #f9fafb; }}
.bold {{ font-weight: 700; }}
.mono {{ font-variant-numeric: tabular-nums; }}
a {{ color: #2563eb; text-decoration: none; }}
a:hover {{ color: #1d4ed8; text-decoration: underline; }}
.heat {{ border-radius: 4px; font-weight: 600; }}
.heat-pos-strong {{ background: #bbf7d0; color: #166534; }}
.heat-pos        {{ background: #dcfce7; color: #15803d; }}
.heat-zero       {{ color: #6b7280; }}
.heat-neg        {{ background: #fee2e2; color: #b91c1c; }}
.heat-neg-strong {{ background: #fecaca; color: #991b1b; }}
.chart-card {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 10px;
               padding: 18px 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.04);
               margin-bottom: 24px; }}
.chart-wrap {{ position: relative; height: 320px; }}
.empty {{ color: #6b7280; font-size: 0.88rem; padding: 24px; text-align: center;
         background: #fff; border: 1px dashed #e5e7eb; border-radius: 10px; }}
.footer {{ margin-top: 32px; font-size: 0.7rem; color: #9ca3af; }}
.sortable {{ cursor: pointer; user-select: none; }}
.sortable:hover {{ color: #2563eb; }}
.arrow {{ font-size: 0.7em; margin-left: 4px; color: #9ca3af; }}
.month-row {{ cursor: pointer; }}
.month-row:hover td {{ background: #eef2ff; }}
.filter-bar {{ font-size: 0.8rem; color: #6b7280; margin-bottom: 12px; }}
.filter-bar strong {{ color: #2563eb; }}
.filter-bar .hint {{ color: #9ca3af; }}
</style>
</head><body>

<h1>🤖 Claude Model Portfolio</h1>
<p class="subtitle">Alpaca paper account · auto-executed from daily screener · updated {updated}</p>

<div class="stat-grid">
  <div class="stat-card">
    <div class="stat-label">Equity</div>
    <div class="stat-val">{_fmt_money(equity)}</div>
    <div class="stat-sub">Buying power {_fmt_money(bp)}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Total Return</div>
    <div class="stat-val heat {tot_heat}" style="display:inline-block;padding:2px 8px;">
      {"+" if total_ret >= 0 else ""}{_fmt_money(total_ret)} ({_fmt_pct(total_ret_pct)})
    </div>
    <div class="stat-sub">{"since " + _fmt_money(start_equity) + " start" if start_equity > 0 else "history unavailable"}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Today P&amp;L</div>
    <div class="stat-val heat {day_heat}" style="display:inline-block;padding:2px 8px;">
      {"+" if day_pl >= 0 else ""}{_fmt_money(day_pl)} ({_fmt_pct(day_pl_pct)})
    </div>
    <div class="stat-sub">vs last close {_fmt_money(last_equity)}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Open P&amp;L</div>
    <div class="stat-val heat {unr_heat}" style="display:inline-block;padding:2px 8px;">
      {"+" if total_unrealized >= 0 else ""}{_fmt_money(total_unrealized)} ({_fmt_pct(total_unr_pct)})
    </div>
    <div class="stat-sub">on {_fmt_money(total_cost)} cost basis</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Positions</div>
    <div class="stat-val">{len(positions)}</div>
    <div class="stat-sub">{winners} winners · {losers} losers</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Cash</div>
    <div class="stat-val">{_fmt_money(cash)}</div>
    <div class="stat-sub">{(cash / equity * 100) if equity else 0:.1f}% of equity</div>
  </div>
</div>

<div class="chart-card">
  <h2 style="margin-top:0">Equity Curve</h2>
  <div class="chart-wrap"><canvas id="eqchart"></canvas></div>
</div>

<h2>Open Positions</h2>
{"<table class='pos-table'><thead><tr><th>Ticker</th><th>Entry Date</th><th>Held</th><th>Qty</th><th>Entry $</th><th>Price</th><th>Mkt Value</th><th>Alloc</th><th>Unrealized $</th><th>Unrealized %</th></tr></thead><tbody>" + pos_rows + "</tbody></table>" if pos_rows else "<div class='empty'>No open positions.</div>"}

<div class="chart-card" style="margin-top:28px">
  <h2 style="margin-top:0">Month-over-Month Performance</h2>
  {'<div class="chart-wrap" style="height:240px"><canvas id="mochart"></canvas></div>' if months else '<div class="empty">Not enough history yet for monthly performance.</div>'}
  {("<table class='pos-table' style='margin-top:16px'><thead><tr><th>Month</th><th>Start Equity</th><th>End Equity</th><th>P&amp;L</th><th>Return</th></tr></thead><tbody>" + mo_rows + "</tbody></table>") if months else ""}
</div>

<h2>Trade History (closed, 2026)</h2>
{("<p class='subtitle' style='margin-bottom:12px'>" + stats_line + "</p>") if stats_line else ""}
{filter_bar_html}
{trade_table_html}

<div class="footer">Data: Alpaca paper API · updates hourly during US market hours via position-monitor workflow.</div>

<script>
const points = {equity_points};
if (points.length > 0) {{
  const ctx = document.getElementById('eqchart').getContext('2d');
  new Chart(ctx, {{
    type: 'line',
    data: {{
      datasets: [{{
        label: 'Equity',
        data: points,
        borderColor: '#2563eb',
        backgroundColor: 'rgba(37,99,235,0.08)',
        fill: true,
        tension: 0.25,
        pointRadius: 0,
        borderWidth: 2
      }}]
    }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      plugins: {{ legend: {{ display: false }} }},
      scales: {{
        x: {{ type: 'category', grid: {{ display: false }}, ticks: {{ color: '#6b7280', maxTicksLimit: 8 }} }},
        y: {{ grid: {{ color: '#f3f4f6' }}, ticks: {{ color: '#6b7280',
               callback: function(v){{ return '$' + v.toLocaleString(); }} }} }}
      }}
    }}
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
    options: {{
      responsive: true,
      maintainAspectRatio: false,
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

function filterMonth(key) {{
  var rows = document.querySelectorAll('#tradeTable tbody tr');
  var shown = 0;
  rows.forEach(function(r) {{
    var match = (key === 'all') || (r.getAttribute('data-month') === key);
    r.style.display = match ? '' : 'none';
    if (match) shown++;
  }});
  var lbl = document.getElementById('monthLabel');
  if (lbl) lbl.textContent = (key === 'all') ? 'all months' : (key + ' (' + shown + ' trades)');
  var tt = document.getElementById('tradeTable');
  if (tt) tt.scrollIntoView({{behavior: 'smooth', block: 'start'}});
}}

function sortTable(tableId, col, type, th) {{
  var table = document.getElementById(tableId);
  var tbody = table.tBodies[0];
  var rows = Array.prototype.slice.call(tbody.querySelectorAll('tr'));
  var asc = th.getAttribute('data-asc') !== 'true';
  // reset arrows
  table.querySelectorAll('th .arrow').forEach(function(a) {{ a.textContent = ''; }});
  th.querySelector('.arrow').textContent = asc ? ' ▲' : ' ▼';
  th.parentNode.querySelectorAll('th').forEach(function(h) {{ h.setAttribute('data-asc', 'false'); }});
  th.setAttribute('data-asc', asc ? 'true' : 'false');
  rows.sort(function(a, b) {{
    var x = a.children[col].getAttribute('data-sort') || a.children[col].textContent;
    var y = b.children[col].getAttribute('data-sort') || b.children[col].textContent;
    var cmp;
    if (type === 'num') {{ cmp = parseFloat(x) - parseFloat(y); }}
    else {{ cmp = String(x).localeCompare(String(y)); }}  // date ISO + str both sort lexically
    return asc ? cmp : -cmp;
  }});
  rows.forEach(function(r) {{ tbody.appendChild(r); }});
}}
</script>
</body></html>
"""


def main() -> str | None:
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        log.warning("Skipping portfolio page — no Alpaca credentials.")
        return None

    account   = _get("/account") or {}
    positions = _get("/positions") or []
    # period=all so the curve + Total Return reflect the full account growth
    # since inception, not just the trailing 3 months. Fall back to 1A then 3M
    # if a longer window returns nothing.
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

    os.makedirs(DATA_DIR, exist_ok=True)
    html = generate_html(account, positions, history, trades=trades,
                         entry_dates=entry_dates)
    with open(OUTPUT_PATH, "w") as f:
        f.write(html)
    log.info("claude_portfolio.html written → %s", OUTPUT_PATH)
    return OUTPUT_PATH


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
