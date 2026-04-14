#!/usr/bin/env python3
"""
Watchlist page generator.

Reads data/watchlist.json and writes watchlist.html with:
  - Focus List (priority=focus, status=watching/focus) — actionable this week
  - Full Watchlist (status=watching, priority=watching)
  - Archived (collapsed)
  - CSV download button for TradingView import
"""

import json, os, glob, datetime, logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DATA_DIR          = os.environ.get("DATA_DIR", "data")
GITHUB_PAGES_BASE = os.environ.get("GITHUB_PAGES_BASE", "")
OUTPUT_PATH       = "watchlist.html"

FINVIZ_CHART      = "https://finviz.com/chart.ashx?t={ticker}&ty=c&ta=1&p=d"
FINVIZ_QUOTE      = "https://finviz.com/quote.ashx?t={ticker}"


def load_watchlist() -> list[dict]:
    path = os.path.join(DATA_DIR, "watchlist.json")
    with open(path) as f:
        return json.load(f).get("watchlist", [])


def load_latest_quality() -> dict:
    files = sorted(glob.glob(os.path.join(DATA_DIR, "daily_quality_*.json")))
    if not files:
        return {}
    with open(files[-1]) as f:
        return json.load(f)


def _days_on_list(added: str) -> str:
    try:
        d = datetime.date.fromisoformat(added)
        delta = (datetime.date.today() - d).days
        if delta == 0:
            return "today"
        elif delta == 1:
            return "1d"
        else:
            return f"{delta}d"
    except Exception:
        return ""


def _row(entry: dict, quality: dict, include_priority_badge: bool = False) -> str:
    ticker   = entry.get("ticker", "")
    added    = entry.get("added", "")
    thesis   = entry.get("thesis", "")
    note     = entry.get("entry_note", "")
    stop     = entry.get("stop")
    source   = entry.get("source", "manual")
    priority = entry.get("priority", "watching")
    age      = _days_on_list(added)

    q        = quality.get(ticker, {})
    q_rank   = q.get("q_rank", "")
    stage    = q.get("stage_label", "")

    chart_url = FINVIZ_CHART.format(ticker=ticker)
    quote_url = FINVIZ_QUOTE.format(ticker=ticker)

    stop_str = f"${stop}" if stop else "—"
    q_str    = str(q_rank) if q_rank else "—"
    src_badge = (
        '<span class="badge badge-auto">auto</span>' if source in ("screener_auto", "weekly_auto")
        else '<span class="badge badge-manual">manual</span>'
    )
    focus_badge = '<span class="badge badge-focus">FOCUS</span> ' if (priority == "focus" and include_priority_badge) else ""

    return f"""
    <tr>
      <td class="col-ticker">
        {focus_badge}<a href="{quote_url}" target="_blank" class="ticker-link">{ticker}</a>
      </td>
      <td class="col-note">{note}</td>
      <td class="col-thesis">{thesis}</td>
      <td class="col-stop">{stop_str}</td>
      <td class="col-q">{q_str}</td>
      <td class="col-stage">{stage}</td>
      <td class="col-age" data-added="{added}">{age}</td>
      <td class="col-src">{src_badge}</td>
      <td class="col-chart">
        <a href="{chart_url}" target="_blank" class="chart-link">chart ↗</a>
      </td>
    </tr>"""


def _table(entries: list[dict], quality: dict, table_id: str, include_priority_badge: bool = False) -> str:
    if not entries:
        return '<p class="empty-msg">No entries.</p>'
    rows = "".join(_row(e, quality, include_priority_badge) for e in entries)
    return f"""
    <table id="{table_id}" class="watchlist-table">
      <thead>
        <tr>
          <th>Ticker</th>
          <th>Setup note</th>
          <th>Thesis</th>
          <th>Stop</th>
          <th>Q</th>
          <th>Stage</th>
          <th>Age</th>
          <th>Source</th>
          <th>Chart</th>
        </tr>
      </thead>
      <tbody>{rows}
      </tbody>
    </table>"""


def generate(watchlist: list[dict], quality: dict) -> str:
    today       = datetime.date.today().isoformat()
    generated   = datetime.datetime.now(datetime.timezone.utc).strftime("%d %b %Y %H:%M UTC")
    index_url   = f"{GITHUB_PAGES_BASE}/index.html" if GITHUB_PAGES_BASE else "index.html"
    dash_url    = f"{GITHUB_PAGES_BASE}/dashboard.html" if GITHUB_PAGES_BASE else "dashboard.html"

    focus_list  = [e for e in watchlist if e.get("priority") == "focus"   and e.get("status") != "archived"]
    watching    = [e for e in watchlist if e.get("priority") != "focus"    and e.get("status") not in ("archived",)]
    archived    = [e for e in watchlist if e.get("status") == "archived"]

    # Sort: watching by added desc (newest first)
    watching.sort(key=lambda e: e.get("added", ""), reverse=True)
    archived.sort(key=lambda e: e.get("archived_date", e.get("added", "")), reverse=True)

    focus_table   = _table(focus_list, quality, "tbl-focus",   include_priority_badge=False)
    watching_table = _table(watching,  quality, "tbl-watching", include_priority_badge=False)
    archived_table = _table(archived,  quality, "tbl-archived", include_priority_badge=False)

    # All active tickers for CSV (focus first, then watching)
    active_all = focus_list + watching
    all_tickers_csv = ",".join(e.get("ticker", "") for e in active_all)

    n_focus    = len(focus_list)
    n_watching = len(watching)
    n_archived = len(archived)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Watchlist — {today}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #f9fafb; color: #111827; min-height: 100vh; }}

  .topbar {{ display: flex; align-items: center; gap: 16px; padding: 14px 28px;
             background: #fff; border-bottom: 1px solid #e5e7eb; flex-wrap: wrap; }}
  .topbar h1 {{ font-size: 1.1rem; font-weight: 700; color: #111827; flex: 1; }}
  .topbar-links {{ display: flex; gap: 10px; }}
  .topbar-links a {{ font-size: 0.8rem; color: #2563eb; text-decoration: none; padding: 5px 10px;
                     border: 1px solid #bfdbfe; border-radius: 6px; background: #eff6ff; }}
  .topbar-links a:hover {{ background: #dbeafe; }}

  .stats {{ display: flex; gap: 24px; padding: 16px 28px; background: #fff;
            border-bottom: 1px solid #e5e7eb; flex-wrap: wrap; }}
  .stat {{ display: flex; flex-direction: column; gap: 2px; }}
  .stat-val {{ font-size: 1.3rem; font-weight: 700; color: #111827; }}
  .stat-label {{ font-size: 0.7rem; color: #9ca3af; text-transform: uppercase; letter-spacing: .05em; }}

  .section {{ padding: 24px 28px; background: #fff; margin-top: 12px;
              border-top: 1px solid #e5e7eb; border-bottom: 1px solid #e5e7eb; }}
  .section-header {{ display: flex; align-items: center; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }}
  .section-header h2 {{ font-size: 0.82rem; font-weight: 700; color: #374151;
                        text-transform: uppercase; letter-spacing: .07em; }}
  .section-count {{ font-size: 0.75rem; color: #9ca3af; }}

  .csv-btn {{ margin-left: auto; padding: 6px 14px; font-size: 0.78rem; font-weight: 600;
              color: #15803d; background: #f0fdf4; border: 1px solid #bbf7d0;
              border-radius: 6px; cursor: pointer; text-decoration: none; }}
  .csv-btn:hover {{ background: #dcfce7; }}

  .watchlist-table {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; }}
  .watchlist-table th {{ text-align: left; padding: 8px 10px; font-size: 0.7rem; font-weight: 700;
                         text-transform: uppercase; letter-spacing: .05em; color: #6b7280;
                         border-bottom: 2px solid #e5e7eb; }}
  .watchlist-table td {{ padding: 9px 10px; border-bottom: 1px solid #f3f4f6; vertical-align: top; }}
  .watchlist-table tr:hover td {{ background: #f9fafb; }}

  .col-ticker  {{ width: 90px; white-space: nowrap; }}
  .col-note    {{ width: 200px; color: #374151; }}
  .col-thesis  {{ color: #6b7280; font-size: 0.77rem; }}
  .col-stop    {{ width: 60px; text-align: right; color: #dc2626; font-weight: 600; white-space: nowrap; }}
  .col-q       {{ width: 40px; text-align: right; font-weight: 600; color: #111827; }}
  .col-stage   {{ width: 80px; color: #6b7280; font-size: 0.75rem; }}
  .col-age     {{ width: 50px; text-align: right; color: #9ca3af; font-size: 0.75rem; }}
  .col-src     {{ width: 70px; }}
  .col-chart   {{ width: 60px; text-align: right; }}

  .ticker-link {{ font-weight: 700; color: #2563eb; text-decoration: none; }}
  .ticker-link:hover {{ text-decoration: underline; }}
  .chart-link {{ color: #2563eb; font-size: 0.75rem; text-decoration: none; }}
  .chart-link:hover {{ text-decoration: underline; }}

  .badge {{ display: inline-block; padding: 1px 6px; border-radius: 4px;
            font-size: 0.65rem; font-weight: 700; letter-spacing: .04em; }}
  .badge-focus  {{ background: #fef3c7; color: #92400e; border: 1px solid #fde68a; }}
  .badge-auto   {{ background: #eff6ff; color: #1d4ed8; border: 1px solid #bfdbfe; }}
  .badge-manual {{ background: #f0fdf4; color: #15803d; border: 1px solid #bbf7d0; }}

  .focus-section {{ background: #fffbeb; border-color: #fde68a; }}
  .focus-section .section-header h2 {{ color: #92400e; }}

  details summary {{ cursor: pointer; list-style: none; }}
  details summary::-webkit-details-marker {{ display: none; }}
  .archived-toggle {{ display: inline-flex; align-items: center; gap: 6px; font-size: 0.8rem;
                      color: #9ca3af; padding: 5px 10px; border: 1px solid #e5e7eb;
                      border-radius: 6px; cursor: pointer; }}
  .archived-toggle:hover {{ background: #f9fafb; }}
  .archived-section {{ padding: 16px 28px; background: #f9fafb; border-top: 1px solid #e5e7eb; }}

  .empty-msg {{ color: #9ca3af; font-size: 0.82rem; font-style: italic; padding: 12px 0; }}

  .footer {{ padding: 16px 28px; font-size: 0.7rem; color: #9ca3af; border-top: 1px solid #e5e7eb;
             background: #fff; margin-top: 12px; }}
</style>
</head>
<body>

<div class="topbar">
  <h1>📋 Watchlist — {today}</h1>
  <div class="topbar-links">
    <a href="{index_url}">← Home</a>
    <a href="{dash_url}">Dashboard</a>
  </div>
</div>

<div class="stats">
  <div class="stat">
    <span class="stat-val">{n_focus}</span>
    <span class="stat-label">Focus (act now)</span>
  </div>
  <div class="stat">
    <span class="stat-val">{n_watching}</span>
    <span class="stat-label">Watching</span>
  </div>
  <div class="stat">
    <span class="stat-val">{n_archived}</span>
    <span class="stat-label">Archived</span>
  </div>
  <div class="stat">
    <span class="stat-val">{today}</span>
    <span class="stat-label">Last updated</span>
  </div>
</div>

<!-- FOCUS LIST -->
<div class="section focus-section">
  <div class="section-header">
    <h2>📌 Focus List</h2>
    <span class="section-count">{n_focus} ticker{"s" if n_focus != 1 else ""} — actionable this week</span>
    <button class="csv-btn" onclick="downloadCSV('tbl-focus', 'focus_list_{today}.csv')">
      ⬇ Download CSV
    </button>
  </div>
  {focus_table}
</div>

<!-- FULL WATCHLIST -->
<div class="section">
  <div class="section-header">
    <h2>👁 Watching</h2>
    <span class="section-count">{n_watching} ticker{"s" if n_watching != 1 else ""} — on radar, not yet actionable</span>
    <button class="csv-btn" onclick="downloadAllActive('{all_tickers_csv}', 'watchlist_{today}.txt')">
      ⬇ Export all for TradingView
    </button>
  </div>
  {watching_table}
</div>

<!-- ARCHIVED (collapsed) -->
<div class="archived-section">
  <details>
    <summary>
      <span class="archived-toggle">🗃 Archived ({n_archived}) — click to expand</span>
    </summary>
    <div style="margin-top:16px">
      {archived_table}
    </div>
  </details>
</div>

<div class="footer">
  Generated {generated} · Auto-archive: screener_auto entries expire after 14 days ·
  Promote to Focus via position-monitor workflow_dispatch (watchlist_action=focus)
</div>

<script>
function downloadCSV(tableId, filename) {{
  const table = document.getElementById(tableId);
  if (!table) {{ alert('No data in this section.'); return; }}
  const rows = table.querySelectorAll('tr');
  const csv = Array.from(rows).map(row => {{
    const cells = row.querySelectorAll('th, td');
    return Array.from(cells).map(c => {{
      const text = c.innerText.trim().replace(/,/g, ';').replace(/\\n/g, ' ');
      return text;
    }}).join(',');
  }}).join('\\n');
  _triggerDownload(csv, filename);
}}

function downloadAllActive(tickers, filename) {{
  // TradingView-compatible: one ticker per line
  const lines = tickers.split(',').filter(t => t.trim()).join('\\n');
  _triggerDownload(lines, filename);
}}

function _triggerDownload(content, filename) {{
  const blob = new Blob([content], {{ type: 'text/csv' }});
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement('a');
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click();
  document.body.removeChild(a); URL.revokeObjectURL(url);
}}
</script>

</body>
</html>"""


def main():
    log.info("=== Watchlist generator starting ===")
    watchlist = load_watchlist()
    quality   = load_latest_quality()
    html      = generate(watchlist, quality)
    with open(OUTPUT_PATH, "w") as f:
        f.write(html)
    log.info("watchlist.html written → %s (%d entries)", OUTPUT_PATH, len(watchlist))


if __name__ == "__main__":
    main()
