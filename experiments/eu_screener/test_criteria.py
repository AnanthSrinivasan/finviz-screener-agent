"""Tests for the EU screener experiment.

The point of these: the criteria engine can be validated with NO market data
access and NO coupling to the production system, by feeding it rows whose
correct verdict is already known from a chart.

RHEINMETALL is the anchor fixture. On 2026-09-06 the user charted XETR:RHM and
the model had claimed European defense showed "Stage 2 structure". The chart
showed the opposite. Every number below is read off that chart, and the
engine's own ATR multiple reproduces TradingView's -1.37 reading, which is what
makes this a real check rather than a self-consistent one.
"""
import unittest
from criteria import evaluate, stage, atr_multiple, tier_warn


# XETR:RHM, daily, 2026-09-05 close — values read from the user's chart.
RHM = {"ticker": "RHM", "close": 1034.0, "sma20": 1093.3, "sma50": 1095.0,
       "sma200": 1396.5, "atr_pct": 4.08, "high_52w": 2008.0, "rvol": 1.12}


class RheinmetallRegression(unittest.TestCase):
    def test_reproduces_tradingview_atr_multiple(self):
        r = evaluate(RHM)
        # TradingView's "ATR% Multiple From MA" printed -1.37 on this bar.
        self.assertAlmostEqual(r["atr_multiple"], -1.37, places=2)
        # and its "% Gain From MA" printed -5.57%
        self.assertAlmostEqual(r["sma50_pct"], -5.57, places=2)

    def test_is_stage_4_not_stage_2(self):
        r = evaluate(RHM)
        self.assertEqual(r["stage"]["stage"], 4)
        self.assertIn("markdown", r["stage"]["label"])

    def test_is_rejected_by_the_gate(self):
        r = evaluate(RHM)
        self.assertFalse(r["passes"])
        self.assertIn("stage 4", r["fails"])

    def test_rejected_on_distance_from_high_too(self):
        r = evaluate(RHM)
        self.assertLess(r["dist_high_pct"], -48.0)   # -48.5% from the 2008 high
        self.assertTrue(any("dist" in f for f in r["fails"]))


class GateMechanics(unittest.TestCase):
    def test_clean_stage2_setup_passes(self):
        row = {"ticker": "OK", "close": 100.0, "sma20": 98.0, "sma50": 95.0,
               "sma200": 80.0, "atr_pct": 3.0, "high_52w": 106.0, "rvol": 0.8}
        r = evaluate(row)
        self.assertEqual(r["stage"]["stage"], 2)
        self.assertTrue(r["stage"]["perfect"])
        self.assertTrue(r["passes"], r["fails"])

    def test_extended_name_fails_peel_safe(self):
        # Stage 2 and near the high, but 8x ATR above the 50 MA.
        row = {"ticker": "EXT", "close": 100.0, "sma20": 98.0, "sma50": 76.0,
               "sma200": 60.0, "atr_pct": 4.0, "high_52w": 101.0, "rvol": 0.9}
        r = evaluate(row)
        self.assertEqual(r["stage"]["stage"], 2)
        self.assertFalse(r["passes"])
        self.assertTrue(any("extended" in f for f in r["fails"]))

    def test_high_rvol_chase_fails(self):
        row = {"ticker": "CHASE", "close": 100.0, "sma20": 98.0, "sma50": 95.0,
               "sma200": 80.0, "atr_pct": 3.0, "high_52w": 106.0, "rvol": 2.4}
        self.assertTrue(any("RVol" in f for f in evaluate(row)["fails"]))

    def test_tier_warn_ladder(self):
        self.assertEqual(tier_warn(2.0), 3.0)
        self.assertEqual(tier_warn(6.0), 5.0)
        self.assertEqual(tier_warn(9.0), 6.5)
        self.assertEqual(tier_warn(20.0), 8.5)

    def test_atr_multiple_is_sma50pct_over_atrpct(self):
        self.assertAlmostEqual(atr_multiple(10.0, 2.5), 4.0)

    def test_stage4_needs_price_below_both(self):
        self.assertEqual(stage(-5, -6, -20)["stage"], 4)
        self.assertEqual(stage(1, 5, 20)["stage"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
