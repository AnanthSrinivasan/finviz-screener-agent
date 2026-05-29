"""Test RTE / RS-Leader pullback-friendly Stage 2 when dist ≤ -10% (May 2026)."""

import unittest
from unittest.mock import patch

import pandas as pd

from agents.screener.finviz_agent import _is_ready_to_enter, _is_rs_leader_candidate


def _permissive_peel(ticker, atr_pct):
    return 10.0


def _rte_row(**over):
    """SMCI Jan 17 2024 class: dist -11%, on PB, SMA20 slightly below 0."""
    base = {
        "Ticker":           "SMCI",
        "Quality Score":    85.0,
        "Stage":            {"stage": 2, "perfect": False},  # SMA20 dipped below 0
        "VCP":              {"confidence": 75.0},
        "Dist From High%":  -11.0,
        "ATR%":             5.5,
        "SMA20%":           -2.0,  # dipped just below SMA20 on PB day
        "SMA50%":           4.0,
        "SMA200%":          10.0,
        "Rel Volume":       1.09,
    }
    base.update(over)
    return pd.Series(base)


def _rsl_row(**over):
    base = {
        "Ticker":           "SMCI",
        "Quality Score":    85.0,
        "Stage":            {"stage": 2, "perfect": False},
        "Dist From High%":  -10.0,
        "ATR%":             5.5,
        "SMA20%":           -1.0,
        "SMA50%":           4.0,
        "SMA200%":          10.0,
        "Rel Volume":       1.1,
        "Sector":           "Technology",
        "RS Rating":        80,
    }
    base.update(over)
    return pd.Series(base)


class TestRtePullbackStage(unittest.TestCase):
    def test_smci_class_passes_pullback_stage_at_minus_11(self):
        # Dist -11% (≤ -10%) — relaxed stage applies, SMA20 = -2 acceptable
        self.assertTrue(_is_ready_to_enter(_rte_row(), set()))

    def test_at_minus_9_requires_strict_perfect(self):
        # Dist -9% (> -10%) — strict perfect still required, fails
        row = _rte_row(**{"Dist From High%": -9.0})
        self.assertFalse(_is_ready_to_enter(row, set()))

    def test_at_minus_9_with_perfect_passes(self):
        row = _rte_row(**{
            "Dist From High%": -9.0,
            "Stage": {"stage": 2, "perfect": True},
            "SMA20%": 2.0,
        })
        self.assertTrue(_is_ready_to_enter(row, set()))

    def test_pullback_stage_rejects_sma20_below_minus_3(self):
        # SMA20 = -4 — too broken, not a PB
        row = _rte_row(**{"SMA20%": -4.0})
        self.assertFalse(_is_ready_to_enter(row, set()))

    def test_pullback_stage_rejects_sma50_negative(self):
        row = _rte_row(**{"SMA50%": -1.0})
        self.assertFalse(_is_ready_to_enter(row, set()))

    def test_amd_class_rejected_by_peel_warn(self):
        # AMD 2026-05-29 class: Q91, dist -11%, ATR 5.1, but sma50 = +61%
        # → sma50/atr = 11.96 vs mid-vol warn 5.0. Must reject as extended.
        row = _rte_row(**{
            "Ticker":          "AMD",
            "Stage":           {"stage": 2, "perfect": True},
            "Dist From High%": -2.0,  # back in -1..-12 band
            "SMA20%":          19.9,
            "SMA50%":          61.0,
            "ATR%":            5.1,
            "Rel Volume":      0.8,
            "VCP":             {"confidence": 75.0},
        })
        self.assertFalse(
            _is_ready_to_enter(row, set()),
            "AMD-class extended setup (sma50/atr = 11.96) must be rejected by peel-warn gate",
        )

    def test_dell_class_rejected_by_peel_warn(self):
        # DELL 2026-05-29: sma50 +50.7 / ATR 4.9 = 10.35 — mid-vol warn 5.0.
        row = _rte_row(**{
            "Ticker":          "DELL",
            "Stage":           {"stage": 2, "perfect": True},
            "Dist From High%": -2.0,
            "SMA20%":          27.1,
            "SMA50%":          50.7,
            "ATR%":             4.9,
            "Rel Volume":       1.0,
            "VCP":             {"confidence": 75.0},
        })
        self.assertFalse(_is_ready_to_enter(row, set()))

    def test_adi_class_rejected_by_calibration_cap(self):
        # ADI 2026-05-29: ATR 3.7 (low-vol tier → warn 3.0), sma50 +12.8 →
        # sma50/atr = 3.46. Per-ticker calibration would float warn to 7.5
        # (floor); the cap ensures tier wins for low-vol names. Must reject.
        row = _rte_row(**{
            "Ticker":          "ADI",
            "Stage":           {"stage": 2, "perfect": True},
            "Dist From High%": -3.8,
            "SMA20%":          1.8,
            "SMA50%":          12.8,
            "ATR%":             3.7,
            "Rel Volume":       1.2,
            "VCP":             {"confidence": 75.0},
        })
        self.assertFalse(
            _is_ready_to_enter(row, set()),
            "ADI-class (low-vol, mid-extended) must be caught by tier warn — "
            "calibration floor 7.5 must NOT mask tier warn 3.0",
        )


class TestRsLeaderPullbackStage(unittest.TestCase):
    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_minus_10_with_dipped_sma20_passes(self):
        # Dist exactly -10%, SMA20 -1 → pullback stage applies, passes
        self.assertTrue(_is_rs_leader_candidate(_rsl_row(), set()))

    @patch("agents.screener.finviz_agent._peel_warn_for", _permissive_peel)
    def test_minus_5_requires_strict_perfect(self):
        # Inside dist band (> -10%) — strict perfect required
        row = _rsl_row(**{"Dist From High%": -5.0})
        self.assertFalse(_is_rs_leader_candidate(row, set()))


if __name__ == "__main__":
    unittest.main()
