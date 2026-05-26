#!/usr/bin/env python3
"""
Live SnapTrade Portfolio — HTML dashboard for the real-money book.

Mirrors utils/generators/generate_portfolio.py (paper / Alpaca) but pulls
from SnapTrade (account header) + Finviz (live quote + technicals). Writes
data/live_portfolio.html.

Light theme only (see memory/feedback_light_theme.md).

Invoked from agents/trading/position_monitor.py on every monitor run
(3x daily book runs + every 30 min during market hours).
"""

import datetime
import logging
import os
import re
import urllib.request

log = logging.getLogger(__name__)

DATA_DIR    = os.environ.get("DATA_DIR", "data")
OUTPUT_PATH = os.path.join(DATA_DIR, "live_portfolio.html")


# ---------- formatters ----------

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


# ---------- verdict ----------

def verdict_for(gain: float, atr: float, s20: float, stage: str) -> str:
    """Mirrors .claude/commands/pos-review.md verdict logic."""
    parts = []
    if gain <= -5:
        parts.append("🚨 CUT — past stop zone")
    elif gain >= 20:
        parts.append("💰 PEEL ½ (T1 rule)")
    elif gain >= 10:
        parts.append("🟢 trail tighter")
    elif gain >= 7 and atr > 7:
        parts.append("⚠ peel ⅓ — high vol extended")
    elif gain >= 5:
        parts.append("✅ working, hold")
    elif gain >= 2:
        parts.append("hold")
    elif gain >= 1:
        parts.append("🟡 sleeping")
    elif gain >= 0:
        parts.append("💀 dead weight — review")
    else:
        parts.append("watch — give a day")
    if s20 > 20:
        parts.append(f"ext +{s20:.0f}% S20")
    if stage and stage not in ("2P", "2"):
        parts.append(f"⚠ {stage}")
    return " · ".join(parts)


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


def build_row(pos: dict, tech_lookup=None) -> dict:
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
        "atr":    tech.get("atr", 0.0),
        "s20":    tech.get("s20", 0.0),
        "stage":  tech.get("stage", "?"),
    }


# ---------- HTML ----------

def render_html(account: dict, rows: list) -> str:
    equity = float(account.get("equity", 0) or 0)
    cash   = float(account.get("cash", 0) or 0)
    bp     = float(account.get("buying_power", 0) or 0)
    total_mv = sum(r["mv"] for r in rows)
    total_pl = sum(r["pl"] for r in rows)
    leverage_pct = ((-cash) / equity * 100) if (equity > 0 and cash < 0) else 0.0
    total_unr_pct = ((total_pl) / (total_mv - total_pl) * 100) if (total_mv - total_pl) > 0 else 0.0
    rows = sorted(rows, key=lambda r: -r["mv"])

    peel_count = sum(1 for r in rows if r["gain"] >= 20 or (r["gain"] >= 7 and r["atr"] > 7))
    cut_count  = sum(1 for r in rows if r["gain"] <= -5)
    dead_count = sum(1 for r in rows if 0 <= r["gain"] < 1 and r["mv"] > 5000)

    pos_rows_html = ""
    for r in rows:
        heat = _heat_class(r["gain"])
        verdict = verdict_for(r["gain"], r["atr"], r["s20"], r["stage"])
        pct_book = (r["mv"] / equity * 100) if equity > 0 else 0
        pl_sign  = "+" if r["pl"]   >= 0 else ""
        pct_sign = "+" if r["gain"] >= 0 else ""
        pos_rows_html += (
            "<tr>"
            f"<td class='bold'><a href='https://finviz.com/quote.ashx?t={r['ticker']}' target='_blank'>{r['ticker']}</a></td>"
            f"<td class='mono'>{r['shares']:.0f}</td>"
            f"<td class='mono'>${r['avg']:.2f}</td>"
            f"<td class='mono'>${r['live']:.2f}</td>"
            f"<td class='mono heat {heat}'>{pct_sign}{r['gain']:.2f}%</td>"
            f"<td class='mono heat {heat}'>{pl_sign}{_fmt_money(r['pl'])}</td>"
            f"<td class='mono'>{_fmt_money(r['mv'])}</td>"
            f"<td class='mono'>{pct_book:.1f}%</td>"
            f"<td class='mono'>{r['atr']:.1f}</td>"
            f"<td class='mono'>{r['s20']:+.1f}</td>"
            f"<td class='mono'>{r['stage']}</td>"
            f"<td>{verdict}</td>"
            "</tr>"
        )

    updated = datetime.datetime.now(datetime.timezone.utc).strftime("%d %b %Y %H:%M UTC")
    unr_heat = _heat_class(total_unr_pct)

    table_html = (
        "<table class='pos-table'><thead><tr>"
        "<th>TKR</th><th>Sh</th><th>Avg</th><th>Live</th><th>Δ%</th>"
        "<th>$P/L</th><th>MV</th><th>%Bk</th><th>ATR%</th><th>S20%</th>"
        "<th>St</th><th>Verdict</th></tr></thead><tbody>"
        + pos_rows_html + "</tbody></table>"
    ) if rows else "<div class='empty'>No open positions.</div>"

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<title>Live SnapTrade Portfolio</title>
<style>
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #f8f9fc; color: #111827; padding: 32px; max-width: 1500px; margin: 0 auto; }}
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
.heat {{ border-radius: 4px; font-weight: 600; padding: 2px 6px; display: inline-block; }}
.heat-pos-strong {{ background: #bbf7d0; color: #166534; }}
.heat-pos        {{ background: #dcfce7; color: #15803d; }}
.heat-zero       {{ color: #6b7280; }}
.heat-neg        {{ background: #fee2e2; color: #b91c1c; }}
.heat-neg-strong {{ background: #fecaca; color: #991b1b; }}
.empty {{ color: #6b7280; font-size: 0.88rem; padding: 24px; text-align: center;
         background: #fff; border: 1px dashed #e5e7eb; border-radius: 10px; }}
.footer {{ margin-top: 32px; font-size: 0.7rem; color: #9ca3af; }}
.action-summary {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 10px;
                   padding: 14px 18px; margin: 18px 0; font-size: 0.88rem; }}
.action-summary strong {{ color: #111827; }}
</style>
</head><body>

<h1>📈 Live SnapTrade Portfolio</h1>
<p class="subtitle">Real-money book · SnapTrade + Finviz · refreshed {updated}</p>

<div class="stat-grid">
  <div class="stat-card">
    <div class="stat-label">Equity</div>
    <div class="stat-val">{_fmt_money(equity)}</div>
    <div class="stat-sub">Buying power {_fmt_money(bp)}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Cash</div>
    <div class="stat-val">{_fmt_money(cash)}</div>
    <div class="stat-sub">{"margin debt" if cash < 0 else "free cash"}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Position MV</div>
    <div class="stat-val">{_fmt_money(total_mv)}</div>
    <div class="stat-sub">{len(rows)} positions</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Open P&amp;L</div>
    <div class="stat-val heat {unr_heat}">{"+" if total_pl >= 0 else ""}{_fmt_money(total_pl)} ({_fmt_pct(total_unr_pct)})</div>
    <div class="stat-sub">across all positions</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Leverage</div>
    <div class="stat-val">{leverage_pct:.0f}%</div>
    <div class="stat-sub">debt / equity</div>
  </div>
</div>

<div class="action-summary">
  <strong>Actions:</strong>
  💰 PEEL candidates: {peel_count} ·
  🚨 CUT today: {cut_count} ·
  💀 dead weight: {dead_count}
</div>

<h2>Open Positions — sorted by Market Value</h2>
{table_html}

<div class="footer">Data: SnapTrade (account) + Finviz (quotes &amp; technicals) · refreshes on every position monitor run.</div>

</body></html>
"""


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
        rows = [build_row(p) for p in positions]
        html = render_html(account, rows)
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
