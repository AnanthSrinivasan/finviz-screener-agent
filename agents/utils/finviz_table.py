"""Shared Finviz screener-table cell parsing.

Finviz added a company-logo element inside the v=111 ticker cell
(observed 2026-07-17): the cell now holds an `a.company-ticker` logo link
whose `<span>` carries the ticker's FIRST LETTER as an image-load fallback,
followed by the real `a.tab-link` ticker link. A bare `td.text` therefore
returns the ticker with its first letter doubled ("AAPL" → "AAAPL"), which
404s every snapshot fetch. Never read the ticker cell with `.text` —
use `extract_ticker(td)`.
"""


def extract_ticker(td) -> str:
    """Ticker from a screener-table cell, robust to the 2026-07-17 logo cell.

    Priority: `data-boxover-ticker` attribute (cleanest, attribute-only) →
    `a.tab-link` text (the real ticker link) → whole-cell text (pre-logo
    legacy layout, where the cell held only the ticker)."""
    attr = (td.get("data-boxover-ticker") or "").strip()
    if attr:
        return attr
    link = td.select_one("a.tab-link")
    if link:
        t = link.get_text(strip=True)
        if t:
            return t
    return td.get_text(strip=True)
