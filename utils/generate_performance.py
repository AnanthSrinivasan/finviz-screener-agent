#!/usr/bin/env python3
"""
Performance charts generator — Robinhood 2026 YTD.

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

DATA_DIR    = os.environ.get("DATA_DIR", "data")
CSV_PATH    = os.path.join(DATA_DIR, "RH-2026.csv")
OUTPUT_PATH = os.path.join(DATA_DIR, "performance_2026.html")

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


def generate_html(trades: list[dict], stats: dict) -> str:
    eq_labels, eq_data = _equity_curve_js(trades)
    mo_labels, mo_data, mo_colors = _monthly_pnl_js(trades)
    rows = _trade_rows(trades)

    best  = stats["best_trade"]
    worst = stats["worst_trade"]
    best_str  = f"{best['ticker']} {_fmt_pnl(best['pnl'])}" if best else "—"
    worst_str = f"{worst['ticker']} {_fmt_pnl(worst['pnl'])}" if worst else "—"

    pf = stats["profit_factor"]
    pf_str = f"{pf:.2f}x" if pf != float("inf") else "∞"

    prior_note = ""
    if stats["prior_pnl"] != 0:
        prior_note = f"""
        <div class="prior-note">
          ⚠ {_fmt_pnl(stats['prior_pnl'])} from sells with cost basis in a prior period
          (excluded from stats above, included in equity curve).
        </div>"""

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Performance Overview — 2026 YTD</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f8fafc;color:#111827;font-size:14px}}
.page-wrap{{max-width:1200px;margin:0 auto;padding:24px 16px}}
h1{{font-size:22px;font-weight:700;color:#111827;margin-bottom:4px}}
.subtitle{{color:#6b7280;font-size:13px;margin-bottom:24px}}
.stat-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:28px}}
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
.prior-note{{background:#fffbeb;border:1px solid #fcd34d;border-radius:8px;padding:12px 16px;margin-bottom:20px;font-size:13px;color:#92400e}}
.footer{{color:#9ca3af;font-size:12px;text-align:center;margin-top:16px}}
</style>
</head>
<body>
<div class="page-wrap">
  <h1>Performance Overview — 2026 YTD</h1>
  <div class="subtitle">Robinhood account · realized trades · generated {now}</div>

  <div class="stat-grid">
    <div class="stat-card">
      <div class="stat-label">Realized P&amp;L</div>
      <div class="stat-val {_pnl_class(stats['total_pnl'])}">{_fmt_pnl(stats['total_pnl'])}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Win Rate</div>
      <div class="stat-val">{stats['win_rate']}%</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Avg Win</div>
      <div class="stat-val pos">{_fmt_pnl(stats['avg_win'])}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Avg Loss</div>
      <div class="stat-val neg">{_fmt_pnl(stats['avg_loss'])}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Profit Factor</div>
      <div class="stat-val">{pf_str}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Trades</div>
      <div class="stat-val">{stats['n_wins']}W / {stats['n_losses']}L</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Best Trade</div>
      <div class="stat-val pos" style="font-size:16px">{best_str}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Worst Trade</div>
      <div class="stat-val neg" style="font-size:16px">{worst_str}</div>
    </div>
  </div>

  {prior_note}

  <div class="charts-row">
    <div class="chart-card">
      <h2>Cumulative P&amp;L — 2026 YTD</h2>
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
      <tbody>{rows}</tbody>
    </table>
    </div>
  </div>

  <div class="footer">Source: Robinhood export · FIFO cost basis · prior-period sells estimated at sell price (P&amp;L = $0)</div>
</div>

<script>
const eqCtx = document.getElementById('eqChart').getContext('2d');
const eqLabels = {eq_labels};
const eqData   = {eq_data};
const eqColors = eqData.map(v => v >= 0 ? '#16a34a' : '#dc2626');
new Chart(eqCtx, {{
  type: 'line',
  data: {{
    labels: eqLabels,
    datasets: [{{
      data: eqData,
      borderColor: '#2563eb',
      backgroundColor: 'rgba(37,99,235,0.07)',
      borderWidth: 2,
      pointRadius: 3,
      pointBackgroundColor: eqColors,
      fill: true,
      tension: 0.3,
    }}]
  }},
  options: {{
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      x: {{ ticks: {{ maxTicksLimit: 8, font: {{ size: 11 }} }}, grid: {{ display: false }} }},
      y: {{ ticks: {{ callback: v => '$' + v.toLocaleString(), font: {{ size: 11 }} }} }}
    }}
  }}
}});

const moCtx = document.getElementById('moChart').getContext('2d');
const moLabels = {mo_labels};
const moData   = {mo_data};
const moColors = {mo_colors};
new Chart(moCtx, {{
  type: 'bar',
  data: {{
    labels: moLabels,
    datasets: [{{
      data: moData,
      backgroundColor: moColors,
      borderRadius: 4,
    }}]
  }},
  options: {{
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      x: {{ ticks: {{ font: {{ size: 11 }} }}, grid: {{ display: false }} }},
      y: {{ ticks: {{ callback: v => '$' + v.toLocaleString(), font: {{ size: 11 }} }} }}
    }}
  }}
}});
</script>
</body>
</html>"""


def main():
    rows   = load_csv(CSV_PATH)
    trades = compute_trades(rows)
    stats  = compute_stats(trades)
    html   = generate_html(trades, stats)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        f.write(html)
    print(f"Written: {OUTPUT_PATH}")
    print(f"Trades: {stats['n_trades']} closed | P&L: {_fmt_pnl(stats['total_pnl'])} | Win rate: {stats['win_rate']}%")


if __name__ == "__main__":
    main()
