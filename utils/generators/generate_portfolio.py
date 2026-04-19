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


def generate_html(account: dict, positions: list, history: dict) -> str:
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
        pos_rows += (
            "<tr>"
            f"<td class='bold'><a href='https://finviz.com/quote.ashx?t={sym}' target='_blank'>{sym}</a></td>"
            f"<td class='mono'>{qty}</td>"
            f"<td class='mono'>${entry:.2f}</td>"
            f"<td class='mono'>${price:.2f}</td>"
            f"<td class='mono'>{_fmt_money(mv)}</td>"
            f"<td class='mono'>{alloc:.1f}%</td>"
            f"<td class='mono heat {heat}'>{pl_sign}{_fmt_money(unr)}</td>"
            f"<td class='mono heat {heat}'>{pct_sign}{unr_pct:.2f}%</td>"
            "</tr>"
        )

    equity_points = build_equity_curve_js(history)
    day_heat  = _heat_class(day_pl_pct)
    unr_heat  = _heat_class(total_unr_pct)
    updated   = datetime.datetime.now(datetime.timezone.utc).strftime("%d %b %Y %H:%M UTC")

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
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
{"<table class='pos-table'><thead><tr><th>Ticker</th><th>Qty</th><th>Entry</th><th>Price</th><th>Mkt Value</th><th>Alloc</th><th>Unrealized $</th><th>Unrealized %</th></tr></thead><tbody>" + pos_rows + "</tbody></table>" if pos_rows else "<div class='empty'>No open positions.</div>"}

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
</script>
</body></html>
"""


def main() -> str | None:
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        log.warning("Skipping portfolio page — no Alpaca credentials.")
        return None

    account   = _get("/account") or {}
    positions = _get("/positions") or []
    history   = _get("/account/portfolio/history",
                     {"period": "3M", "timeframe": "1D"}) or {}

    if not account:
        log.warning("Skipping portfolio page — account fetch failed.")
        return None

    os.makedirs(DATA_DIR, exist_ok=True)
    html = generate_html(account, positions, history)
    with open(OUTPUT_PATH, "w") as f:
        f.write(html)
    log.info("claude_portfolio.html written → %s", OUTPUT_PATH)
    return OUTPUT_PATH


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
