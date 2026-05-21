"""Unit tests for _is_recovery_leader predicate (🐉 Recovery Leader block).

Reference case: ALAB 2026-05-19 — V-recovery, pre-golden-cross. Every Stage 2
gate rejects by design (compute_stage requires SMA50 > SMA200 in price terms);
this block catches the structural miss.
"""

import unittest
from unittest.mock import patch

import pandas as pd

from agents.screener.finviz_agent import _is_recovery_leader


def _permissive_peel(ticker, atr_pct):
    return 10.0


def _row(**over):
    """ALAB 2026-05-19 reference row."""
    base = {
        "Ticker":           "ALAB",
        "Quality Score":    71.0,
        "RS Rating":        72,
        "Stage":            {"stage": 0, "perfect": False},
        "SMA20%":           17.4,
        "SMA50%":           49.8,
        "SMA200%":          44.9,
        "ATR%":             7.9,
        "Rel Volume":       1.81,
        "Perf Quarter":     88.5,
        "Dist From High%":  -7.1,
        "Sector":           "Technology",
    }
    base.update(over)
    return pd.Series(base)


class TestRecoveryLeader(unittest.TestCase):
    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_alab_reference_passes(self):
        self.assertTrue(_is_recovery_leader(_row(), set(), set()))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_rejects_stage_2(self):
        # Stage 2 is caught by other blocks — not this one's job
        self.assertFalse(_is_recovery_leader(
            _row(**{"Stage": {"stage": 2, "perfect": True}}), set(), set()))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_accepts_stage_1(self):
        self.assertTrue(_is_recovery_leader(
            _row(**{"Stage": {"stage": 1, "perfect": False}}), set(), set()))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_rejects_stage_4_breakdown(self):
        self.assertFalse(_is_recovery_leader(
            _row(**{"Stage": {"stage": 4, "perfect": False}}), set(), set()))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_rejects_price_below_sma20(self):
        # Short-term not confirmed
        self.assertFalse(_is_recovery_leader(_row(**{"SMA20%": -0.5}), set(), set()))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_rejects_sma50_below_15(self):
        # Price not meaningfully above 50MA — not a real recovery
        self.assertFalse(_is_recovery_leader(_row(**{"SMA50%": 12.0}), set(), set()))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_rejects_sma200_below_15(self):
        # Price not meaningfully above 200MA — early reclaim (caught by ST)
        self.assertFalse(_is_recovery_leader(_row(**{"SMA200%": 8.0}), set(), set()))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_rejects_perf_quarter_below_50(self):
        # No momentum behind the recovery
        self.assertFalse(_is_recovery_leader(_row(**{"Perf Quarter": 42.0}), set(), set()))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_rejects_rs_rating_below_65(self):
        self.assertFalse(_is_recovery_leader(_row(**{"RS Rating": 60}), set(), set()))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_rejects_q_below_65(self):
        self.assertFalse(_is_recovery_leader(_row(**{"Quality Score": 60.0}), set(), set()))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_rejects_atr_above_9(self):
        self.assertFalse(_is_recovery_leader(_row(**{"ATR%": 9.5}), set(), set()))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_rejects_rvol_below_1(self):
        self.assertFalse(_is_recovery_leader(_row(**{"Rel Volume": 0.85}), set(), set()))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_rejects_excluded_sector(self):
        for sec in ("Utilities", "Energy", "Real Estate", "Basic Materials", "Consumer Defensive"):
            self.assertFalse(_is_recovery_leader(_row(**{"Sector": sec}), set(), set()), sec)

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_rejects_held(self):
        self.assertFalse(_is_recovery_leader(_row(), {"ALAB"}, set()))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_rejects_already_in_callout(self):
        self.assertFalse(_is_recovery_leader(_row(), set(), {"ALAB"}))

    def test_rejects_blown_off_by_peel_warn(self):
        # Extended past peel-warn — strict peel uses tier fallback (ATR≤10 → 6.5x)
        # ALAB at sma50=49.8, atr=7.9 → 6.3x just under; bump sma50 to 55 → 6.96x rejects
        self.assertFalse(_is_recovery_leader(_row(**{"SMA50%": 55.0}), set(), set()))


if __name__ == "__main__":
    unittest.main()
