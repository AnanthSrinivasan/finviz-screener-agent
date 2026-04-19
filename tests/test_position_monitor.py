"""
Unit tests for agents/trading/position_monitor.py — the rules engine that
protects live capital. Covers:
  - peel threshold tiers (ATR-based) + per-ticker calibration override
  - apply_minervini_rules: stop hit, ATR trail, breakeven at +20%, trailing at +30%,
    gain fading, target1/target2
  - update_sizing_mode: suspended / reduced / aggressive / normal transitions
"""

import unittest
from unittest.mock import patch

from agents.trading import position_monitor as pm


class PeelThresholdTests(unittest.TestCase):
    def setUp(self):
        # Reset calibration cache between tests
        pm._PEEL_CALIBRATION_CACHE = {}

    def test_low_atr_tier(self):
        # <= 4% ATR → low tier: warn 3.0, signal 4.0
        self.assertEqual(pm.get_peel_thresholds(3.5), (3.0, 4.0))
        self.assertEqual(pm.get_peel_thresholds(4.0), (3.0, 4.0))

    def test_mid_atr_tier(self):
        # (4, 7] ATR → mid: warn 5.0, signal 6.0
        self.assertEqual(pm.get_peel_thresholds(5.0), (5.0, 6.0))
        self.assertEqual(pm.get_peel_thresholds(7.0), (5.0, 6.0))

    def test_high_atr_tier(self):
        # (7, 10] ATR → high: warn 6.5, signal 8.0
        self.assertEqual(pm.get_peel_thresholds(8.5), (6.5, 8.0))
        self.assertEqual(pm.get_peel_thresholds(10.0), (6.5, 8.0))

    def test_extreme_atr_tier(self):
        # > 10% ATR → extreme: warn 8.5, signal 10.0
        self.assertEqual(pm.get_peel_thresholds(12.0), (8.5, 10.0))
        self.assertEqual(pm.get_peel_thresholds(50.0), (8.5, 10.0))

    def test_calibrated_override_used_when_present(self):
        pm._PEEL_CALIBRATION_CACHE = {
            "SMCI": {"calibrated": True, "warn": 9.5, "signal": 13.2},
        }
        # Even though ATR=3% would normally hit low tier, calibration wins
        self.assertEqual(pm.get_peel_thresholds(3.0, ticker="SMCI"), (9.5, 13.2))

    def test_uncalibrated_ticker_falls_back_to_tier(self):
        pm._PEEL_CALIBRATION_CACHE = {
            "SMCI": {"calibrated": True, "warn": 9.5, "signal": 13.2},
        }
        # NVDA not in cache → use tier table
        self.assertEqual(pm.get_peel_thresholds(6.0, ticker="NVDA"), (5.0, 6.0))


class ApplyMinerviniRulesTests(unittest.TestCase):
    def _pos(self, **overrides):
        base = {
            "ticker":       "NVDA",
            "entry_price":  100.0,
            "stop":         95.0,
            "target1":      120.0,
            "target2":      140.0,
            "highest_price_seen": 100.0,
            "current_gain_pct":   0,
            "status":       "open",
        }
        base.update(overrides)
        return base

    def test_stop_hit_fires_alert(self):
        pos = self._pos(stop=95.0)
        alerts, modified = pm.apply_minervini_rules(pos, current_price=94.50, atr=2.0)
        self.assertTrue(any("STOP HIT" in a for a in alerts))
        self.assertEqual(pos["status"], "stop_hit")
        self.assertTrue(modified)

    def test_no_stop_alert_above_stop(self):
        pos = self._pos(stop=95.0)
        alerts, _ = pm.apply_minervini_rules(pos, current_price=96.0, atr=2.0)
        self.assertFalse(any("STOP HIT" in a for a in alerts))
        self.assertNotEqual(pos["status"], "stop_hit")

    def test_breakeven_activates_at_20_pct_gain(self):
        pos = self._pos(entry_price=100.0, stop=95.0,
                        target1=200.0, target2=300.0)  # lift targets out of range
        alerts, modified = pm.apply_minervini_rules(pos, current_price=120.0, atr=2.0)
        self.assertTrue(pos.get("breakeven_stop_activated"))
        # Stop must be at or above breakeven (100.50). ATR trail may already have
        # raised it higher (price − 2×ATR = 116) — that's fine; breakeven is a floor.
        self.assertGreaterEqual(pos["stop"], 100.5)
        self.assertTrue(any("stop moved to breakeven" in a for a in alerts))
        self.assertTrue(modified)

    def test_breakeven_floor_applies_when_no_atr(self):
        # atr=0 skips ATR trail entirely → breakeven floor of 100.5 applies
        pos = self._pos(entry_price=100.0, stop=95.0,
                        target1=200.0, target2=300.0)
        alerts, _ = pm.apply_minervini_rules(pos, current_price=120.0, atr=0)
        self.assertTrue(pos.get("breakeven_stop_activated"))
        self.assertAlmostEqual(pos["stop"], 100.5, places=2)
        self.assertTrue(any("stop moved to breakeven" in a for a in alerts))

    def test_trailing_stop_raises_at_30_pct_gain(self):
        pos = self._pos(entry_price=100.0, stop=100.5,
                        highest_price_seen=130.0,
                        target1=200.0, target2=300.0,
                        breakeven_stop_activated=True)
        alerts, _ = pm.apply_minervini_rules(pos, current_price=130.0, atr=2.0)
        # 10% trail from 130 = 117
        self.assertAlmostEqual(pos["stop"], 117.0, places=2)
        self.assertTrue(any("trailing stop raised" in a for a in alerts))

    def test_atr_trail_silent_before_breakeven(self):
        pos = self._pos(entry_price=100.0, stop=95.0,
                        target1=200.0, target2=300.0)
        alerts, modified = pm.apply_minervini_rules(pos, current_price=110.0, atr=2.0)
        # price - 2*ATR = 110 - 4 = 106 → stop raised silently (no alert text for ATR trail)
        self.assertAlmostEqual(pos["stop"], 106.0, places=2)
        self.assertTrue(modified)
        self.assertFalse(any("ATR trail" in a for a in alerts))

    def test_gain_fading_warning(self):
        pos = self._pos(entry_price=100.0, stop=100.5,
                        target1=200.0, target2=300.0,
                        breakeven_stop_activated=True)
        alerts, _ = pm.apply_minervini_rules(pos, current_price=102.0, atr=2.0)
        self.assertTrue(any("gain fading" in a for a in alerts))

    def test_target1_alert_fires_once(self):
        pos = self._pos(entry_price=100.0, target1=120.0, target2=140.0, stop=95.0)
        with patch.object(pm, "_save_winner_chart"):
            alerts, _ = pm.apply_minervini_rules(pos, current_price=120.5, atr=2.0)
        self.assertTrue(any("TARGET 1" in a for a in alerts))
        self.assertTrue(pos["target1_hit"])

        # Second run: target1_hit already true → should NOT re-alert
        with patch.object(pm, "_save_winner_chart"):
            alerts2, _ = pm.apply_minervini_rules(pos, current_price=121.0, atr=2.0)
        self.assertFalse(any("TARGET 1" in a for a in alerts2))

    def test_target2_alert(self):
        pos = self._pos(entry_price=100.0, target1=120.0, target2=140.0, stop=95.0,
                        target1_hit=True)
        alerts, _ = pm.apply_minervini_rules(pos, current_price=141.0, atr=2.0)
        self.assertTrue(any("TARGET 2" in a for a in alerts))

    def test_highest_price_seen_tracks_up(self):
        pos = self._pos(highest_price_seen=110.0)
        pm.apply_minervini_rules(pos, current_price=115.0, atr=2.0)
        self.assertEqual(pos["highest_price_seen"], 115.0)

    def test_highest_price_seen_does_not_decrease(self):
        pos = self._pos(highest_price_seen=120.0)
        pm.apply_minervini_rules(pos, current_price=115.0, atr=2.0)
        self.assertEqual(pos["highest_price_seen"], 120.0)


class UpdateSizingModeTests(unittest.TestCase):
    def _state(self, losses=0, wins=0, mode="normal"):
        return {
            "consecutive_losses": losses,
            "consecutive_wins":   wins,
            "current_sizing_mode": mode,
        }

    def test_three_losses_suspends(self):
        st = self._state(losses=3)
        alerts = pm.update_sizing_mode(st, market_state="GREEN")
        self.assertEqual(st["current_sizing_mode"], "suspended")
        self.assertTrue(any("SIZING SUSPENDED" in a for a in alerts))

    def test_two_losses_reduced(self):
        st = self._state(losses=2)
        alerts = pm.update_sizing_mode(st, market_state="GREEN")
        self.assertEqual(st["current_sizing_mode"], "reduced")
        self.assertTrue(any("SIZING REDUCED" in a for a in alerts))

    def test_aggressive_requires_wins_and_green_or_thrust(self):
        st = self._state(wins=2)
        pm.update_sizing_mode(st, market_state="GREEN")
        self.assertEqual(st["current_sizing_mode"], "aggressive")

        st = self._state(wins=2)
        pm.update_sizing_mode(st, market_state="THRUST")
        self.assertEqual(st["current_sizing_mode"], "aggressive")

    def test_aggressive_downgrades_in_red(self):
        # 2 wins but market is RED → not aggressive, just normal
        st = self._state(wins=2)
        pm.update_sizing_mode(st, market_state="RED")
        self.assertEqual(st["current_sizing_mode"], "normal")

    def test_normal_default(self):
        st = self._state()
        pm.update_sizing_mode(st, market_state="CAUTION")
        self.assertEqual(st["current_sizing_mode"], "normal")

    def test_no_alert_when_mode_unchanged(self):
        st = self._state(losses=3, mode="suspended")
        alerts = pm.update_sizing_mode(st, market_state="RED")
        self.assertEqual(st["current_sizing_mode"], "suspended")
        self.assertEqual(alerts, [])


if __name__ == "__main__":
    unittest.main()
