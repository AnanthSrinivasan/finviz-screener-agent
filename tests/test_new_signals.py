"""Unit tests for Fresh Breakout, Textbook VCP, Power Play predicates."""

import unittest
from unittest.mock import patch

import pandas as pd

from agents.screener.finviz_agent import (
    _is_fresh_breakout,
    _is_textbook_vcp,
    _is_power_play,
)


# Patch peel-warn to a permissive 10x for most tests — we test that gate separately.
def _permissive_peel(ticker, atr_pct):
    return 10.0


def _row(**over):
    """Row defaulted to a valid Fresh Breakout (ANET-Apr8-ish: just reclaimed MAs)."""
    base = {
        "Ticker": "ANET",
        "Quality Score": 90.0,
        "Stage": {"stage": 2, "perfect": False},   # fresh breakout may not be "perfect" yet
        "SMA20%": 5.0,
        "SMA50%": 10.0,
        "SMA200%": 7.0,
        "ATR%": 4.0,
        "Rel Volume": 1.5,
        "Dist From High%": -5.0,
        "VCP": {"confidence": 30, "vcp_possible": False},
        "Appearances": 1,
        "Perf Month": 20.0,
        "Perf Quarter": 30.0,
        "Inst Trans": 2.0,
        "EPS Y/Y TTM": 100.0,
        "EPS Q/Q": 50.0,
    }
    base.update(over)
    return pd.Series(base)


# -- Fresh Breakout ---------------------------------------------------------

class TestFreshBreakout(unittest.TestCase):
    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_anet_reconstructed_passes(self):
        self.assertTrue(_is_fresh_breakout(_row(), set()))

    def test_rejects_if_held(self):
        self.assertFalse(_is_fresh_breakout(_row(), {"ANET"}))

    def test_rejects_not_stage_2(self):
        self.assertFalse(_is_fresh_breakout(_row(Stage={"stage": 1, "perfect": False}), set()))

    def test_rejects_sma20_not_positive(self):
        self.assertFalse(_is_fresh_breakout(_row(**{"SMA20%": -0.5}), set()))

    def test_rejects_sma50_extended(self):
        # SMA50% > 25 = already extended, not "fresh"
        self.assertFalse(_is_fresh_breakout(_row(**{"SMA50%": 30.0}), set()))

    def test_rejects_sma200_not_above(self):
        self.assertFalse(_is_fresh_breakout(_row(**{"SMA200%": -2.0}), set()))

    def test_rejects_rvol_no_expansion(self):
        self.assertFalse(_is_fresh_breakout(_row(**{"Rel Volume": 0.8}), set()))

    def test_rejects_atr_too_volatile(self):
        self.assertFalse(_is_fresh_breakout(_row(**{"ATR%": 9.0}), set()))

    def test_rejects_q_below_70(self):
        self.assertFalse(_is_fresh_breakout(_row(**{"Quality Score": 60.0}), set()))

    def test_rejects_broken_base(self):
        self.assertFalse(_is_fresh_breakout(_row(**{"Dist From High%": -15.0}), set()))

    @patch("agents.screener.finviz_agent._peel_warn_for", lambda t, a: 3.0)
    def test_peel_warn_blocks_extended(self):
        # With peel_warn mocked to 3.0 (low ATR tier), sma50/atr = 20/3 = 6.67 > 3.0 → blocked
        self.assertFalse(_is_fresh_breakout(_row(**{"SMA50%": 20.0, "ATR%": 3.0}), set()))


# -- Textbook VCP -----------------------------------------------------------

def _tb_row(**over):
    base = {
        "Ticker": "MU",
        "Quality Score": 100.0,
        "Stage": {"stage": 2, "perfect": True},
        "VCP": {"confidence": 85, "vcp_possible": True},
        "Appearances": 3,
        "ATR%": 4.5,
        "Dist From High%": -5.0,
    }
    base.update(over)
    return pd.Series(base)


class TestTextbookVCP(unittest.TestCase):
    def test_textbook_passes(self):
        self.assertTrue(_is_textbook_vcp(_tb_row()))

    def test_rejects_vcp_below_85(self):
        self.assertFalse(_is_textbook_vcp(_tb_row(VCP={"confidence": 80})))

    def test_rejects_appearances_below_3(self):
        self.assertFalse(_is_textbook_vcp(_tb_row(Appearances=2)))

    def test_rejects_atr_above_5(self):
        self.assertFalse(_is_textbook_vcp(_tb_row(**{"ATR%": 6.0})))

    def test_rejects_stage_not_perfect(self):
        self.assertFalse(_is_textbook_vcp(_tb_row(Stage={"stage": 2, "perfect": False})))

    def test_rejects_pullback_too_shallow(self):
        self.assertFalse(_is_textbook_vcp(_tb_row(**{"Dist From High%": -2.0})))

    def test_rejects_pullback_too_deep(self):
        self.assertFalse(_is_textbook_vcp(_tb_row(**{"Dist From High%": -16.0})))

    def test_accepts_deep_base_indv_class(self):
        # INDV (Apr 2026) was at -13% — Apr 30 widened band to -15%
        self.assertTrue(_is_textbook_vcp(_tb_row(**{"Dist From High%": -13.0})))

    def test_rejects_q_below_80(self):
        self.assertFalse(_is_textbook_vcp(_tb_row(**{"Quality Score": 75.0})))


# -- Power Play -------------------------------------------------------------

def _pp_row(**over):
    base = {
        "Ticker": "XYZ",
        "Quality Score": 80.0,
        "Stage": {"stage": 2, "perfect": True},
        "SMA50%": 10.0,
        "ATR%": 4.0,
        "Rel Volume": 0.7,
        "Perf Month": 60.0,     # big runup
        "Perf Quarter": 110.0,  # rocket
    }
    base.update(over)
    return pd.Series(base)


class TestPowerPlay(unittest.TestCase):
    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_passes_with_rocket_runup_tight_dry(self):
        self.assertTrue(_is_power_play(_pp_row(), set()))

    def test_rejects_if_held(self):
        self.assertFalse(_is_power_play(_pp_row(), {"XYZ"}))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_perf_month_alone_can_trigger(self):
        # Perf Quarter small, Perf Month ≥ 50 → qualifies
        r = _pp_row(**{"Perf Month": 55.0, "Perf Quarter": 30.0})
        self.assertTrue(_is_power_play(r, set()))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_rejects_no_rocket(self):
        r = _pp_row(**{"Perf Month": 20.0, "Perf Quarter": 40.0})
        self.assertFalse(_is_power_play(r, set()))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_rejects_atr_not_tight(self):
        self.assertFalse(_is_power_play(_pp_row(**{"ATR%": 7.0}), set()))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_rejects_rvol_not_drying(self):
        self.assertFalse(_is_power_play(_pp_row(**{"Rel Volume": 1.3}), set()))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_rejects_not_stage_2(self):
        self.assertFalse(_is_power_play(_pp_row(Stage={"stage": 3, "perfect": False}), set()))


if __name__ == "__main__":
    unittest.main()
