"""
Resolve held-ticker → sector ETF for the rotation tracker.

Priority:
  1. Explicit ticker→ETF map at data/ticker_sector_map.json.
  2. Finviz Sector string passed in by caller, mapped via FINVIZ_SECTOR_TO_ETF.
  3. None (caller skips the position for sector signals).
"""

from __future__ import annotations

import json
import os
from typing import Optional

DATA_DIR = os.environ.get("DATA_DIR", "data")
TICKER_MAP_FILE = os.path.join(DATA_DIR, "ticker_sector_map.json")

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


def lookup(ticker: str, finviz_sector: Optional[str] = None) -> Optional[str]:
    """Return the ETF symbol for a ticker, or None if unmapped."""
    if not ticker:
        return None
    mapping = _load_ticker_map()
    etf = mapping.get(ticker.upper())
    if etf:
        return etf
    if finviz_sector:
        return FINVIZ_SECTOR_TO_ETF.get(finviz_sector.strip())
    return None


def reset_cache() -> None:
    """Test hook — forces re-read of the ticker map on next lookup."""
    global _cache
    _cache = None
