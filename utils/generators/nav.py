#!/usr/bin/env python3
"""
Shared nav bar for every generated page (spec: docs/specs/cx-rehaul.md §A.1).

render_nav(active) returns one self-contained horizontal bar (own <style>, no
dependency on the host page's CSS) so it can be injected into dark theme pages
and legacy light pages alike:

    ⌂ · ☀️ Cockpit · 💰 Flow · 📓 Book · 🤖 Paper · 👀 Watchlist · 🖼 Charts · 📈 Record

Pages live in two directories on GitHub Pages: repo root (index.html,
watchlist.html) and data/ (everything else). `at_root=True` renders
root-relative hrefs; default renders data/-relative hrefs. Charts resolves to
the newest finviz_chart_grid_*.html at render time. Mobile: horizontal scroll,
44px touch targets.
"""

import glob
import os

DATA_DIR = os.environ.get("DATA_DIR", "data")


def _cache_q() -> str:
    """Cache-bust query (same pattern as the other generators)."""
    sha = os.environ.get("GITHUB_SHA") or os.environ.get("CACHE_BUST_SHA") or ""
    if not sha:
        try:
            import subprocess
            sha = subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"], text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        except Exception:
            sha = ""
    return f"?v={sha[:7]}" if sha else ""


def latest_chart_grid() -> str:
    """Basename of the newest chart gallery, or '' when none exist."""
    files = sorted(glob.glob(os.path.join(DATA_DIR, "finviz_chart_grid_2*.html")))
    return os.path.basename(files[-1]) if files else ""


# (key, label, filename, lives_in_data_dir)
def _items() -> list:
    return [
        ("home",      "⌂",            "index.html",           False),
        ("cockpit",   "☀️ Cockpit",   "daily.html",           True),
        ("flow",      "💰 Flow",      "etf_rotation.html",    True),
        ("book",      "📓 Book",      "live_portfolio.html",  True),
        ("paper",     "🤖 Paper",     "claude_portfolio.html", True),
        ("watchlist", "👀 Watchlist", "watchlist.html",       False),
        ("charts",    "🖼 Charts",    latest_chart_grid(),    True),
        ("record",    "📈 Record",    "record.html",          True),
    ]


NAV_CSS = """
.site-nav{display:flex;gap:2px;overflow-x:auto;-webkit-overflow-scrolling:touch;
  background:#101a2c;border:1px solid #223049;border-radius:10px;padding:4px;
  margin-bottom:16px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
.site-nav a{display:inline-flex;align-items:center;min-height:44px;padding:6px 14px;
  border-radius:8px;font-size:.84rem;font-weight:600;color:#a5b1c4;
  text-decoration:none;white-space:nowrap;flex-shrink:0}
.site-nav a:hover{color:#f1f5f9;background:#16233a;text-decoration:none}
.site-nav a.nav-active{color:#f1f5f9;background:#1c3157}
.site-nav::-webkit-scrollbar{height:0}
"""


def render_nav(active: str = "", at_root: bool = False) -> str:
    """One nav bar, self-contained. `active` highlights the current page key;
    `at_root=True` for pages generated at the repo root (index, watchlist)."""
    q = _cache_q()
    links = []
    for key, label, fname, in_data in _items():
        if not fname:
            continue  # e.g. no chart grid generated yet
        if at_root:
            href = (f"data/{fname}" if in_data else fname) + q
        else:
            href = (fname if in_data else f"../{fname}") + q
        cls = ' class="nav-active"' if key == active else ""
        links.append(f'<a href="{href}"{cls}>{label}</a>')
    return (f"<style>{NAV_CSS}</style>"
            f'<nav class="site-nav">{"".join(links)}</nav>')
