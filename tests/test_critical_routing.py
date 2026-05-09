"""Tests for critical-vs-digest event routing."""

import unittest

from agents.trading import rules


class CriticalKindsTests(unittest.TestCase):
    def test_stop_hit_is_critical(self):
        self.assertIn("stop_hit", rules.CRITICAL_EVENT_KINDS)

    def test_target_hits_are_critical(self):
        self.assertIn("target1", rules.CRITICAL_EVENT_KINDS)
        self.assertIn("target2", rules.CRITICAL_EVENT_KINDS)

    def test_share_drift_is_critical(self):
        self.assertIn("share_drift_avg_up", rules.CRITICAL_EVENT_KINDS)
        self.assertIn("share_drift_partial_sell", rules.CRITICAL_EVENT_KINDS)

    def test_auto_close_is_critical(self):
        self.assertIn("auto_closed", rules.CRITICAL_EVENT_KINDS)

    def test_hard_stop_is_critical(self):
        self.assertIn("hard_stop", rules.CRITICAL_EVENT_KINDS)

    def test_peel_warn_is_not_critical(self):
        self.assertNotIn("peel_warn", rules.CRITICAL_EVENT_KINDS)
        self.assertNotIn("peel_signal", rules.CRITICAL_EVENT_KINDS)
        self.assertNotIn("ma_trail", rules.CRITICAL_EVENT_KINDS)
        self.assertNotIn("fade", rules.CRITICAL_EVENT_KINDS)
        self.assertNotIn("breakeven", rules.CRITICAL_EVENT_KINDS)


class TargetFirstHitGuardTests(unittest.TestCase):
    """Target1 / Target2 events should fire ONCE on first cross — subsequent
    runs while still above target must not re-emit. This is what makes them
    safe to mark critical.
    """

    def _pos(self):
        return {
            "entry_price": 100, "stop_price": 95, "atr_pct": 4.0,
            "highest_price_seen": 100, "peak_gain_pct": 0,
            "target1": 120, "target2": 140,
        }

    def test_target1_emits_once(self):
        pos = self._pos()
        ev1, _ = rules.apply_position_rules("X", pos, 121, 121, atr_pct=4.0)
        ev2, _ = rules.apply_position_rules("X", pos, 122, 122, atr_pct=4.0)
        kinds1 = [e["kind"] for e in ev1]
        kinds2 = [e["kind"] for e in ev2]
        self.assertIn("target1", kinds1)
        self.assertNotIn("target1", kinds2)

    def test_target2_emits_once(self):
        pos = self._pos()
        ev1, _ = rules.apply_position_rules("X", pos, 141, 141, atr_pct=4.0)
        ev2, _ = rules.apply_position_rules("X", pos, 142, 142, atr_pct=4.0)
        kinds1 = [e["kind"] for e in ev1]
        kinds2 = [e["kind"] for e in ev2]
        self.assertIn("target2", kinds1)
        self.assertNotIn("target2", kinds2)


if __name__ == "__main__":
    unittest.main()
