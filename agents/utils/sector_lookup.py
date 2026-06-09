"""
Resolve held-ticker → sector ETF for the rotation tracker.

Priority:
  1. Explicit ticker→ETF map at data/ticker_sector_map.json.
  2. Finviz Industry string passed in by caller, matched substring-wise against
     INDUSTRY_TO_ETF (semis vs software vs banks etc. — sub-sector precision).
  3. Finviz Sector string passed in by caller, mapped via FINVIZ_SECTOR_TO_ETF.
  4. None (caller skips the position for sector signals).
"""

from __future__ import annotations

import json
import os
from typing import Optional

DATA_DIR = os.environ.get("DATA_DIR", "data")
TICKER_MAP_FILE = os.path.join(DATA_DIR, "ticker_sector_map.json")

# Substring match on Finviz Industry strings — first matching key wins.
# Order matters: list specific industries before generic ones.
INDUSTRY_TO_ETF = {
    # Technology subsectors — the bug fix (semis vs software vs hardware)
    "Semiconductor":                   "SMH",   # "Semiconductors", "Semiconductor Equipment & Materials"
    "Software - Application":          "IGV",
    "Software - Infrastructure":       "IGV",
    "Internet Content":                "FDN",   # "Internet Content & Information"
    "Information Technology Services": "XLK",
    "Computer Hardware":               "XLK",
    "Electronic Components":           "XLK",
    # Financials subsectors
    "Banks":                           "KBE",   # "Banks - Regional", "Banks - Diversified"
    "Capital Markets":                 "KCE",
    "Insurance":                       "KIE",
    "Credit Services":                 "ARKF",  # fintech / consumer-finance (DAVE, SoFi, AFRM class)
    "Financial - Credit Services":     "ARKF",
    # Healthcare subsectors — biotech vs broad healthcare
    "Biotechnology":                   "XBI",
    "Drug Manufacturers":              "XBI",   # "Drug Manufacturers - Specialty & Generic" included
    # Consumer cyclical subsectors — homebuilders vs broad
    "Residential Construction":        "XHB",
    "Building Products":               "XHB",
}

FINVIZ_SECTOR_TO_ETF = {
    "Technology":             "XLK",
    "Financial":              "XLF",
    "Financials":             "XLF",
    "Energy":                 "XLE",
    "Healthcare":             "XLV",
    "Industrials":            "XLI",
    "Industrial":             "XLI",
    "Consumer Cyclical":      "XLY",
    "Consumer Defensive":     "XLP",
    "Utilities":              "XLU",
    "Basic Materials":        "XLB",
    "Real Estate":            "XLRE",
    "Communication Services": "XLC",
}


_cache: dict | None = None


def _load_ticker_map() -> dict:
    global _cache
    if _cache is None:
        try:
            with open(TICKER_MAP_FILE) as f:
                _cache = json.load(f) or {}
        except FileNotFoundError:
            _cache = {}
    return _cache


def _industry_to_etf(finviz_industry: str) -> Optional[str]:
    """Substring match against INDUSTRY_TO_ETF — first matching key wins."""
    if not finviz_industry:
        return None
    industry = finviz_industry.strip()
    if not industry:
        return None
    for key, etf in INDUSTRY_TO_ETF.items():
        if key in industry:
            return etf
    return None


def lookup(ticker: str, finviz_sector: Optional[str] = None,
           finviz_industry: Optional[str] = None) -> Optional[str]:
    """Return the ETF symbol for a ticker, or None if unmapped.

    Priority: explicit ticker map > industry substring > sector map.
    """
    if not ticker:
        return None
    mapping = _load_ticker_map()
    etf = mapping.get(ticker.upper())
    if etf:
        return etf
    etf = _industry_to_etf(finviz_industry or "")
    if etf:
        return etf
    if finviz_sector:
        return FINVIZ_SECTOR_TO_ETF.get(finviz_sector.strip())
    return None


def reset_cache() -> None:
    """Test hook — forces re-read of the ticker map on next lookup."""
    global _cache
    _cache = None
