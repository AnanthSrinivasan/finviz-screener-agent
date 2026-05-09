"""Unit tests for _is_htf_base_reclaim_candidate and _is_htf_base_reclaim."""

import unittest
from unittest.mock import patch

import pandas as pd

from agents.screener.finviz_agent import (
    _is_htf_base_reclaim_candidate,
    _is_htf_base_reclaim,
)


def _permissive_peel(ticker, atr_pct):
    return 10.0


def _row(**over):
    """RKLB Apr 16 reference row: -16.7% from 52w high but tight at swing pivot."""
    base = {
        "Ticker":           "RKLB",
        "Quality Score":    78.0,
        "Stage":            {"stage": 2, "perfect": True},
        "Dist From High%":  -16.7,
        "ATR%":             5.0,
        "SMA20%":           2.0,
        "SMA50%":           4.0,
        "SMA200%":          6.0,
        "Rel Volume":       1.1,
    }
    base.update(over)
    return pd.Series(base)


class TestHtfBaseReclaimCandidate(unittest.TestCase):
    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_rklb_passes_pre_filter(self):
        self.assertTrue(_is_htf_base_reclaim_candidate(_row(), set(), set()))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_rejects_held(self):
        self.assertFalse(_is_htf_base_reclaim_candidate(_row(), {"RKLB"}, set()))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_rejects_excluded(self):
        self.assertFalse(_is_htf_base_reclaim_candidate(_row(), set(), {"RKLB"}))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_rejects_stage_not_2_perfect(self):
        self.assertFalse(_is_htf_base_reclaim_candidate(
            _row(Stage={"stage": 2, "perfect": False}), set(), set()
        ))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_rejects_q_below_75(self):
        self.assertFalse(_is_htf_base_reclaim_candidate(
            _row(**{"Quality Score": 70.0}), set(), set()
        ))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_rejects_dist_above_minus_12(self):
        # Dist -10 = handled by RTE (after gate softening), not HTF.
        self.assertFalse(_is_htf_base_reclaim_candidate(
            _row(**{"Dist From High%": -10.0}), set(), set()
        ))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_rejects_atr_above_7(self):
        self.assertFalse(_is_htf_base_reclaim_candidate(
            _row(**{"ATR%": 7.5}), set(), set()
        ))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_rejects_falling_ma_stack(self):
        self.assertFalse(_is_htf_base_reclaim_candidate(
            _row(**{"SMA200%": -2.0}), set(), set()
        ))
        self.assertFalse(_is_htf_base_reclaim_candidate(
            _row(**{"SMA50%": -1.0}), set(), set()
        ))
        self.assertFalse(_is_htf_base_reclaim_candidate(
            _row(**{"SMA20%": -0.5}), set(), set()
        ))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_rejects_rvol_below_1(self):
        self.assertFalse(_is_htf_base_reclaim_candidate(
            _row(**{"Rel Volume": 0.9}), set(), set()
        ))

    @patch("agents.screener.finviz_agent._peel_warn_for", lambda t, a: 0.5)
    def test_peel_warn_blocks_extended(self):
        self.assertFalse(_is_htf_base_reclaim_candidate(_row(), set(), set()))


class TestHtfBaseReclaimFinal(unittest.TestCase):
    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_rklb_with_swing_pivot_minus_5_passes(self):
        # RKLB Apr 16 reference — swing pivot was tight (~-5% from prior swing high
        # while -16.7% from 52w high). Gate: swing_dist_pct >= -10.
        self.assertTrue(_is_htf_base_reclaim(_row(), set(), set(), swing_dist_pct=-5.0))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_swing_minus_15_fails(self):
        self.assertFalse(_is_htf_base_reclaim(_row(), set(), set(), swing_dist_pct=-15.0))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_none_swing_dist_fails(self):
        self.assertFalse(_is_htf_base_reclaim(_row(), set(), set(), swing_dist_pct=None))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_q70_fails_predicate_even_with_tight_swing(self):
        self.assertFalse(_is_htf_base_reclaim(
            _row(**{"Quality Score": 70.0}), set(), set(), swing_dist_pct=-5.0
        ))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_already_in_fresh_breakout_excluded(self):
        self.assertFalse(_is_htf_base_reclaim(_row(), set(), {"RKLB"}, swing_dist_pct=-5.0))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_swing_at_boundary_minus_10(self):
        self.assertTrue(_is_htf_base_reclaim(_row(), set(), set(), swing_dist_pct=-10.0))
        self.assertFalse(_is_htf_base_reclaim(_row(), set(), set(), swing_dist_pct=-10.01))


if __name__ == "__main__":
    unittest.main()
