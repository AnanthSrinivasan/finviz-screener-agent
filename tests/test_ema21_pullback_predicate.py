"""Unit tests for _is_ema21_pullback predicate (🎯 21 EMA Pullback block)."""

import unittest
from unittest.mock import patch

import pandas as pd

from agents.screener.finviz_agent import _is_ema21_pullback


def _permissive_peel(ticker, atr_pct):
    return 10.0


def _row(**over):
    """ANET Apr 22 2026 reference row — active bounce off SMA20 after run."""
    base = {
        "Ticker":           "ANET",
        "Quality Score":    82.0,
        "Stage":            {"stage": 2, "perfect": True},
        "SMA20%":           1.5,
        "SMA50%":           6.0,
        "SMA200%":          12.0,
        "ATR%":             3.4,
        "Rel Volume":       2.15,
        "Perf Month":       18.0,
    }
    base.update(over)
    return pd.Series(base)


class TestEma21Pullback(unittest.TestCase):
    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_anet_active_bounce_passes(self):
        self.assertTrue(_is_ema21_pullback(_row(), set(), set()))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_app_class_active_bounce_passes(self):
        # APP Sep 9 2024 class — RVol 1.76 (RS Leader rejects at 1.5)
        self.assertTrue(_is_ema21_pullback(_row(**{"Rel Volume": 1.76}), set(), set()))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_quiet_pb_passes(self):
        # Quiet drift PB — RVol < 1.0
        self.assertTrue(_is_ema21_pullback(_row(**{"Rel Volume": 0.7}), set(), set()))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_rejects_rvol_above_2_5(self):
        # RVol 2.6 — too hot, likely earnings gap, not a PB bounce
        self.assertFalse(_is_ema21_pullback(_row(**{"Rel Volume": 2.6}), set(), set()))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_rejects_price_too_far_above_sma20(self):
        # Extended >3% above SMA20 — chase
        self.assertFalse(_is_ema21_pullback(_row(**{"SMA20%": 3.5}), set(), set()))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_rejects_price_too_far_below_sma20(self):
        # >2% below SMA20 — broken, not a PB
        self.assertFalse(_is_ema21_pullback(_row(**{"SMA20%": -2.5}), set(), set()))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_accepts_slight_sma20_dip(self):
        # -1.5% below SMA20 — exactly the EMA21 dip case
        self.assertTrue(_is_ema21_pullback(_row(**{"SMA20%": -1.5}), set(), set()))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_rejects_no_prior_run(self):
        # Perf Month < 12% — no real prior strength to pull back from
        self.assertFalse(_is_ema21_pullback(_row(**{"Perf Month": 8.0}), set(), set()))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_rejects_atr_above_6(self):
        self.assertFalse(_is_ema21_pullback(_row(**{"ATR%": 6.5}), set(), set()))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_rejects_q_below_75(self):
        self.assertFalse(_is_ema21_pullback(_row(**{"Quality Score": 72.0}), set(), set()))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_rejects_falling_ma_stack(self):
        self.assertFalse(_is_ema21_pullback(_row(**{"SMA200%": -5.0}), set(), set()))
        self.assertFalse(_is_ema21_pullback(_row(**{"SMA50%": -1.0}), set(), set()))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_rejects_held(self):
        self.assertFalse(_is_ema21_pullback(_row(), {"ANET"}, set()))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_rejects_excluded(self):
        self.assertFalse(_is_ema21_pullback(_row(), set(), {"ANET"}))

    @patch("agents.screener.finviz_agent._peel_warn_for", lambda t, a: 1.0)
    def test_rejects_peel_warn_violation(self):
        # SMA50/ATR = 6.0 / 3.4 ≈ 1.76 > peel_warn=1.0 → reject
        self.assertFalse(_is_ema21_pullback(_row(), set(), set()))


if __name__ == "__main__":
    unittest.main()
