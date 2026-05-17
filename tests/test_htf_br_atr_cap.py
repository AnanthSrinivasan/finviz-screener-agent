"""Test HTF Base Reclaim ATR cap raised 7 → 8.5 (May 2026)."""

import unittest
from unittest.mock import patch

import pandas as pd

from agents.screener.finviz_agent import _is_htf_base_reclaim_candidate


def _permissive_peel(ticker, atr_pct):
    return 10.0


def _row(**over):
    base = {
        "Ticker":           "DOCN",
        "Quality Score":    78.0,
        "Stage":            {"stage": 2, "perfect": True},
        "Dist From High%":  -15.4,
        "ATR%":             8.0,
        "SMA20%":           1.0,
        "SMA50%":           3.0,
        "SMA200%":          7.0,
        "Rel Volume":       1.33,
    }
    base.update(over)
    return pd.Series(base)


class TestHtfBrAtrCap(unittest.TestCase):
    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_docn_atr_8_passes(self):
        # DOCN Apr 13 2026 — previously dropped at ATR 8.0 with old cap of 7
        self.assertTrue(_is_htf_base_reclaim_candidate(_row(), set(), set()))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_atr_8_5_boundary_passes(self):
        self.assertTrue(_is_htf_base_reclaim_candidate(_row(**{"ATR%": 8.5}), set(), set()))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_atr_8_6_rejects(self):
        self.assertFalse(_is_htf_base_reclaim_candidate(_row(**{"ATR%": 8.6}), set(), set()))


if __name__ == "__main__":
    unittest.main()
