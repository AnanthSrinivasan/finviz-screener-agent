#!/usr/bin/env python3
# ----------------------------
# Index Page Generator — the landing that gets out of the way
# (spec: docs/specs/cx-rehaul.md §A.2)
# ----------------------------
# One primary button (☀️ Open Cockpit) + the shared nav. Below: latest weekly
# card + the 📚 Report Archive — EVERY dated report stays reachable from here
# (user condition 2026-07-15: nothing loses its home), grouped in <details>.
# Also regenerates data/record.html (A.3) since both run at the same cadence.
# ----------------------------

import os
import re
import glob
import datetime
import logging

from utils.generators.nav import render_nav, _cache_q
from utils.generators.theme import page_shell

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DATA_DIR = os.environ.get("DATA_DIR", "data")
GITHUB_PAGES_BASE = os.environ.get("GITHUB_PAGES_BASE", "")

CACHE_Q = _cache_q()
OUTPUT_PATH = "index.html"


def scan_reports(data_dir: str) -> dict:
    """Scan data/ folder and return structured report list."""
    reports = {
        "weekly": [],
        "daily_gallery": [],
        "daily_summary": [],
        "persistence": [],
        "trader_mirror": [],
    }

    globs = {
        "weekly": "finviz_weekly_2*.html",
        "daily_gallery": "finviz_chart_grid_2*.html",
        "daily_summary": "finviz_screeners_2*.html",
        "persistence": "finviz_weekly_persistence_2*.csv",
        "trader_mirror": "trader_mirror_2*.html",
    }
    for key, pattern in globs.items():
        for f in sorted(glob.glob(os.path.join(data_dir, pattern)), reverse=True):
            reports[key].append({"date": _extract_date(f),
                                 "file": os.path.basename(f), "path": f})
    return reports


def _extract_date(filepath: str) -> str:
    match = re.search(r"(\d{4}-\d{2})(-\d{2})?", os.path.basename(filepath))
    return (match.group(1) + (match.group(2) or "")) if match else "unknown"


def _format_date(date_str: str) -> str:
    try:
        d = datetime.date.fromisoformat(date_str)
        return d.strftime("%a %d %b %Y")
    except Exception:
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
    except Exception:
        return ""


INDEX_CSS = """
.hero{background:var(--surface);border:1px solid var(--border);border-radius:12px;
  padding:22px 20px;margin-bottom:18px}
.hero p{color:var(--muted);font-size:.82rem;margin:4px 0 16px}
.btn-cockpit{display:inline-flex;align-items:center;gap:8px;padding:14px 28px;
  border-radius:10px;background:#1c3157;border:1px solid var(--link);color:var(--head);
  font-size:1rem;font-weight:800;text-decoration:none}
.btn-cockpit:hover{background:#234070;text-decoration:none}
.weekly-card{display:block;background:var(--surface);border:1px solid var(--border);
  border-left:4px solid var(--link);border-radius:10px;padding:14px 16px;margin-bottom:18px;
  text-decoration:none}
.weekly-card:hover{border-color:var(--link);text-decoration:none}
.weekly-card .wl{font-size:.66rem;text-transform:uppercase;letter-spacing:.06em;
  font-weight:700;color:var(--link)}
.weekly-card .wd{font-size:.95rem;font-weight:700;color:var(--head);margin-top:2px}
.weekly-card .wa{font-size:.72rem;color:var(--muted)}
.archive-links{display:flex;flex-wrap:wrap;gap:6px;padding:10px 0}
.archive-links a{font-size:.76rem;padding:5px 10px;border-radius:6px;
  background:var(--surface2);border:1px solid var(--border);color:var(--text);white-space:nowrap}
.archive-links a:hover{border-color:var(--link);text-decoration:none}
"""


def _archive_group(title: str, items: list, base_url: str, label=None) -> str:
    """One <details> group of dated links. Empty group renders nothing."""
    if not items:
        return ""
    links = ""
    for r in items:
        path = f"{base_url}/data/{r['file']}" if base_url else f"data/{r['file']}"
        text = label(r) if label else r["date"]
        links += f'<a href="{path}{CACHE_Q}">{text}</a>'
    return (f"<details><summary>{title} ({len(items)})</summary>"
            f'<div class="archive-links">{links}</div></details>')


def generate_index(reports: dict, base_url: str) -> str:
    generated_at = datetime.datetime.now().strftime("%d %b %Y %H:%M UTC")

    def _url(path_in_data: str) -> str:
        base = f"{base_url}/data/" if base_url else "data/"
        return f"{base}{path_in_data}{CACHE_Q}"

    latest_weekly = reports["weekly"][0] if reports["weekly"] else None

    hero = (
        '<div class="hero">'
        "<h1>📈 Finviz Screener Agent</h1>"
        f"<p>Daily screener · position monitor · money flow · generated {generated_at}</p>"
        f'<a class="btn-cockpit" href="{_url("daily.html")}">☀️ Open Cockpit</a>'
        "</div>"
    )

    weekly_card = ""
    if latest_weekly:
        weekly_card = (
            f'<a class="weekly-card" href="{_url(latest_weekly["file"])}">'
            '<div class="wl">Latest Weekly Review</div>'
            f'<div class="wd">{_format_date(latest_weekly["date"])}</div>'
            f'<div class="wa">{_days_ago(latest_weekly["date"])}</div></a>'
        )

    # 📚 Report Archive — every dated report keeps its home (user condition).
    dash_href = f"{base_url}/dashboard.html{CACHE_Q}" if base_url else f"dashboard.html{CACHE_Q}"
    perf_links = (
        '<div class="archive-links">'
        f'<a href="{_url("record.html")}">📈 The Record (tabs)</a>'
        f'<a href="{_url("performance_2026.html")}">Performance 2026 YTD</a>'
        f'<a href="{_url("performance_charts.html")}">Performance 2024–25</a>'
        f'<a href="{_url("mae_analysis.html")}">MAE / MFE Analysis</a>'
        f'<a href="{dash_href}">Retired positions dashboard (snapshot)</a>'
        "</div>"
    )
    archive = (
        "<h2>📚 Report Archive</h2>"
        + _archive_group("Weekly Reviews", reports["weekly"], base_url)
        + _archive_group("Daily Chart Galleries", reports["daily_gallery"], base_url)
        + _archive_group("Daily Screener Tables", reports["daily_summary"], base_url)
        + _archive_group("Trader Mirror (monthly)", reports["trader_mirror"], base_url)
        + _archive_group("Weekly Persistence CSVs", reports["persistence"], base_url)
        + f"<details><summary>Performance &amp; analysis ({4 + 1})</summary>{perf_links}</details>"
    )

    footer = (
        '<div class="footer">Generated ' + generated_at + " · "
        '<a href="https://github.com/AnanthSrinivasan/finviz-screener-agent">'
        "github.com/AnanthSrinivasan/finviz-screener-agent</a></div>"
    )

    body = hero + weekly_card + archive + footer
    return page_shell("Finviz Screener Agent", render_nav("home", at_root=True),
                      body, extra_css=INDEX_CSS)


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

    # A.3 — regenerate data/record.html at the same cadence (non-fatal).
    try:
        from utils.generators.generate_record import write_page as _write_record
        _write_record()
    except Exception as e:
        log.warning("record.html generation failed (non-fatal): %s", e)


if __name__ == "__main__":
    main()
