#!/usr/bin/env python3
"""
Performance charts generator — Robinhood realized trades.

Renders one page with period tabs: YTD (default) · Lifetime · each prior year.

Reads data/RH-2026.csv, does FIFO P&L matching per ticker, and writes
data/performance_charts.html with:
  - Stat cards (total P&L, win rate, avg win/loss, profit factor)
  - Cumulative equity curve (Chart.js)
  - Monthly P&L bar chart
  - Per-trade table (sorted by date)

Sells without a matching buy in the CSV are flagged as "prior period" —
cost basis unknown, excluded from stats but listed in the table.

Run: python utils/generate_performance.py
"""

import csv
import datetime
import json
import os
import re

DATA_DIR       = os.environ.get("DATA_DIR", "data")
CSV_PATH       = os.path.join(DATA_DIR, "RH-2026.csv")
POSITIONS_PATH = os.path.join(DATA_DIR, "positions.json")
HISTORY_PATH   = os.path.join(DATA_DIR, "position_history.json")
OUTPUT_PATH    = os.path.join(DATA_DIR, "performance_2026.html")

TRADE_CODES = {"Buy", "Sell"}


def _parse_amount(s: str) -> float:
    """'$1,234.56' → 1234.56 | '($1,234.56)' → -1234.56 | '' → 0."""
    s = s.strip()
    if not s:
        return 0.0
    negative = s.startswith("(")
    s = s.replace("(", "").replace(")", "").replace("$", "").replace(",", "")
    try:
        v = float(s)
        return -v if negative else v
    except ValueError:
        return 0.0


def _parse_price(s: str) -> float:
    s = s.strip().replace("$", "").replace(",", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _parse_qty(s: str) -> float:
    try:
        return float(s.strip())
    except ValueError:
        return 0.0


def _parse_date(s: str) -> datetime.date:
    return datetime.datetime.strptime(s.strip(), "%m/%d/%Y").date()


def load_csv(path: str) -> list[dict]:
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            code = row.get("Trans Code", "").strip()
            if code not in TRADE_CODES:
                continue
            ticker = row.get("Instrument", "").strip()
            if not ticker:
                continue
            rows.append({
                "date":   _parse_date(row["Activity Date"]),
                "ticker": ticker,
                "side":   code,
                "qty":    _parse_qty(row["Quantity"]),
                "price":  _parse_price(row["Price"]),
                "amount": _parse_amount(row["Amount"]),  # positive = credit (sell)
            })
    # Sort chronologically; within same day, Buys before Sells
    rows.sort(key=lambda r: (r["date"], 0 if r["side"] == "Buy" else 1))
    return rows


def load_system_closed(path: str) -> list[dict]:
    """Read positions.json closed_positions[] → list of trade dicts.

    System-only trades come from the rules engine (auto-close + retro-patch).
    Broker CSV is the canonical source once Robinhood has settled the fill;
    until then, system truth fills the gap so the dashboard stays live.
    """
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []

    trades: list[dict] = []
    for p in state.get("closed_positions", []) or []:
        close_date = p.get("close_date")
        ticker     = p.get("ticker")
        shares     = p.get("shares")
        entry      = p.get("entry_price")
        close_px   = p.get("close_price")
        if not (close_date and ticker and shares and entry and close_px):
            continue
        try:
            sell_dt   = datetime.date.fromisoformat(close_date)
            entry_dt  = datetime.date.fromisoformat(p.get("entry_date", close_date))
        except ValueError:
            continue
        cost     = float(entry) * float(shares)
        proceeds = float(close_px) * float(shares)
        pnl      = proceeds - cost
        trades.append({
            "ticker":       ticker,
            "sell_date":    sell_dt,
            "first_buy":    entry_dt,
            "qty":          float(shares),
            "proceeds":     round(proceeds, 2),
            "cost":         round(cost, 2),
            "pnl":          round(pnl, 2),
            "pnl_pct":      round(p.get("result_pct", pnl / cost * 100 if cost else 0), 2),
            "prior_period": False,
            "system_only":  True,
            "close_source": p.get("close_source", ""),
        })
    return trades


def _split_into_cycles(events: list[dict]) -> list[list[dict]]:
    """Split a ticker's BUY/SELL stream into discrete trade cycles. A cycle
    ends when running shares return to 0; the next BUY starts a new cycle.

    Without this, FLY's 90-day history (Mar round-trip 400 in/400 out + Apr-May
    cycle 450 in/450 out) walks as one 850/850 trade with bogus avg cost.
    """
    cycles: list[list[dict]] = []
    current: list[dict] = []
    running = 0.0
    for ev in events:
        sh = float(ev.get("shares", 0) or 0)
        action = ev.get("action", "")
        if sh <= 0:
            continue
        if action == "BUY" and running <= 0 and current:
            cycles.append(current)
            current = []
        current.append(ev)
        if action == "BUY":
            running += sh
        elif action == "SELL":
            running = max(0.0, running - sh)
    if current:
        cycles.append(current)
    return cycles


def load_snaptrade_partial_realized(path: str) -> list[dict]:
    """Read data/position_history.json → list of fully-closed trade cycles per
    ticker. Each cycle (BUYs until shares return to 0) emits one trade row using
    the shared pnl_walk helper. Open-but-trimmed positions (AAOI/GLW class)
    are intentionally NOT emitted here — performance_2026 is a closed-trade
    ledger, partial-trim realized P/L surfaces on the dashboard's open-position
    $P/L cell instead.
    """
    from utils.pnl_walk import compute_pnl_from_events

    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            hist_doc = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    hist = hist_doc.get("history") or {}
    out: list[dict] = []
    for ticker, events in hist.items():
        if not events:
            continue
        for cycle in _split_into_cycles(events):
            walk = compute_pnl_from_events(cycle, current_price=0, current_shares=0)
            # Only fully-closed cycles land on performance_2026
            if walk["final_shares"] > 0.0001 or walk["total_sold_units"] <= 0:
                continue
            sell_dates = [e.get("date", "")[:10] for e in cycle if e.get("action") == "SELL"]
            buy_dates  = [e.get("date", "")[:10] for e in cycle if e.get("action") == "BUY"]
            if not sell_dates:
                continue
            try:
                sell_dt   = datetime.date.fromisoformat(max(sell_dates))
                first_buy = datetime.date.fromisoformat(min(buy_dates)) if buy_dates else sell_dt
            except ValueError:
                continue
            cost     = walk["cost_basis_sold"]
            proceeds = walk["proceeds_sold"]
            pnl      = walk["realized"]
            pnl_pct  = (pnl / cost * 100) if cost else 0.0
            out.append({
                "ticker":       ticker,
                "sell_date":    sell_dt,
                "first_buy":    first_buy,
                "qty":          float(walk["total_sold_units"]),
                "proceeds":     round(proceeds, 2),
                "cost":         round(cost, 2),
                "pnl":          round(pnl, 2),
                "pnl_pct":      round(pnl_pct, 2),
                "prior_period": False,
                "system_only":  True,
                "close_source": "snaptrade_walk",
            })
    return out


def merge_trades(broker: list[dict], system: list[dict]) -> list[dict]:
    """Broker truth wins. Add system trades that don't have a matching broker
    trade within ±5 days for the same ticker."""
    out = list(broker)
    for s in broker:
        s["system_only"] = False
    for sys_t in system:
        match = False
        for br_t in broker:
            if br_t["ticker"] != sys_t["ticker"]:
                continue
            if abs((br_t["sell_date"] - sys_t["sell_date"]).days) <= 5:
                match = True
                break
        if not match:
            out.append(sys_t)
    return sorted(out, key=lambda t: t["sell_date"])


def compute_trades(rows: list[dict]) -> list[dict]:
    """FIFO matching. Returns list of closed trade dicts."""
    from collections import deque

    # buy_queue[ticker] = deque of (qty, price_per_share, cost_basis_total, date)
    buy_queue: dict[str, deque] = {}
    trades: list[dict] = []

    for row in rows:
        ticker = row["ticker"]
        if ticker not in buy_queue:
            buy_queue[ticker] = deque()

        if row["side"] == "Buy":
            # Push lot onto queue
            buy_queue[ticker].append({
                "qty":    row["qty"],
                "price":  row["price"],
                "cost":   abs(row["amount"]),  # total cost including fees
                "date":   row["date"],
            })

        elif row["side"] == "Sell":
            sell_qty     = row["qty"]
            sell_proceeds = row["amount"]  # positive (net after fees)
            sell_date    = row["date"]
            queue        = buy_queue[ticker]

            remaining_qty      = sell_qty
            total_cost_matched = 0.0
            buy_dates          = []
            prior_period       = False

            while remaining_qty > 0 and queue:
                lot = queue[0]
                take = min(remaining_qty, lot["qty"])
                # Cost per share for this lot
                cost_per_share = lot["cost"] / lot["qty"] if lot["qty"] > 0 else lot["price"]
                total_cost_matched += take * cost_per_share
                buy_dates.append(lot["date"])
                lot["qty"]  -= take
                lot["cost"] -= take * cost_per_share
                remaining_qty -= take
                if lot["qty"] < 0.001:
                    queue.popleft()

            if remaining_qty > 0:
                # Shares sold without a matching buy — prior period basis
                prior_period = True
                # Estimate cost from the sell price (neutral — so P&L = 0 for these)
                total_cost_matched += remaining_qty * row["price"]

            pnl = sell_proceeds - total_cost_matched
            pnl_pct = pnl / total_cost_matched * 100 if total_cost_matched else 0.0
            first_buy = min(buy_dates) if buy_dates else None

            trades.append({
                "ticker":       ticker,
                "sell_date":    sell_date,
                "first_buy":    first_buy,
                "qty":          sell_qty,
                "proceeds":     round(sell_proceeds, 2),
                "cost":         round(total_cost_matched, 2),
                "pnl":          round(pnl, 2),
                "pnl_pct":      round(pnl_pct, 2),
                "prior_period": prior_period,
            })

    return sorted(trades, key=lambda t: t["sell_date"])


def compute_stats(trades: list[dict]) -> dict:
    closed = [t for t in trades if not t["prior_period"]]
    wins   = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] < 0]
    total_pnl   = sum(t["pnl"] for t in closed)
    total_wins  = sum(t["pnl"] for t in wins)
    total_losses = abs(sum(t["pnl"] for t in losses))
    prior_pnl   = sum(t["pnl"] for t in trades if t["prior_period"])
    return {
        "total_pnl":     round(total_pnl, 2),
        "prior_pnl":     round(prior_pnl, 2),
        "n_trades":      len(closed),
        "n_wins":        len(wins),
        "n_losses":      len(losses),
        "win_rate":      round(len(wins) / len(closed) * 100, 1) if closed else 0,
        "avg_win":       round(total_wins / len(wins), 2) if wins else 0,
        "avg_loss":      round(-total_losses / len(losses), 2) if losses else 0,
        "profit_factor": round(total_wins / total_losses, 2) if total_losses else float("inf"),
        "best_trade":    max(closed, key=lambda t: t["pnl"]) if closed else None,
        "worst_trade":   min(closed, key=lambda t: t["pnl"]) if closed else None,
    }


def _period_slices(trades: list[dict]) -> list[tuple]:
    """Return ordered [(key, label, trades_in_period)] for the period tabs.

    Added 2026-08-18: this page rendered a single all-time trade list under a
    "2026 YTD" title, so lifetime stats were being read as this year's — the
    headline showed +$69,857 while 2026 was actually −$20,579, and Best Trade
    surfaced a COIN cycle that closed in July 2025. Slices are now explicit:
    YTD first (the default question), then Lifetime, then each prior year
    descending. Each slice carries its own stats, charts and trade table.
    """
    years = sorted({t["sell_date"].year for t in trades}, reverse=True)
    this_year = datetime.date.today().year
    out: list[tuple] = []
    if this_year in years:
        out.append((f"ytd", f"{this_year} YTD",
                    [t for t in trades if t["sell_date"].year == this_year]))
    out.append(("lifetime", "Lifetime", list(trades)))
    for y in years:
        if y == this_year:
            continue
        out.append((str(y), str(y), [t for t in trades if t["sell_date"].year == y]))
    return out


def _equity_curve_js(trades: list[dict]) -> tuple[str, str]:
    """Return (labels_json, data_json) for the cumulative P&L chart."""
    cumulative = 0.0
    labels = []
    data   = []
    # Carry prior-period trades too so curve starts correctly
    for t in trades:
        cumulative += t["pnl"]
        labels.append(t["sell_date"].strftime("%b %d"))
        data.append(round(cumulative, 2))
    return json.dumps(labels), json.dumps(data)


def _monthly_pnl_js(trades: list[dict]) -> tuple[str, str]:
    monthly: dict[str, float] = {}
    for t in trades:
        key = t["sell_date"].strftime("%b %Y")
        monthly[key] = round(monthly.get(key, 0) + t["pnl"], 2)
    labels = list(monthly.keys())
    data   = list(monthly.values())
    colors = ["#16a34a" if v >= 0 else "#dc2626" for v in data]
    return json.dumps(labels), json.dumps(data), json.dumps(colors)


def _fmt_pnl(v: float) -> str:
    sign = "+" if v >= 0 else ""
    return f"{sign}${v:,.0f}"


def _fmt_pct(v: float) -> str:
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.1f}%"


def _pnl_class(v: float) -> str:
    return "pos" if v > 0 else ("neg" if v < 0 else "neu")


def _trade_rows(trades: list[dict]) -> str:
    rows = ""
    for t in sorted(trades, key=lambda x: x["sell_date"], reverse=True):
        pp_badge = ' <span class="pp-badge">prior period</span>' if t["prior_period"] else ""
        if t.get("system_only"):
            src = (t.get("close_source") or "").lower()
            if src.startswith("snaptrade"):
                # Real broker fill confirmed via SnapTrade — RH CSV just not re-uploaded yet
                pp_badge += ' <span class="sys-badge sys-confirmed" title="SnapTrade fill confirmed — awaiting Robinhood CSV reconciliation">snaptrade fill</span>'
            else:
                # No broker fill landed — closed via fallback (peak high or user-reported)
                label    = "estimated fill" if src else "system close"
                src_disp = src or "unknown"
                pp_badge += f' <span class="sys-badge sys-estimated" title="No broker fill detected — close_source={src_disp}">{label}</span>'
        buy_str  = t["first_buy"].strftime("%b %d") if t["first_buy"] else "—"
        cls      = _pnl_class(t["pnl"])
        rows += f"""
        <tr>
          <td>{t['sell_date'].strftime('%b %d, %Y')}</td>
          <td class="ticker-col">{t['ticker']}{pp_badge}</td>
          <td>{t['qty']:g}</td>
          <td>${t['proceeds']:,.2f}</td>
          <td>${t['cost']:,.2f}</td>
          <td class="{cls} pnl-col">{_fmt_pnl(t['pnl'])}</td>
          <td class="{cls}">{_fmt_pct(t['pnl_pct'])}</td>
          <td>{buy_str}</td>
        </tr>"""
    return rows


def _period_payload(trades: list[dict]) -> dict:
    """Everything the page needs to render one period, ready for JSON embed."""
    stats = compute_stats(trades)
    eq_labels, eq_data = _equity_curve_js(trades)
    mo_labels, mo_data, mo_colors = _monthly_pnl_js(trades)

    best  = stats["best_trade"]
    worst = stats["worst_trade"]
    pf    = stats["profit_factor"]

    prior_note = ""
    if stats["prior_pnl"] != 0:
        prior_note = (
            '<div class="prior-note">&#9888; ' + _fmt_pnl(stats["prior_pnl"])
            + " from sells with cost basis in a prior period "
            + "(excluded from stats above, included in equity curve).</div>"
        )

    return {
        "pnl":        _fmt_pnl(stats["total_pnl"]),
        "pnl_class":  _pnl_class(stats["total_pnl"]),
        "win_rate":   f"{stats['win_rate']}%",
        "avg_win":    _fmt_pnl(stats["avg_win"]),
        "avg_loss":   _fmt_pnl(stats["avg_loss"]),
        "pf":         f"{pf:.2f}x" if pf != float("inf") else "∞",
        "wl":         f"{stats['n_wins']}W / {stats['n_losses']}L",
        "best":       f"{best['ticker']} {_fmt_pnl(best['pnl'])}" if best else "—",
        "worst":      f"{worst['ticker']} {_fmt_pnl(worst['pnl'])}" if worst else "—",
        "prior_note": prior_note,
        "rows":       _trade_rows(trades),
        "eq":         {"labels": json.loads(eq_labels), "data": json.loads(eq_data)},
        "mo":         {"labels": json.loads(mo_labels), "data": json.loads(mo_data),
                       "colors": json.loads(mo_colors)},
    }


def generate_html(trades: list[dict], stats: dict) -> str:
    periods  = _period_slices(trades)
    payloads = {}
    for key, label, sub in periods:
        payloads[key] = _period_payload(sub)
        payloads[key]["label"] = label
    default_key    = periods[0][0] if periods else "lifetime"
    periods_js     = json.dumps(payloads)
    default_key_js = json.dumps(default_key)

    period_btns = "".join(
        '<button class="pbtn{active}" data-period="{k}" onclick="showPeriod(\'{k}\')">{lbl}</button>'.format(
            active=" active" if k == default_key else "", k=k, lbl=lbl)
        for k, lbl, _sub in periods
    )

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>Performance Overview</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f8fafc;color:#111827;font-size:14px}}
.page-wrap{{max-width:1200px;margin:0 auto;padding:24px 16px}}
h1{{font-size:22px;font-weight:700;color:#111827;margin-bottom:4px}}
.subtitle{{color:#6b7280;font-size:13px;margin-bottom:24px}}
.stat-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:28px}}
.period-bar{{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:18px}}
.pbtn{{padding:8px 16px;border-radius:8px;border:1px solid #d1d5db;background:#fff;
  color:#374151;font-size:13px;font-weight:600;cursor:pointer;font-family:inherit}}
.pbtn:hover{{background:#f3f4f6}}
.pbtn.active{{background:#2563eb;color:#fff;border-color:#2563eb}}
.stat-card{{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:16px}}
.stat-label{{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:#6b7280;margin-bottom:6px}}
.stat-val{{font-size:22px;font-weight:700;color:#111827}}
.stat-val.pos{{color:#16a34a}}
.stat-val.neg{{color:#dc2626}}
.charts-row{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:28px}}
@media(max-width:700px){{.charts-row{{grid-template-columns:1fr}}}}
.chart-card{{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:16px}}
.chart-card h2{{font-size:13px;font-weight:600;color:#374151;margin-bottom:12px}}
.chart-card canvas{{max-height:260px}}
.table-card{{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:16px;margin-bottom:28px}}
.table-card h2{{font-size:13px;font-weight:600;color:#374151;margin-bottom:12px}}
.trade-table{{width:100%;border-collapse:collapse;font-size:13px}}
.trade-table th{{text-align:left;padding:8px 10px;border-bottom:2px solid #e5e7eb;color:#6b7280;font-weight:600;font-size:11px;text-transform:uppercase}}
.trade-table td{{padding:7px 10px;border-bottom:1px solid #f3f4f6}}
.trade-table tr:hover td{{background:#f9fafb}}
.pos{{color:#16a34a}}
.neg{{color:#dc2626}}
.neu{{color:#6b7280}}
.pnl-col{{font-weight:600}}
.ticker-col{{font-weight:600;color:#2563eb}}
.pp-badge{{font-size:10px;background:#fef3c7;color:#92400e;border-radius:4px;padding:1px 5px;margin-left:4px;font-weight:500}}
.sys-badge{{font-size:10px;border-radius:4px;padding:1px 5px;margin-left:4px;font-weight:500}}
.sys-confirmed{{background:#dcfce7;color:#166534}}
.sys-estimated{{background:#fef3c7;color:#92400e}}
.prior-note{{background:#fffbeb;border:1px solid #fcd34d;border-radius:8px;padding:12px 16px;margin-bottom:20px;font-size:13px;color:#92400e}}
.footer{{color:#9ca3af;font-size:12px;text-align:center;margin-top:16px}}
</style>
</head>
<body>
<div class="page-wrap">
  <h1>Performance Overview — <span id="periodTitle"></span></h1>
  <div class="subtitle">Robinhood realized trades · <span class="sys-badge sys-confirmed">snaptrade fill</span> = real broker fill (RH CSV not yet re-uploaded) · <span class="sys-badge sys-estimated">estimated fill</span> = no broker fill yet · generated {now}</div>

  <div class="period-bar">{period_btns}</div>

  <div class="stat-grid">
    <div class="stat-card">
      <div class="stat-label">Realized P&amp;L</div>
      <div class="stat-val" id="sPnl"></div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Win Rate</div>
      <div class="stat-val" id="sWinRate"></div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Avg Win</div>
      <div class="stat-val pos" id="sAvgWin"></div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Avg Loss</div>
      <div class="stat-val neg" id="sAvgLoss"></div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Profit Factor</div>
      <div class="stat-val" id="sPf"></div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Trades</div>
      <div class="stat-val" id="sWl"></div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Best Trade</div>
      <div class="stat-val pos" style="font-size:16px" id="sBest"></div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Worst Trade</div>
      <div class="stat-val neg" style="font-size:16px" id="sWorst"></div>
    </div>
  </div>

  <div id="priorNote"></div>

  <div class="charts-row">
    <div class="chart-card">
      <h2>Cumulative P&amp;L — <span id="eqTitle"></span></h2>
      <canvas id="eqChart"></canvas>
    </div>
    <div class="chart-card">
      <h2>Monthly P&amp;L</h2>
      <canvas id="moChart"></canvas>
    </div>
  </div>

  <div class="table-card">
    <h2>Closed Trades</h2>
    <div style="overflow-x:auto">
    <table class="trade-table">
      <thead>
        <tr>
          <th>Close Date</th><th>Ticker</th><th>Qty</th>
          <th>Proceeds</th><th>Cost Basis</th><th>P&amp;L $</th><th>P&amp;L %</th><th>First Buy</th>
        </tr>
      </thead>
      <tbody id="tradeRows"></tbody>
    </table>
    </div>
  </div>

  <div class="footer">Source: Robinhood export · FIFO cost basis · prior-period sells estimated at sell price (P&amp;L = $0)</div>
</div>

<script>
const PERIODS = {periods_js};

const eqChart = new Chart(document.getElementById('eqChart').getContext('2d'), {{
  type: 'line',
  data: {{ labels: [], datasets: [{{
      data: [],
      borderColor: '#2563eb',
      backgroundColor: 'rgba(37,99,235,0.07)',
      borderWidth: 2,
      pointRadius: 3,
      pointBackgroundColor: [],
      fill: true,
      tension: 0.3,
  }}] }},
  options: {{
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      x: {{ ticks: {{ maxTicksLimit: 8, font: {{ size: 11 }} }}, grid: {{ display: false }} }},
      y: {{ ticks: {{ callback: v => '$' + v.toLocaleString(), font: {{ size: 11 }} }} }}
    }}
  }}
}});

const moChart = new Chart(document.getElementById('moChart').getContext('2d'), {{
  type: 'bar',
  data: {{ labels: [], datasets: [{{ data: [], backgroundColor: [], borderRadius: 4 }}] }},
  options: {{
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      x: {{ ticks: {{ font: {{ size: 11 }} }}, grid: {{ display: false }} }},
      y: {{ ticks: {{ callback: v => '$' + v.toLocaleString(), font: {{ size: 11 }} }} }}
    }}
  }}
}});

function showPeriod(key) {{
  const p = PERIODS[key];
  if (!p) return;

  document.getElementById('periodTitle').textContent = p.label;
  document.getElementById('eqTitle').textContent     = p.label;

  const pnlEl = document.getElementById('sPnl');
  pnlEl.textContent = p.pnl;
  pnlEl.className   = 'stat-val ' + p.pnl_class;
  document.getElementById('sWinRate').textContent = p.win_rate;
  document.getElementById('sAvgWin').textContent  = p.avg_win;
  document.getElementById('sAvgLoss').textContent = p.avg_loss;
  document.getElementById('sPf').textContent      = p.pf;
  document.getElementById('sWl').textContent      = p.wl;
  document.getElementById('sBest').textContent    = p.best;
  document.getElementById('sWorst').textContent   = p.worst;

  document.getElementById('priorNote').innerHTML = p.prior_note;
  document.getElementById('tradeRows').innerHTML = p.rows;

  eqChart.data.labels = p.eq.labels;
  eqChart.data.datasets[0].data = p.eq.data;
  eqChart.data.datasets[0].pointBackgroundColor =
    p.eq.data.map(v => v >= 0 ? '#16a34a' : '#dc2626');
  eqChart.update();

  moChart.data.labels = p.mo.labels;
  moChart.data.datasets[0].data = p.mo.data;
  moChart.data.datasets[0].backgroundColor = p.mo.colors;
  moChart.update();

  document.querySelectorAll('.pbtn').forEach(function(b) {{
    b.classList.toggle('active', b.dataset.period === key);
  }});
}}

showPeriod({default_key_js});
</script>
</body>
</html>"""


def main():
    if os.path.exists(CSV_PATH):
        rows   = load_csv(CSV_PATH)
        broker = compute_trades(rows)
    else:
        broker = []
    system = load_system_closed(POSITIONS_PATH)
    snap   = load_snaptrade_partial_realized(HISTORY_PATH)
    # Broker walk (position_history.json) is canonical. Two filters:
    #  1) Drop closed_positions rows for tickers whose SnapTrade walk shows
    #     shares still open — rules engine sometimes records a close prematurely
    #     while broker still holds residual shares (AAOI/GLW May 2026).
    #  2) Drop closed_positions rows whose date falls inside a SnapTrade cycle
    #     (the cycle walk supersedes the synthesized FINAL-tranche row).
    from utils.pnl_walk import compute_pnl_from_events
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH) as f:
                hist = (json.load(f).get("history") or {})
        except (OSError, json.JSONDecodeError):
            hist = {}
    else:
        hist = {}
    open_per_broker = {tk for tk, evs in hist.items()
                       if compute_pnl_from_events(evs, 0, 0)["final_shares"] > 0.0001}
    snap_cycles_by_ticker: dict = {}
    for t in snap:
        snap_cycles_by_ticker.setdefault(t["ticker"], []).append(t)
    system = [s for s in system
              if s["ticker"] not in open_per_broker
              and not any(snap_t["first_buy"] <= s["sell_date"] <= snap_t["sell_date"] + datetime.timedelta(days=5)
                          for snap_t in snap_cycles_by_ticker.get(s["ticker"], []))]
    trades = merge_trades(merge_trades(broker, system), snap)
    stats  = compute_stats(trades)
    html   = generate_html(trades, stats)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        f.write(html)
    print(f"Written: {OUTPUT_PATH}")
    print(f"Trades: {stats['n_trades']} closed | P&L: {_fmt_pnl(stats['total_pnl'])} | Win rate: {stats['win_rate']}%")


if __name__ == "__main__":
    main()
