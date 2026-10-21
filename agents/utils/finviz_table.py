"""Shared Finviz screener-table cell parsing.

Finviz added a company-logo element inside the v=111 ticker cell
(observed 2026-07-17): the cell now holds an `a.company-ticker` logo link
whose `<span>` carries the ticker's FIRST LETTER as an image-load fallback,
followed by the real `a.tab-link` ticker link. A bare `td.text` therefore
returns the ticker with its first letter doubled ("AAPL" → "AAAPL"), which
404s every snapshot fetch. Never read the ticker cell with `.text` —
use `extract_ticker(td)`.
"""

import json


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


# ---------------------------------------------------------------------------
# 2026-10-19 redesign: `screener.ashx` now 301s to `/screener/stocks`, which
# renders rows client-side — the HTML has ZERO <tr> elements, so the
# `tr[valign="top"]` table walk finds nothing (Daily Screener aborted
# "No tickers" on 10-19 and 10-20 while the quote-page canary stayed green).
# The rows ship in an embedded `<script type="application/json">` blob:
#   data.rows[] = {text: {ticker, company, sector, industry, country, ...},
#                  numbers: {marketCap (USD millions), PE, price,
#                            change (%), volume, ...}}
# `screener_json_rows()` extracts them and formats each field the way the
# legacy table cell read, so downstream string parsers keep working.
# ---------------------------------------------------------------------------


def _fmt_mcap(m) -> str:
    """USD millions → legacy '3.35B' / '770.88M' / '1.20T' cell text."""
    if m is None:
        return "-"
    m = float(m)
    if m >= 1e6:
        return f"{m / 1e6:.2f}T"
    if m >= 1e3:
        return f"{m / 1e3:.2f}B"
    return f"{m:.2f}M"


def _fmt_num(x, fmt: str) -> str:
    if x is None:
        return "-"
    try:
        return format(float(x), fmt)
    except (TypeError, ValueError):
        return "-"


def _find_rows(obj):
    """Locate the `data.rows` list anywhere in the decoded JSON blob."""
    if isinstance(obj, dict):
        data = obj.get("data")
        if isinstance(data, dict) and isinstance(data.get("rows"), list):
            return data["rows"]
        for v in obj.values():
            found = _find_rows(v)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_rows(v)
            if found is not None:
                return found
    return None


def screener_json_rows(soup):
    """Rows from the redesigned screener's embedded JSON, or None if the page
    carries no such blob (legacy table layout — caller falls back).

    Each row: {'No.', 'Ticker', 'Company', 'Sector', 'Industry', 'Country',
    'Market Cap', 'P/E', 'Price', 'Change', 'Volume'} as legacy-format text."""
    for script in soup.find_all("script", attrs={"type": "application/json"}):
        raw = script.string or script.get_text() or ""
        if '"rows"' not in raw:
            continue
        try:
            blob = json.loads(raw)
        except ValueError:
            continue
        rows = _find_rows(blob)
        if rows is None:
            continue
        out = []
        for i, r in enumerate(rows):
            text = r.get("text") or {}
            nums = r.get("numbers") or {}
            ticker = (text.get("ticker") or "").strip()
            if not ticker:
                continue
            vol = nums.get("volume")
            out.append({
                "No.":        str(int(nums.get("order", i)) + 1),
                "Ticker":     ticker,
                "Company":    (text.get("company") or "").strip(),
                "Sector":     (text.get("sector") or "").strip(),
                "Industry":   (text.get("industry") or "").strip(),
                "Country":    (text.get("country") or "").strip(),
                "Market Cap": _fmt_mcap(nums.get("marketCap")),
                "P/E":        _fmt_num(nums.get("PE"), ".2f"),
                "Price":      _fmt_num(nums.get("price"), ".2f"),
                "Change":     (_fmt_num(nums.get("change"), ".2f") + "%")
                              if nums.get("change") is not None else "-",
                "Volume":     f"{int(vol):,}" if vol is not None else "",
            })
        return out
    return None
