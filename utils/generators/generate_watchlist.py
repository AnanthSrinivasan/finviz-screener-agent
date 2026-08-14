#!/usr/bin/env python3
"""
Watchlist page generator.

Reads data/watchlist.json and data/hidden_growth.json and writes watchlist.html with:
  - Entry-Ready (priority=entry-ready) — pullback-to-support setups, act now
  - Focus List (priority=focus) — actionable this week
  - Full Watchlist (status=watching, priority=watching) — on radar
  - Hidden Growth Today (from hidden_growth.json) — research prompts, overlaps with tiers
  - Archived (collapsed)
  - CSV download button for TradingView import
"""

import json, os, glob, datetime, logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DATA_DIR          = os.environ.get("DATA_DIR", "data")
GITHUB_PAGES_BASE = os.environ.get("GITHUB_PAGES_BASE", "")

# See generate_dashboard.py — auto cache-bust query for internal nav links.
def _cache_q() -> str:
    sha = os.environ.get("GITHUB_SHA") or os.environ.get("CACHE_BUST_SHA") or ""
    if not sha:
        try:
            import subprocess
            sha = subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"], text=True
            ).strip()
        except Exception:
            sha = ""
    return f"?v={sha[:7]}" if sha else ""

CACHE_Q = _cache_q()
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


def load_hidden_growth() -> dict:
    """Load today's Hidden Growth snapshot. Returns {date, candidates}, or empty."""
    path = os.path.join(DATA_DIR, "hidden_growth.json")
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"date": "", "candidates": []}


# Human-readable labels for each Hidden Growth criterion key
_HG_CRITERIA_LABELS = {
    "persistence":    "3+d",
    "eps_yy_strong":  "TTM+",
    "eps_qq_strong":  "Q/Q+",
    "inst_buying":    "Inst+",
    "stage2_perfect": "S2",
    "ipo_lifecycle":  "IPO",
}


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
    source   = entry.get("source", "manual") or "manual"
    priority = entry.get("priority", "watching")
    age      = _days_on_list(added)

    q         = quality.get(ticker, {})
    q_rank    = q.get("q_rank", "")
    stage     = q.get("stage_label", "")
    textbook  = q.get("textbook_vcp", False)

    chart_url = FINVIZ_CHART.format(ticker=ticker)
    quote_url = FINVIZ_QUOTE.format(ticker=ticker)

    stop_str = f"${stop}" if stop else "—"
    q_str    = str(q_rank) if q_rank else "—"
    # Any *_auto source (screener, hidden-growth, breakout, rs-leader, rotation,
    # stage-transition, ema21-pb, htf-base-reclaim, …) was added by the system.
    # Only a literal "manual" source is hand-entered.
    src_badge = (
        '<span class="badge badge-manual">manual</span>' if source == "manual"
        else '<span class="badge badge-auto">auto</span>'
    )
    focus_badge = '<span class="badge badge-focus">FOCUS</span> ' if (priority == "focus" and include_priority_badge) else ""
    textbook_badge = '<span class="badge badge-textbook" title="Textbook VCP — all criteria aligned">⭐</span> ' if textbook else ""

    return f"""
    <tr>
      <td class="col-ticker">
        {focus_badge}{textbook_badge}<a href="{quote_url}" target="_blank" class="ticker-link">{ticker}</a>
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


def _hg_row(candidate: dict, watchlist_tier_by_ticker: dict[str, str]) -> str:
    ticker   = candidate.get("ticker", "")
    score    = candidate.get("signal_score", 0)
    criteria = candidate.get("criteria", {}) or {}
    eps_yy   = candidate.get("eps_yy_ttm", 0) or 0
    eps_qq   = candidate.get("eps_qq", 0) or 0
    inst     = candidate.get("inst_trans", 0) or 0
    appear   = candidate.get("appearances", 0) or 0

    tier = watchlist_tier_by_ticker.get(ticker)
    tier_badge = ""
    if tier == "entry-ready":
        tier_badge = '<span class="badge badge-er">ENTRY-READY</span> '
    elif tier == "focus":
        tier_badge = '<span class="badge badge-focus">FOCUS</span> '
    elif tier == "watching":
        tier_badge = '<span class="badge badge-watch">WATCH</span> '
    # else: not in watchlist yet (research-only HG hit)

    distorted = eps_yy < -50 and eps_qq > 0
    eps_tag = f"TTM {eps_yy:+.0f}% / Q/Q {eps_qq:+.0f}%" + (" ⚠" if distorted else "")

    # Criteria checklist badges — lit when True
    crit_pills = ""
    for key, label in _HG_CRITERIA_LABELS.items():
        lit = bool(criteria.get(key))
        cls = "hg-pill hg-pill-on" if lit else "hg-pill"
        crit_pills += f'<span class="{cls}">{label}</span>'

    chart_url = FINVIZ_CHART.format(ticker=ticker)
    quote_url = FINVIZ_QUOTE.format(ticker=ticker)

    return f"""
    <tr>
      <td class="col-ticker">
        {tier_badge}<a href="{quote_url}" target="_blank" class="ticker-link">{ticker}</a>
      </td>
      <td class="col-hg-score"><strong>{score}/6</strong></td>
      <td class="col-hg-crit">{crit_pills}</td>
      <td class="col-hg-eps">{eps_tag}</td>
      <td class="col-hg-inst">{inst:+.1f}%</td>
      <td class="col-hg-appear">{appear}d</td>
      <td class="col-chart">
        <a href="{chart_url}" target="_blank" class="chart-link">chart ↗</a>
      </td>
    </tr>"""


def _hg_table(candidates: list[dict], watchlist_tier_by_ticker: dict[str, str]) -> str:
    if not candidates:
        return '<p class="empty-msg">No Hidden Growth candidates today.</p>'
    rows = "".join(_hg_row(c, watchlist_tier_by_ticker) for c in candidates)
    return f"""
    <table id="tbl-hidden-growth" class="watchlist-table">
      <thead>
        <tr>
          <th>Ticker</th>
          <th>Score</th>
          <th>Criteria</th>
          <th>EPS</th>
          <th>Inst Trans</th>
          <th>Screens</th>
          <th>Chart</th>
        </tr>
      </thead>
      <tbody>{rows}
      </tbody>
    </table>"""


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


# Setup categories for the browse grid (2026-08-14 redesign — user asked for a
# short act-now list + break-by-setup instead of a 200-name flat wall). Ordered
# tightest / most-actionable first. (source_key, emoji, label). A watchlist row's
# category is its `source` (the signal that put it there).
CATEGORY_ORDER = [
    ("breakout_auto",          "🚀", "Fresh Breakout"),
    ("ema21_pb_auto",          "🎯", "21 EMA Pullback"),
    ("rs_leader_auto",         "🛡️", "RS Leader"),
    ("stage_transition_auto",  "🌱", "Stage Transition"),
    ("rotation_catalyst_auto", "🌊", "Rotation Catalyst"),
    ("htf_base_reclaim_auto",  "🌀", "HTF Base Reclaim"),
    ("hidden_growth_auto",     "🔬", "Hidden Growth"),
    ("recovery_leader_auto",   "🐉", "Recovery Leader"),
    ("episodic_pivot_auto",    "⚡", "Episodic Pivot"),
    ("screener_auto",          "📊", "Screener (technical)"),
    ("weekly_auto",            "📅", "Weekly"),
    ("manual",                 "✋", "Manual"),
]
CATEGORY_CAP = 3  # names shown per setup category (rest stay tracked, off-page)


def _q_of(ticker: str, quality: dict) -> float:
    """Numeric Quality Score for ranking; -1 when the name has no fresh screener
    quality (so unscored names sort last within a category)."""
    q = quality.get(ticker, {}) or {}
    v = q.get("q_rank")
    if v is None:
        v = q.get("quality_score")
    try:
        return float(v)
    except (TypeError, ValueError):
        return -1.0


def _category_card(emoji: str, label: str, entries: list[dict], quality: dict, cap: int) -> str:
    """One compact setup-category card: top `cap` names by Quality Score."""
    ranked = sorted(entries, key=lambda e: _q_of(e.get("ticker", ""), quality), reverse=True)
    shown = ranked[:cap]
    rows = ""
    for e in shown:
        t = e.get("ticker", "")
        qd = quality.get(t, {}) or {}
        qv = _q_of(t, quality)
        q_str = str(int(qv)) if qv >= 0 else "—"
        stage = qd.get("stage_label", "")
        star = "⭐ " if qd.get("textbook_vcp") else ""
        age = _days_on_list(e.get("added", ""))
        rows += f"""<div class="cat-row">
          <a href="{FINVIZ_QUOTE.format(ticker=t)}" target="_blank" class="cat-tk">{star}{t}</a>
          <span class="cat-q">{q_str}</span>
          <span class="cat-stage">{stage}</span>
          <span class="cat-age">{age}</span>
          <a href="{FINVIZ_CHART.format(ticker=t)}" target="_blank" class="cat-ch" title="chart">↗</a>
        </div>"""
    more = len(ranked) - len(shown)
    more_html = f'<div class="cat-more">+{more} more tracked (off-page)</div>' if more > 0 else ""
    return f"""<div class="cat-card">
      <div class="cat-head">{emoji} {label} <span class="cat-n">{len(ranked)}</span></div>
      <div class="cat-rows">{rows}</div>
      {more_html}
    </div>"""


def _category_grid(entries: list[dict], quality: dict, cap: int = CATEGORY_CAP) -> tuple[str, int]:
    """Group active (non-entry-ready) rows by source into ordered setup cards.
    Returns (grid_html, n_categories)."""
    by_source: dict[str, list[dict]] = {}
    for e in entries:
        by_source.setdefault(e.get("source") or "manual", []).append(e)
    cards = ""
    n = 0
    for key, emoji, label in CATEGORY_ORDER:
        grp = by_source.get(key)
        if grp:
            cards += _category_card(emoji, label, grp, quality, cap)
            n += 1
    # future-proof: any unknown source still gets a card
    known = {k for k, _, _ in CATEGORY_ORDER}
    for key, grp in by_source.items():
        if key not in known and grp:
            cards += _category_card("•", key, grp, quality, cap)
            n += 1
    if not cards:
        cards = '<p class="empty-msg">No setups tracked right now.</p>'
    return f'<div class="cat-grid">{cards}</div>', n


def generate(watchlist: list[dict], quality: dict, hidden_growth: dict | None = None) -> str:
    from utils.generators.nav import render_nav
    from utils.generators.theme import BASE_CSS

    today       = datetime.date.today().isoformat()
    generated   = datetime.datetime.now(datetime.timezone.utc).strftime("%d %b %Y %H:%M UTC")
    nav_html    = render_nav("watchlist", at_root=True)

    # Entry-ready is the act-now list; everything else active is grouped by
    # setup category (2026-08-14 redesign). Watching + focus both feed the grid.
    entry_ready = [e for e in watchlist if e.get("priority") == "entry-ready" and e.get("status") != "archived"]
    grid_source = [e for e in watchlist if e.get("priority") != "entry-ready"  and e.get("status") != "archived"]
    archived    = [e for e in watchlist if e.get("status") == "archived"]

    entry_ready.sort(key=lambda e: _q_of(e.get("ticker", ""), quality), reverse=True)
    archived.sort(key=lambda e: e.get("archived_date", e.get("added", "")), reverse=True)

    entry_ready_table    = _table(entry_ready, quality, "tbl-entry-ready", include_priority_badge=False)
    category_grid, n_cat = _category_grid(grid_source, quality)
    archived_table       = _table(archived, quality, "tbl-archived", include_priority_badge=False)

    # Hidden Growth section — reads today's snapshot, annotates with tier if ticker is on watchlist
    hg = hidden_growth or {"date": "", "candidates": []}
    hg_candidates = hg.get("candidates", []) or []
    tier_by_ticker: dict[str, str] = {}
    for e in watchlist:
        if e.get("status") == "archived":
            continue
        tier_by_ticker[e.get("ticker", "")] = e.get("priority", "watching")
    hg_table = _hg_table(hg_candidates, tier_by_ticker)
    hg_date  = hg.get("date") or today

    # All active tickers for CSV (entry-ready first, then the rest of the grid)
    active_all = entry_ready + grid_source
    all_tickers_csv         = ",".join(e.get("ticker", "") for e in active_all)
    entry_ready_tickers_csv = ",".join(e.get("ticker", "") for e in entry_ready)
    hg_tickers_csv          = ",".join(c.get("ticker", "") for c in hg_candidates)

    n_entry_ready = len(entry_ready)
    n_active      = len(active_all)
    n_archived    = len(archived)
    n_hg          = len(hg_candidates)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>Watchlist — {today}</title>
<style>
{BASE_CSS}
  /* Watchlist-specific — thin extension of BASE_CSS (cx-rehaul §4) */
  .stats {{ display: flex; gap: 24px; padding: 12px 0; flex-wrap: wrap; }}
  .stat {{ display: flex; flex-direction: column; gap: 2px; }}
  .stat-val {{ font-size: 1.3rem; font-weight: 700; color: var(--head); }}
  .stat-label {{ font-size: 0.7rem; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; }}

  .section {{ padding: 16px; background: var(--surface); border: 1px solid var(--border);
              border-left-width: 4px; border-radius: 10px; margin-top: 14px; }}
  .section-header {{ display: flex; align-items: center; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }}
  .section-header h2 {{ margin: 0; color: var(--head); }}
  .section-count {{ font-size: 0.75rem; color: var(--muted); }}

  .csv-btn {{ margin-left: auto; padding: 6px 14px; font-size: 0.78rem; font-weight: 600;
              color: var(--green-text); background: var(--green-bg); border: 1px solid var(--border);
              border-radius: 6px; cursor: pointer; }}
  .csv-btn:hover {{ border-color: var(--green-text); }}

  .watchlist-table {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; }}
  .watchlist-table th {{ text-align: left; padding: 8px 10px; font-size: 0.68rem; font-weight: 700;
                         text-transform: uppercase; letter-spacing: .05em; color: var(--muted);
                         border-bottom: 2px solid var(--border); }}
  .watchlist-table td {{ padding: 8px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }}
  .watchlist-table tr:hover td {{ background: var(--surface2); }}

  .col-ticker  {{ width: 90px; white-space: nowrap; }}
  .col-note    {{ width: 200px; color: var(--text); }}
  .col-thesis  {{ color: var(--muted); font-size: 0.77rem; }}
  .col-stop    {{ width: 60px; text-align: right; color: var(--red); font-weight: 600; white-space: nowrap; }}
  .col-q       {{ width: 40px; text-align: right; font-weight: 600; color: var(--head); }}
  .col-stage   {{ width: 80px; color: var(--muted); font-size: 0.75rem; }}
  .col-age     {{ width: 50px; text-align: right; color: var(--muted); font-size: 0.75rem; }}
  .col-src     {{ width: 70px; }}
  .col-chart   {{ width: 60px; text-align: right; }}

  .ticker-link {{ font-weight: 700; }}
  .chart-link {{ font-size: 0.75rem; }}

  .badge-focus  {{ background: var(--amber-bg); color: var(--amber); }}
  .badge-er     {{ background: var(--green-bg); color: var(--green-text); }}
  .badge-watch  {{ background: var(--surface2); color: var(--muted); }}
  .badge-auto   {{ background: var(--blue-bg); color: var(--blue-text); }}
  .badge-manual {{ background: var(--green-bg); color: var(--green-text); }}
  .badge-textbook {{ background: var(--amber-bg); color: var(--amber); }}

  .entry-ready-section {{ border-left-color: var(--green); }}
  .focus-section {{ border-left-color: var(--amber); }}
  .hg-section {{ border-left-color: #a78bfa; }}

  /* Hidden Growth criteria pills */
  .hg-pill {{ display: inline-block; padding: 1px 5px; margin-right: 3px;
              font-size: 0.6rem; font-weight: 600; border-radius: 3px;
              background: var(--surface2); color: var(--muted); border: 1px solid var(--border); }}
  .hg-pill-on {{ background: #2b2350; color: #c4b5fd; border-color: #7c6bd6; }}

  .col-hg-score  {{ width: 45px; text-align: center; color: #c4b5fd; }}
  .col-hg-crit   {{ width: 260px; }}
  .col-hg-eps    {{ width: 210px; color: var(--text); font-size: 0.75rem; white-space: nowrap; }}
  .col-hg-inst   {{ width: 70px; text-align: right; color: var(--text); font-size: 0.78rem; }}
  .col-hg-appear {{ width: 55px; text-align: right; color: var(--muted); font-size: 0.75rem; }}

  .empty-msg {{ color: var(--muted); font-size: 0.82rem; font-style: italic; padding: 12px 0; }}

  /* Setups-by-category grid (2026-08-14 redesign) */
  .setups-section {{ border-left-color: var(--blue-text, #60a5fa); }}
  .cat-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 14px; }}
  .cat-card {{ background: var(--surface2); border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; }}
  .cat-head {{ display: flex; align-items: center; gap: 6px; font-weight: 700; color: var(--head);
               font-size: 0.9rem; margin-bottom: 8px; }}
  .cat-n {{ margin-left: auto; font-size: 0.68rem; font-weight: 600; color: var(--muted);
            background: var(--surface); border: 1px solid var(--border); padding: 1px 8px; border-radius: 10px; }}
  .cat-rows {{ display: flex; flex-direction: column; }}
  .cat-row {{ display: grid; grid-template-columns: 1fr 34px 82px 42px 16px; align-items: center; gap: 6px;
              padding: 5px 2px; border-bottom: 1px solid var(--border); font-size: 0.8rem; }}
  .cat-row:last-child {{ border-bottom: none; }}
  .cat-tk {{ font-weight: 700; white-space: nowrap; }}
  .cat-q {{ text-align: right; font-weight: 600; color: var(--head); }}
  .cat-stage {{ color: var(--muted); font-size: 0.72rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .cat-age {{ text-align: right; color: var(--muted); font-size: 0.72rem; }}
  .cat-ch {{ text-align: right; font-size: 0.8rem; }}
  .cat-more {{ margin-top: 8px; font-size: 0.7rem; color: var(--muted); font-style: italic; }}
</style>
</head>
<body>
{nav_html}
<h1>📋 Watchlist — {today}</h1>

<div class="stats">
  <div class="stat">
    <span class="stat-val">{n_entry_ready}</span>
    <span class="stat-label">Ready to Trade</span>
  </div>
  <div class="stat">
    <span class="stat-val">{n_cat}</span>
    <span class="stat-label">Setup Categories</span>
  </div>
  <div class="stat">
    <span class="stat-val">{n_active}</span>
    <span class="stat-label">Tracked (active)</span>
  </div>
  <div class="stat">
    <span class="stat-val">{n_hg}</span>
    <span class="stat-label">Hidden Growth</span>
  </div>
  <div class="stat">
    <span class="stat-val">{n_archived}</span>
    <span class="stat-label">Archived</span>
  </div>
</div>

<!-- READY TO TRADE (the act-now list) -->
<div class="section entry-ready-section">
  <div class="section-header">
    <h2>🎯 Ready to Trade</h2>
    <span class="section-count">{n_entry_ready} ticker{"s" if n_entry_ready != 1 else ""} — passing the tightest gate (Stage 2 + VCP tight pullback, not extended). Buy candidates now.</span>
    <button class="csv-btn" onclick="downloadAllActive('{entry_ready_tickers_csv}', 'ready_to_trade_tv_{today}.txt')">
      ⬇ TradingView
    </button>
    <button class="csv-btn" onclick="downloadCSV('tbl-entry-ready', 'ready_to_trade_{today}.csv')">
      ⬇ CSV
    </button>
  </div>
  {entry_ready_table}
</div>

<!-- SETUPS BY CATEGORY (browse — top {CATEGORY_CAP} by Quality per setup) -->
<div class="section setups-section">
  <div class="section-header">
    <h2>🗂 Setups by Category</h2>
    <span class="section-count">Top {CATEGORY_CAP} by Quality Score per setup — the number badge is the full count still tracked. Pick the setup type you trade.</span>
    <button class="csv-btn" onclick="downloadAllActive('{all_tickers_csv}', 'watchlist_all_active_{today}.txt')">
      ⬇ Export all active
    </button>
  </div>
  {category_grid}
</div>

<!-- HIDDEN GROWTH TODAY (fundamental lens — richer detail than the category card) -->
<div class="section hg-section">
  <div class="section-header">
    <h2>🔬 Hidden Growth — {hg_date}</h2>
    <span class="section-count">{n_hg} research candidate{"s" if n_hg != 1 else ""} — today's fundamental-accumulation fires with criteria breakdown</span>
    <button class="csv-btn" onclick="downloadAllActive('{hg_tickers_csv}', 'hidden_growth_tv_{today}.txt')">
      ⬇ TradingView
    </button>
  </div>
  {hg_table}
</div>

<!-- ARCHIVED (collapsed) -->
<details>
  <summary>🗃 Archived ({n_archived})</summary>
  <div style="margin-top:12px">
    {archived_table}
  </div>
</details>

<div class="footer">
  Generated {generated} · Ready-to-Trade = entry-ready gate · categories capped at top {CATEGORY_CAP} by Q (rest tracked off-page) ·
  Auto-archive: watching ages out at 14 days, focus cold-archives at 15 trading days absent from the screener
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
    watchlist     = load_watchlist()
    quality       = load_latest_quality()
    hidden_growth = load_hidden_growth()
    html          = generate(watchlist, quality, hidden_growth)
    with open(OUTPUT_PATH, "w") as f:
        f.write(html)
    log.info(
        "watchlist.html written → %s (%d watchlist entries, %d hidden growth)",
        OUTPUT_PATH, len(watchlist), len(hidden_growth.get("candidates", []) or []),
    )


if __name__ == "__main__":
    main()
