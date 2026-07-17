#!/usr/bin/env python3
"""
Record page — data/record.html (spec: docs/specs/cx-rehaul.md §A.3).

One "how am I doing" page replacing three index buttons: client-side tabs over
Performance 2026 YTD · Performance 2024-25 · MAE/MFE · Trader Mirror archive.
Existing artifacts are EMBEDDED (lazy-loaded iframes), not rewritten — this
page is pure chrome. Tabs whose artifact is missing render a note instead.

Run alongside generate_index.py (its main() calls write_page here, non-fatal).
"""

import datetime
import glob
import logging
import os

from utils.generators.nav import render_nav
from utils.generators.theme import page_shell

log = logging.getLogger(__name__)

DATA_DIR = os.environ.get("DATA_DIR", "data")
OUTPUT_PATH = os.path.join(DATA_DIR, "record.html")

RECORD_CSS = """
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px}
.tab-btn{padding:10px 16px;border-radius:8px;border:1px solid var(--border);
  background:var(--surface);color:var(--muted);font-size:.84rem;font-weight:700;cursor:pointer}
.tab-btn.active{background:#1c3157;color:var(--head);border-color:var(--link)}
.tab-pane{display:none}
.tab-pane.active{display:block}
.tab-frame{width:100%;height:calc(100vh - 190px);min-height:520px;border:1px solid var(--border);
  border-radius:10px;background:#fff}
.mirror-list{display:flex;flex-direction:column;gap:8px;margin-bottom:12px}
"""

RECORD_JS = """
function showTab(key, btn) {
  document.querySelectorAll('.tab-pane').forEach(function(p){ p.classList.remove('active'); });
  document.querySelectorAll('.tab-btn').forEach(function(b){ b.classList.remove('active'); });
  var pane = document.getElementById('pane-' + key);
  if (pane) pane.classList.add('active');
  if (btn) btn.classList.add('active');
  // Lazy-load the iframe on first activation
  if (pane) {
    var f = pane.querySelector('iframe[data-src]');
    if (f && !f.getAttribute('src')) f.setAttribute('src', f.getAttribute('data-src'));
  }
}
"""


def trader_mirror_files(data_dir: str = None) -> list:
    """Monthly trader-mirror HTML reports, newest first (may be empty)."""
    d = data_dir or DATA_DIR
    return sorted(glob.glob(os.path.join(d, "trader_mirror_2*.html")), reverse=True)


def _tab_defs(data_dir: str = None) -> list:
    """(key, label, artifact basename or None). Artifact None → missing note."""
    d = data_dir or DATA_DIR

    def _have(name):
        return name if os.path.exists(os.path.join(d, name)) else None

    mirrors = trader_mirror_files(d)
    return [
        ("perf26", "Performance 2026 YTD", _have("performance_2026.html")),
        ("perf2425", "Performance 2024–25", _have("performance_charts.html")),
        ("mae", "MAE / MFE", _have("mae_analysis.html")),
        ("mirror", "Trader Mirror",
         os.path.basename(mirrors[0]) if mirrors else None),
    ]


def render_page(data_dir: str = None) -> str:
    tabs = _tab_defs(data_dir)
    first_available = next((k for k, _, f in tabs if f), tabs[0][0])

    btns, panes = "", ""
    for key, label, fname in tabs:
        active = " active" if key == first_available else ""
        btns += (f'<button class="tab-btn{active}" '
                 f'onclick="showTab(\'{key}\', this)">{label}</button>')
        if fname is None:
            inner = (f'<div class="empty">{label} — no report generated yet. '
                     "It will appear here on its next run.</div>")
        else:
            # First (default) tab loads eagerly; the rest lazy-load on click.
            src_attr = f'src="{fname}"' if active else f'data-src="{fname}"'
            inner = f'<iframe class="tab-frame" {src_attr} title="{label}"></iframe>'
        if key == "mirror":
            mirrors = trader_mirror_files(data_dir)
            if len(mirrors) > 1:
                links = " · ".join(
                    f'<a href="{os.path.basename(m)}">{os.path.basename(m)[len("trader_mirror_"):-len(".html")]}</a>'
                    for m in mirrors)
                inner = f'<div class="mirror-list">Months: {links}</div>' + inner
        panes += f'<div id="pane-{key}" class="tab-pane{active}">{inner}</div>'

    updated = datetime.datetime.now(datetime.timezone.utc).strftime("%d %b %Y %H:%M UTC")
    body = f'<div class="tabs">{btns}</div>{panes}'
    return page_shell(
        "The Record", render_nav("record"), body,
        h1="📈 The Record — am I improving?",
        subtitle=f"Performance · MAE/MFE · Trader Mirror in one place · {updated}",
        extra_css=RECORD_CSS, extra_script=RECORD_JS,
    )


def write_page() -> str:
    os.makedirs(DATA_DIR, exist_ok=True)
    html = render_page()
    with open(OUTPUT_PATH, "w") as f:
        f.write(html)
    log.info("record.html written → %s", OUTPUT_PATH)
    return OUTPUT_PATH


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    write_page()
