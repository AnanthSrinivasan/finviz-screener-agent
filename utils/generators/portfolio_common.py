#!/usr/bin/env python3
"""
Shared rendering + analytics for the portfolio dashboards.

Both portfolio pages — the paper account (Alpaca) and the live book (SnapTrade) —
are the SAME dashboard reading from different sources. This module holds every
piece they share so the two never drift apart again:

  • formatters + heat classes + the pos-review verdict/action ladder
  • a single FIFO trade-pairing engine (`closed_trades`, `open_entry_dates`)
    that consumes a normalized event list, so Alpaca fills and SnapTrade
    activities both flow through one code path
  • the unified Open Positions section (verdict + technicals + entry/held +
    clickable action chips + legend)
  • the Trade History section (sortable + month filter)
  • the shared CSS + JS (sort / filter)

Each generator becomes a thin adapter: fetch from its source → normalize to the
common row / event schema → call these renderers.

One design system: PORTFOLIO_CSS is a thin extension of theme.BASE_CSS and
page_shell delegates to theme.page_shell (spec docs/specs/cx-rehaul.md §4).
"""

import datetime
import json

from utils.generators.theme import page_shell as _theme_page_shell


# ---------- formatters ----------

def fmt_money(v: float) -> str:
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.0f}"


def fmt_pct(v: float) -> str:
    return f"{v:+.2f}%"


def heat_class(pct: float) -> str:
    if pct >= 5:    return "heat-pos-strong"
    if pct > 0:     return "heat-pos"
    if pct == 0:    return "heat-zero"
    if pct > -5:    return "heat-neg"
    return "heat-neg-strong"


def held_days(entry_date: str) -> str:
    if not entry_date or entry_date == "—":
        return "—"
    try:
        d0 = datetime.date.fromisoformat(str(entry_date)[:10])
        return f"{(datetime.date.today() - d0).days}d"
    except ValueError:
        return "—"


def _parse_held(val) -> int:
    """Parse held-days from string like '18d' or int."""
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        return int(val.replace("d", "")) if val.rstrip("d").isdigit() else 0
    return 0


# ---------- verdict / action ----------

def verdict_for(gain: float, atr: float, s20: float, stage: str, held: int = 0) -> str:
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
        if held <= 2:
            parts.append("watch — give a day")
        else:
            parts.append("💀 dead weight — review")
    else:
        parts.append("watch — give a day")
    if s20 > 20:
        parts.append(f"ext +{s20:.0f}% S20")
    if stage and stage not in ("2P", "2"):
        parts.append(f"⚠ {stage}")
    return " · ".join(parts)


def classify_action(gain: float, atr: float, held: int = 0) -> str:
    """Single source for the action summary chips AND each row's data-action tag,
    so they can never disagree with the Verdict column.

    Returns a key in {cut, peel, trail, dead, hold}. Mirrors verdict_for's EXACT
    clause ordering — gain≥10 → trail is checked BEFORE the high-vol peel clause,
    so a +11% high-ATR name is `trail`, not `peel`.
    """
    if gain <= -5:
        return "cut"
    if gain >= 20:
        return "peel"
    if gain >= 10:
        return "trail"
    if gain >= 7 and atr > 7:
        return "peel"
    if 0 <= gain < 1:
        return "hold" if held <= 2 else "dead"
    return "hold"


# ---------- FIFO trade engine (source-agnostic) ----------
#
# Both sources normalize their fills/activities into a list of events:
#   {"symbol": str, "side": "buy"|"sell", "qty": float, "price": float,
#    "date": "YYYY-MM-DD"}
# and these two functions do the rest.

def _normalize_events(events: list) -> list:
    out = []
    for e in events or []:
        try:
            qty = float(e.get("qty", 0) or 0)
            price = float(e.get("price", 0) or 0)
        except (TypeError, ValueError):
            continue
        sym = e.get("symbol")
        side = (e.get("side") or "").lower()
        if not sym or qty <= 0 or side not in ("buy", "sell"):
            continue
        out.append({"symbol": sym, "side": side, "qty": qty,
                    "price": price, "date": (e.get("date") or "")[:10]})
    out.sort(key=lambda e: e["date"])
    return out


def closed_trades(events: list, since: str = None) -> list:
    """FIFO round-trip trades, aggregated to one row per (symbol, exit date).

    Newest-first: [{symbol, entry_date, exit_date, qty, avg_entry, avg_exit,
    pnl, pct, hold_days}]. `since` (YYYY-MM-DD) drops trades whose exit predates
    it.
    """
    lots: dict[str, list] = {}   # symbol -> FIFO list of [qty, price, date]
    legs = []
    for e in _normalize_events(events):
        sym, side, qty, price, date = (e["symbol"], e["side"], e["qty"],
                                       e["price"], e["date"])
        if side == "buy":
            lots.setdefault(sym, []).append([qty, price, date])
            continue
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
        try:
            d0 = datetime.date.fromisoformat(row["entry_date"])
            d1 = datetime.date.fromisoformat(row["exit_date"])
            hold = (d1 - d0).days
        except ValueError:
            hold = 0
        out.append({
            "symbol": row["symbol"], "entry_date": row["entry_date"],
            "exit_date": row["exit_date"], "qty": round(qty),
            "avg_entry": round(avg_entry, 2), "avg_exit": round(avg_exit, 2),
            "pnl": round(pnl, 2), "pct": round(pct, 2), "hold_days": hold,
        })
    out.sort(key=lambda t: (t["exit_date"], t["symbol"]), reverse=True)
    return out


def open_entry_dates(events: list) -> dict:
    """Earliest still-open FIFO lot date per symbol (no source carries an entry
    date on the open-positions payload, so we replay the events)."""
    lots: dict[str, list] = {}
    for e in _normalize_events(events):
        sym, side, qty, date = e["symbol"], e["side"], e["qty"], e["date"]
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


def trade_stats(trades: list) -> dict:
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


def monthly_realized(trades: list) -> list:
    """Realized P&L grouped by exit month, oldest→newest:
    [{month: 'Jun 2026', key: '2026-06', pnl: .., count: ..}].

    Used by the live page (no daily equity series) so it still gets a
    month-over-month bar that mirrors the paper page's shape."""
    buckets: dict[str, dict] = {}
    for t in trades:
        key = t["exit_date"][:7]
        b = buckets.setdefault(key, {"key": key, "pnl": 0.0, "count": 0})
        b["pnl"] += t["pnl"]
        b["count"] += 1
    out = []
    for key in sorted(buckets):
        b = buckets[key]
        dt = datetime.date.fromisoformat(key + "-01")
        out.append({"month": dt.strftime("%b %Y"), "key": key,
                    "pnl": round(b["pnl"], 2), "count": b["count"]})
    return out


# ---------- shared renderers ----------

def render_stat_cards(cards: list) -> str:
    """cards: [{label, value, sub, heat(optional bool→wrap value in heat span,
    pct used for class)}]."""
    html = "<div class='stat-grid'>"
    for c in cards:
        val = c["value"]
        if c.get("heat") is not None:
            cls = heat_class(c["heat"])
            val = (f"<span class='stat-val heat {cls}' "
                   f"style='display:inline-block;padding:2px 8px;'>{val}</span>")
        else:
            val = f"<div class='stat-val'>{val}</div>"
        html += ("<div class='stat-card'>"
                 f"<div class='stat-label'>{c['label']}</div>{val}"
                 f"<div class='stat-sub'>{c.get('sub','')}</div></div>")
    return html + "</div>"


# One-liner — full definitions live in each column header's hover tooltip
# (Principle 4, cx-rehaul: verbose legends → tooltip/one-liner).
POSITIONS_LEGEND = (
    "<div class='legend'>Rows sort action-first · click a summary chip to "
    "filter · hover a column header for its definition.</div>"
)


def render_positions_section(rows: list, equity: float) -> str:
    """Unified Open Positions: action chips + table + legend. `rows` use the
    common schema {ticker, shares, avg, live, gain, pl, mv, entry_date, held,
    atr, s20, stage}. Empty atr/s20/stage render as `—`."""
    if not rows:
        return "<div class='empty'>No open positions.</div>"

    for r in rows:
        r["action"] = classify_action(r.get("gain", 0), r.get("atr", 0) or 0, held=_parse_held(r.get("held")))
    _prio = {"cut": 0, "peel": 1, "dead": 2, "trail": 3, "hold": 4}
    rows = sorted(rows, key=lambda r: (_prio.get(r["action"], 9), -r.get("mv", 0)))

    peel = sum(1 for r in rows if r["action"] == "peel")
    cut  = sum(1 for r in rows if r["action"] == "cut")
    dead = sum(1 for r in rows if r["action"] == "dead")

    summary = (
        "<div class='action-summary'><strong>Actions</strong> "
        "<span class='hint'>(click to filter the table)</span>: "
        f"<a class='chip' href='#' onclick=\"filterAction('peel');return false;\">💰 PEEL: {peel}</a>"
        f"<a class='chip' href='#' onclick=\"filterAction('cut');return false;\">🚨 CUT: {cut}</a>"
        f"<a class='chip' href='#' onclick=\"filterAction('dead');return false;\">💀 dead weight: {dead}</a>"
        "<a class='chip chip-all' href='#' onclick=\"filterAction('all');return false;\">show all</a></div>"
    )

    body = ""
    for idx, r in enumerate(rows, 1):
        gain = r.get("gain", 0)
        h = heat_class(gain)
        verdict = verdict_for(gain, r.get("atr", 0) or 0, r.get("s20", 0) or 0,
                              r.get("stage", "?"), held=_parse_held(r.get("held")))
        pct_book = (r.get("mv", 0) / equity * 100) if equity > 0 else 0
        atr = r.get("atr")
        s20 = r.get("s20")
        stage = r.get("stage") or "—"
        atr_txt = f"{atr:.1f}" if atr else "—"
        s20_txt = f"{s20:+.1f}" if (s20 is not None and s20 != 0) else ("—" if not s20 else f"{s20:+.1f}")
        pl_sign = "+" if r.get("pl", 0) >= 0 else ""
        pct_sign = "+" if gain >= 0 else ""
        stop = r.get("stop")
        live_price = r.get("live", 0)
        if stop and live_price > 0:
            dist_stop_dollars = live_price - stop
            dist_stop_pct = dist_stop_dollars / live_price * 100
            stop_txt = f"${stop:.2f}"
            dist_cls = "heat-neg" if dist_stop_pct < 2 else ""
            sign = "+" if dist_stop_dollars >= 0 else ""
            dist_txt = f"{sign}${dist_stop_dollars:.2f}"
        else:
            stop_txt = "—"
            dist_cls = ""
            dist_txt = "—"
        body += (
            f"<tr data-action='{r['action']}'>"
            f"<td class='mono num'>{idx}</td>"
            f"<td class='bold'><a href='https://finviz.com/quote.ashx?t={r['ticker']}' target='_blank'>{r['ticker']}</a></td>"
            f"<td class='mono'>{r.get('entry_date','—')}</td>"
            f"<td class='mono'>{r.get('held','—')}</td>"
            f"<td class='mono'>{r.get('shares',0):.0f}</td>"
            f"<td class='mono'>${r.get('avg',0):.2f}</td>"
            f"<td class='mono'>${r.get('live',0):.2f}</td>"
            f"<td class='mono heat {h}'>{pct_sign}{gain:.2f}%</td>"
            f"<td class='mono heat {h}'>{pl_sign}{fmt_money(r.get('pl',0))}</td>"
            f"<td class='mono'>{fmt_money(r.get('mv',0))}</td>"
            f"<td class='mono'>{pct_book:.1f}%</td>"
            f"<td class='mono'>{atr_txt}</td>"
            f"<td class='mono {dist_cls}'>{stop_txt}</td>"
            f"<td class='mono {dist_cls}'>{dist_txt}</td>"
            f"<td class='mono'>{s20_txt}</td>"
            f"<td class='mono'>{stage}</td>"
            f"<td>{verdict}</td>"
            "</tr>"
        )

    table = (
        "<table class='pos-table' id='posTable'><thead><tr>"
        "<th>No.</th><th>TKR</th>"
        "<th title='Date the position was opened'>Entry</th>"
        "<th title='Days held since entry'>Held</th>"
        "<th>Sh</th><th title='Average cost'>Avg</th><th>Live</th>"
        "<th title='Gain since entry'>Δ%</th>"
        "<th title='Unrealized profit/loss in dollars'>$P/L</th>"
        "<th title='Market value'>MV</th>"
        "<th title='Position size as % of total book/equity'>%Bk</th>"
        "<th title='Average True Range % — daily volatility'>ATR%</th>"
        "<th title='Tracked stop price — ATR-tiered trail off peak'>Stop</th>"
        "<th title='Dollar distance from live price to stop'>Room</th>"
        "<th title='Price vs its 20-day moving average — + above / - below'>S20%</th>"
        "<th title='Weinstein stage — 2P = perfect Stage 2'>St</th>"
        "<th>Verdict</th></tr></thead><tbody>"
        + body + "</tbody></table>"
    )
    return summary + table + POSITIONS_LEGEND


def render_trade_history(trades: list, table_id: str = "tradeTable") -> str:
    """Sortable, month-filterable closed-trades table + W/L stats line."""
    if not trades:
        return "<div class='empty'>No closed trades yet.</div>"

    stats = trade_stats(trades)
    net_sign = "+" if stats["net"] >= 0 else ""
    stats_line = (
        f"{stats['count']} closed trades · {stats['wins']}W / {stats['losses']}L "
        f"({stats['win_rate']:.0f}% win) · net {net_sign}{fmt_money(stats['net'])} · "
        f"avg win +{fmt_money(stats['avg_win'])} / avg loss {fmt_money(stats['avg_loss'])} · "
        f"payoff {stats['payoff']:.1f}"
    )

    body = ""
    for t in trades:
        h = heat_class(t["pct"])
        ps = "+" if t["pnl"] >= 0 else ""
        em = t["exit_date"][:7]
        body += (
            f"<tr class='trade-row' data-month='{em}'>"
            f"<td class='bold' data-sort='{t['symbol']}'><a href='https://finviz.com/quote.ashx?t={t['symbol']}' target='_blank'>{t['symbol']}</a></td>"
            f"<td class='mono' data-sort='{t['entry_date']}'>{t['entry_date']}</td>"
            f"<td class='mono' data-sort='{t['exit_date']}'>{t['exit_date']}</td>"
            f"<td class='mono' data-sort='{t['hold_days']}'>{t['hold_days']}d</td>"
            f"<td class='mono' data-sort='{t['qty']}'>{t['qty']}</td>"
            f"<td class='mono' data-sort='{t['avg_entry']}'>${t['avg_entry']:,.2f}</td>"
            f"<td class='mono' data-sort='{t['avg_exit']}'>${t['avg_exit']:,.2f}</td>"
            f"<td class='mono heat {h}' data-sort='{t['pnl']}'>{ps}{fmt_money(t['pnl'])}</td>"
            f"<td class='mono heat {h}' data-sort='{t['pct']}'>{fmt_pct(t['pct'])}</td>"
            "</tr>"
        )

    cols = [("Ticker", "str"), ("Entry Date", "date"), ("Exit Date", "date"),
            ("Held", "num"), ("Qty", "num"), ("Avg Entry", "num"),
            ("Avg Exit", "num"), ("P&amp;L", "num"), ("Return", "num")]
    ths = "".join(
        f"<th class='sortable' onclick=\"sortTable('{table_id}',{i},'{ty}',this)\">"
        f"{lbl}<span class='arrow'></span></th>"
        for i, (lbl, ty) in enumerate(cols)
    )
    filter_bar = (
        "<div class='filter-bar'>Showing <strong id='monthLabel'>all months</strong> · "
        "<a href='#' onclick=\"filterMonth('all');return false;\">show all</a> "
        "<span class='hint'>— click a month above, or a column header to sort</span></div>"
    )
    table = (f"<table class='pos-table' id='{table_id}'><thead><tr>{ths}</tr></thead>"
             f"<tbody>{body}</tbody></table>")
    return (f"<p class='subtitle' style='margin-bottom:12px'>{stats_line}</p>"
            + filter_bar + table)


# ---------- shared CSS + JS ----------

# Thin extension of theme.BASE_CSS — only portfolio-specific classes here.
PORTFOLIO_CSS = """
body { max-width: 1500px; }
.stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
              gap: 12px; margin-bottom: 24px; }
.stat-card { background: var(--surface); border: 1px solid var(--border);
              border-radius: 10px; padding: 14px 16px; }
.stat-label { font-size: 0.68rem; color: var(--muted); text-transform: uppercase;
               letter-spacing: .06em; margin-bottom: 6px; }
.stat-val { font-size: 1.35rem; font-weight: 700; color: var(--head); }
.stat-sub { font-size: 0.73rem; color: var(--muted); margin-top: 4px; }
.pos-table { width: 100%; border-collapse: collapse; font-size: 0.82rem;
              background: var(--surface); border: 1px solid var(--border);
              border-radius: 10px; overflow: hidden; }
.pos-table th { text-align: left; padding: 9px 11px; color: var(--muted); font-weight: 600;
                 border-bottom: 1px solid var(--border); text-transform: uppercase;
                 font-size: 0.66rem; letter-spacing: .05em; background: var(--surface2); }
.pos-table td { padding: 9px 11px; border-bottom: 1px solid var(--border); color: var(--text); }
.pos-table tr:last-child td { border-bottom: none; }
.pos-table tr:hover td { background: var(--surface2); }
.num  { color: var(--muted); }
.heat { border-radius: 4px; font-weight: 600; padding: 2px 6px; display: inline-block;
         white-space: nowrap; min-width: 58px; text-align: right; }
.heat-pos-strong { background: var(--green-bg); color: var(--green); }
.heat-pos        { background: var(--green-bg); color: var(--green-text); }
.heat-zero       { color: var(--muted); }
.heat-neg        { background: var(--red-bg); color: var(--red-text); }
.heat-neg-strong { background: var(--red-bg); color: var(--red); }
.chart-card { background: var(--surface); border: 1px solid var(--border);
               border-radius: 10px; padding: 16px 18px; margin-bottom: 24px; }
.chart-wrap { position: relative; height: 320px; }
.action-summary { background: var(--surface); border: 1px solid var(--border);
                   border-radius: 10px; padding: 12px 16px; margin: 18px 0; font-size: 0.88rem; }
.action-summary strong { color: var(--head); }
.action-summary .hint { color: var(--muted); font-size: 0.8rem; font-weight: 400; }
.chip-all { color: var(--muted); }
.legend { font-size: 0.76rem; color: var(--muted); margin-top: 12px; line-height: 1.6; }
.sortable { cursor: pointer; user-select: none; }
.sortable:hover { color: var(--link); }
.arrow { font-size: 0.7em; margin-left: 4px; color: var(--muted); }
.month-row { cursor: pointer; }
.month-row:hover td { background: var(--surface2); }
.filter-bar { font-size: 0.8rem; color: var(--muted); margin-bottom: 12px; }
.filter-bar strong { color: var(--link); }
.filter-bar .hint { color: var(--muted); }
"""

PORTFOLIO_JS = """
function filterAction(key) {
  var rows = document.querySelectorAll('#posTable tbody tr');
  rows.forEach(function(r) {
    var match = (key === 'all') || (r.getAttribute('data-action') === key);
    r.style.display = match ? '' : 'none';
  });
}
function filterMonth(key) {
  var rows = document.querySelectorAll('#tradeTable tbody tr');
  var shown = 0;
  rows.forEach(function(r) {
    var match = (key === 'all') || (r.getAttribute('data-month') === key);
    r.style.display = match ? '' : 'none';
    if (match) shown++;
  });
  var lbl = document.getElementById('monthLabel');
  if (lbl) lbl.textContent = (key === 'all') ? 'all months' : (key + ' (' + shown + ' trades)');
  var tt = document.getElementById('tradeTable');
  if (tt) tt.scrollIntoView({behavior: 'smooth', block: 'start'});
}
function sortTable(tableId, col, type, th) {
  var table = document.getElementById(tableId);
  var tbody = table.tBodies[0];
  var rows = Array.prototype.slice.call(tbody.querySelectorAll('tr'));
  var asc = th.getAttribute('data-asc') !== 'true';
  table.querySelectorAll('th .arrow').forEach(function(a) { a.textContent = ''; });
  th.querySelector('.arrow').textContent = asc ? ' ▲' : ' ▼';
  th.parentNode.querySelectorAll('th').forEach(function(h) { h.setAttribute('data-asc', 'false'); });
  th.setAttribute('data-asc', asc ? 'true' : 'false');
  rows.sort(function(a, b) {
    var x = a.children[col].getAttribute('data-sort') || a.children[col].textContent;
    var y = b.children[col].getAttribute('data-sort') || b.children[col].textContent;
    var cmp;
    if (type === 'num') { cmp = parseFloat(x) - parseFloat(y); }
    else { cmp = String(x).localeCompare(String(y)); }
    return asc ? cmp : -cmp;
  });
  rows.forEach(function(r) { tbody.appendChild(r); });
}
"""


def page_shell(title: str, h1: str, subtitle: str, body: str,
               extra_head: str = "", extra_script: str = "",
               nav: str = "") -> str:
    """Portfolio HTML skeleton — delegates to the shared theme shell."""
    return _theme_page_shell(
        title, nav, body, h1=h1, subtitle=subtitle,
        extra_head=extra_head, extra_css=PORTFOLIO_CSS,
        extra_script=PORTFOLIO_JS + "\n" + extra_script,
    )
