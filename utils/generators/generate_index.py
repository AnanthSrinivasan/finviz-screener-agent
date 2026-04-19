#!/usr/bin/env python3
# ----------------------------
# Finviz Index Page Generator
# ----------------------------
# Scans the data/ folder and regenerates index.html at the repo root.
# Run at the end of both daily and weekly workflows.
# ----------------------------

import os
import re
import glob
import datetime
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DATA_DIR        = os.environ.get("DATA_DIR", "data")
GITHUB_PAGES_BASE = os.environ.get("GITHUB_PAGES_BASE", "")
OUTPUT_PATH     = "index.html"


def scan_reports(data_dir: str) -> dict:
    """Scan data/ folder and return structured report list."""
    reports = {
        "weekly": [],
        "daily_gallery": [],
        "daily_summary": [],
        "persistence": [],
    }

    # Weekly HTML reports
    for f in sorted(glob.glob(os.path.join(data_dir, "finviz_weekly_2*.html")), reverse=True):
        date = _extract_date(f)
        reports["weekly"].append({"date": date, "file": os.path.basename(f), "path": f})

    # Daily chart galleries
    for f in sorted(glob.glob(os.path.join(data_dir, "finviz_chart_grid_2*.html")), reverse=True):
        date = _extract_date(f)
        reports["daily_gallery"].append({"date": date, "file": os.path.basename(f), "path": f})

    # Daily screener summaries
    for f in sorted(glob.glob(os.path.join(data_dir, "finviz_screeners_2*.html")), reverse=True):
        date = _extract_date(f)
        reports["daily_summary"].append({"date": date, "file": os.path.basename(f), "path": f})

    # Weekly persistence CSVs
    for f in sorted(glob.glob(os.path.join(data_dir, "finviz_weekly_persistence_2*.csv")), reverse=True):
        date = _extract_date(f)
        reports["persistence"].append({"date": date, "file": os.path.basename(f), "path": f})

    return reports


def _extract_date(filepath: str) -> str:
    match = re.search(r"(\d{4}-\d{2}-\d{2})", filepath)
    return match.group(1) if match else "unknown"


def _format_date(date_str: str) -> str:
    try:
        d = datetime.date.fromisoformat(date_str)
        return d.strftime("%a %d %b %Y")
    except:
        return date_str


def _days_ago(date_str: str) -> str:
    try:
        d = datetime.date.fromisoformat(date_str)
        delta = (datetime.date.today() - d).days
        if delta == 0:
            return "today"
        elif delta == 1:
            return "yesterday"
        else:
            return f"{delta}d ago"
    except:
        return ""


def generate_index(reports: dict, base_url: str) -> str:
    today = datetime.date.today().strftime("%Y-%m-%d")
    generated_at = datetime.datetime.now().strftime("%d %b %Y %H:%M UTC")

    # Latest report cards
    latest_weekly  = reports["weekly"][0]  if reports["weekly"]  else None
    latest_gallery = reports["daily_gallery"][0] if reports["daily_gallery"] else None

    def report_url(report):
        return f"{base_url}/data/{report['file']}" if base_url else f"data/{report['file']}"

    # Build weekly cards
    weekly_cards = ""
    for r in reports["weekly"][:8]:
        weekly_cards += f"""
        <a href="{report_url(r)}" class="card card-weekly">
          <div class="card-label">Weekly Review</div>
          <div class="card-date">{_format_date(r['date'])}</div>
          <div class="card-ago">{_days_ago(r['date'])}</div>
        </a>"""

    # Build daily gallery cards
    gallery_cards = ""
    for r in reports["daily_gallery"][:10]:
        gallery_cards += f"""
        <a href="{report_url(r)}" class="card card-daily">
          <div class="card-label">Chart Gallery</div>
          <div class="card-date">{_format_date(r['date'])}</div>
          <div class="card-ago">{_days_ago(r['date'])}</div>
        </a>"""

    # Latest links for hero section
    dashboard_url = f"{base_url}/dashboard.html" if base_url else "dashboard.html"

    perf_url      = f"{base_url}/data/performance_charts.html" if base_url else "data/performance_charts.html"
    mae_url       = f"{base_url}/data/mae_analysis.html" if base_url else "data/mae_analysis.html"
    watchlist_url = f"{base_url}/watchlist.html" if base_url else "watchlist.html"
    portfolio_url = f"{base_url}/data/claude_portfolio.html" if base_url else "data/claude_portfolio.html"

    hero_links = f'<a href="{dashboard_url}" class="hero-btn btn-dash">Dashboard</a>'
    hero_links += f'<a href="{portfolio_url}" class="hero-btn btn-portfolio">Claude Portfolio</a>'
    hero_links += f'<a href="{watchlist_url}" class="hero-btn btn-watchlist">Watchlist</a>'
    if latest_weekly:
        hero_links += f'<a href="{report_url(latest_weekly)}" class="hero-btn btn-weekly">Latest Weekly Review</a>'
    if latest_gallery:
        hero_links += f'<a href="{report_url(latest_gallery)}" class="hero-btn btn-daily">Latest Chart Gallery</a>'
    hero_links += f'<a href="{perf_url}" class="hero-btn btn-perf">Performance Overview</a>'
    hero_links += f'<a href="{mae_url}" class="hero-btn btn-mae">MAE / MFE Analysis</a>'

    total_daily  = len(reports["daily_gallery"])
    total_weekly = len(reports["weekly"])

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Finviz Screener Agent</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #f8f9fc; color: #111827; min-height: 100vh; }}

  /* Hero */
  .hero {{ padding: 48px 32px 40px; border-bottom: 1px solid #e5e7eb; background: #fff; }}
  .hero h1 {{ font-size: 1.6rem; font-weight: 700; margin-bottom: 6px; color: #111827; }}
  .hero p {{ color: #6b7280; font-size: 0.88rem; margin-bottom: 24px; }}
  .hero-links {{ display: flex; gap: 12px; flex-wrap: wrap; }}
  .hero-btn {{ display: inline-flex; align-items: center; gap: 6px;
               padding: 10px 20px; border-radius: 8px; font-size: 0.88rem;
               font-weight: 600; text-decoration: none; transition: opacity .15s; }}
  .hero-btn:hover {{ opacity: .85; }}
  .btn-weekly {{ background: #eff6ff; color: #1d4ed8; border: 1px solid #bfdbfe; }}
  .btn-daily  {{ background: #f0fdf4; color: #15803d; border: 1px solid #bbf7d0; }}
  .btn-dash   {{ background: #faf5ff; color: #7c3aed; border: 1px solid #ddd6fe; }}
  .btn-perf   {{ background: #fff7ed; color: #c2410c; border: 1px solid #fed7aa; }}
  .btn-mae       {{ background: #fdf2f8; color: #9d174d; border: 1px solid #fbcfe8; }}
  .btn-watchlist {{ background: #f0fdf4; color: #15803d; border: 1px solid #bbf7d0; }}
  .btn-portfolio {{ background: #ecfeff; color: #0e7490; border: 1px solid #a5f3fc; }}

  /* Stats bar */
  .stats {{ display: flex; gap: 32px; padding: 20px 32px;
            border-bottom: 1px solid #e5e7eb; flex-wrap: wrap; background: #fff; }}
  .stat {{ display: flex; flex-direction: column; gap: 2px; }}
  .stat-val {{ font-size: 1.4rem; font-weight: 700; color: #111827; }}
  .stat-label {{ font-size: 0.72rem; color: #9ca3af; text-transform: uppercase; letter-spacing: .05em; }}

  /* Sections */
  .section {{ padding: 28px 32px; background: #fff; }}
  .section + .section {{ border-top: 1px solid #e5e7eb; }}
  .section h2 {{ font-size: 0.78rem; font-weight: 700; color: #6b7280;
                 text-transform: uppercase; letter-spacing: .08em; margin-bottom: 16px; }}

  /* Cards */
  .cards {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 10px; }}
  .card {{ display: flex; flex-direction: column; gap: 4px; padding: 14px 16px;
           border-radius: 10px; text-decoration: none; transition: border-color .15s, box-shadow .15s;
           border: 1px solid #e5e7eb; background: #fff; }}
  .card:hover {{ border-color: #2563eb; box-shadow: 0 2px 8px rgba(37,99,235,0.08); }}
  .card-weekly {{ background: #eff6ff; border-color: #dbeafe; }}
  .card-daily  {{ background: #f0fdf4; border-color: #dcfce7; }}
  .card-label {{ font-size: 0.68rem; text-transform: uppercase; letter-spacing: .06em; font-weight: 700; }}
  .card-weekly .card-label {{ color: #1d4ed8; }}
  .card-daily  .card-label {{ color: #15803d; }}
  .card-date {{ font-size: 0.88rem; font-weight: 600; color: #111827; }}
  .card-ago  {{ font-size: 0.72rem; color: #9ca3af; }}

  /* Footer */
  .footer {{ padding: 20px 32px; border-top: 1px solid #e5e7eb;
             font-size: 0.72rem; color: #9ca3af; background: #fff; }}
</style>
</head>
<body>

<div class="hero">
  <h1>📈 Finviz Screener Agent</h1>
  <p>Daily momentum screener · Weekly persistence review · Auto-generated {generated_at}</p>
  <div class="hero-links">{hero_links}</div>
</div>

<div class="stats">
  <div class="stat">
    <span class="stat-val">{total_daily}</span>
    <span class="stat-label">Daily reports</span>
  </div>
  <div class="stat">
    <span class="stat-val">{total_weekly}</span>
    <span class="stat-label">Weekly reviews</span>
  </div>
  <div class="stat">
    <span class="stat-val">{reports["daily_gallery"][0]["date"] if reports["daily_gallery"] else "—"}</span>
    <span class="stat-label">Last daily run</span>
  </div>
  <div class="stat">
    <span class="stat-val">{reports["weekly"][0]["date"] if reports["weekly"] else "—"}</span>
    <span class="stat-label">Last weekly run</span>
  </div>
</div>

{"<div class='section'><h2>Weekly Reviews</h2><div class='cards'>" + weekly_cards + "</div></div>" if weekly_cards else ""}

<div class="section">
  <h2>Daily Chart Galleries</h2>
  <div class="cards">{gallery_cards}</div>
</div>

<div class="footer">
  Generated {generated_at} · 
  <a href="https://github.com/AnanthSrinivasan/finviz-screener-agent" 
     style="color:#9ca3af">github.com/AnanthSrinivasan/finviz-screener-agent</a>
</div>

</body>
</html>"""

    return html


def main():
    log.info("=== Index generator starting ===")

    reports = scan_reports(DATA_DIR)
    total = sum(len(v) for v in reports.values())
    log.info(f"Found {total} report files across {len(reports)} categories")

    base_url = GITHUB_PAGES_BASE.rstrip("/") if GITHUB_PAGES_BASE else ""
    html = generate_index(reports, base_url)

    with open(OUTPUT_PATH, "w") as f:
        f.write(html)

    log.info(f"index.html written → {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
