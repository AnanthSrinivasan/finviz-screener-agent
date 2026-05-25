"""Test HTF Base Reclaim ATR cap — raised 7 → 8.5 (May 2026), then 8.5 → 10 (2026-05-25)."""

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
    def test_atr_8_5_passes(self):
        self.assertTrue(_is_htf_base_reclaim_candidate(_row(**{"ATR%": 8.5}), set(), set()))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_rdw_atr_9_12_passes(self):
        # RDW May 8 2026 — Q84, Stage 2 perfect, ATR 9.12, dist -50%, RVol 2.76.
        # Previously dropped at 8.5 cap → went +90% over next 2 weeks.
        rdw = _row(**{
            "Ticker":          "RDW",
            "Quality Score":   84.0,
            "ATR%":            9.12,
            "Dist From High%": -50.25,
            "SMA20%":          12.86,
            "SMA50%":          16.92,
            "SMA200%":         22.23,
            "Rel Volume":      2.76,
        })
        self.assertTrue(_is_htf_base_reclaim_candidate(rdw, set(), set()))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_atr_10_boundary_passes(self):
        self.assertTrue(_is_htf_base_reclaim_candidate(_row(**{"ATR%": 10.0}), set(), set()))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_atr_10_01_rejects(self):
        self.assertFalse(_is_htf_base_reclaim_candidate(_row(**{"ATR%": 10.01}), set(), set()))


if __name__ == "__main__":
    unittest.main()
