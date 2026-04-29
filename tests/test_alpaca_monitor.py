"""
Unit tests for agents/trading/alpaca_monitor.py — schema migration and the
trailing-rules engine ported from the real-account position monitor.
"""

import unittest

from agents.trading import alpaca_monitor as am


class MigrateStopEntryTests(unittest.TestCase):
    def test_adds_missing_fields_with_defaults(self):
        entry = {"stop_price": 90.0, "entry_price": 100.0, "atr_pct": 5.0,
                 "entry_date": "2026-04-01"}
        out = am.migrate_stop_entry("FOO", entry, 100.0)
        self.assertEqual(out["highest_price_seen"], 100.0)
        self.assertEqual(out["peak_gain_pct"], 0.0)
        self.assertFalse(out["breakeven_activated"])
        self.assertEqual(out["target1"], 120.0)  # +20%
        self.assertEqual(out["target2"], 140.0)  # +40%
        self.assertFalse(out["target1_hit"])
        # Original fields untouched
        self.assertEqual(out["stop_price"], 90.0)

    def test_idempotent_does_not_overwrite(self):
        entry = {
            "stop_price": 90.0, "entry_price": 100.0,
            "highest_price_seen": 175.0, "peak_gain_pct": 75.0,
            "breakeven_activated": True, "target1": 120.0, "target2": 140.0,
            "target1_hit": True,
        }
        out = am.migrate_stop_entry("FOO", entry, 100.0)
        self.assertEqual(out["highest_price_seen"], 175.0)
        self.assertEqual(out["peak_gain_pct"], 75.0)
        self.assertTrue(out["breakeven_activated"])
        self.assertTrue(out["target1_hit"])


class ApplyPaperRulesTests(unittest.TestCase):
    def _entry(self, **overrides):
        base = {
            "stop_price": 90.0,
            "entry_price": 100.0,
            "atr_pct": 4.0,  # ATR$ = 4 at entry
            "entry_date": "2026-04-01",
            "highest_price_seen": 100.0,
            "peak_gain_pct": 0.0,
            "breakeven_activated": False,
            "target1": 120.0,
            "target2": 140.0,
            "target1_hit": False,
        }
        base.update(overrides)
        return base

    def test_atr_trail_raises_silently_before_breakeven(self):
        entry = self._entry()
        # price=110, atr_pct=4% → atr$=4 (on entry price), trail = 110 - 8 = 102
        events, _ = am.apply_paper_rules("FOO", entry, 110.0, day_high=110.0, atr_pct=4.0)
        self.assertEqual(entry["stop_price"], 102.0)
        self.assertFalse(any("trailing" in e["message"].lower() for e in events))
        self.assertFalse(any("breakeven" in e["message"].lower() for e in events))

    def test_breakeven_fires_and_sets_stop(self):
        entry = self._entry()
        events, _ = am.apply_paper_rules("FOO", entry, 120.0, day_high=120.0, atr_pct=4.0)
        self.assertTrue(entry["breakeven_activated"])
        self.assertGreaterEqual(entry["stop_price"], 100.5)
        self.assertTrue(any("breakeven" in e["message"].lower() for e in events))

    def test_target1_alert_once(self):
        entry = self._entry()
        events, _ = am.apply_paper_rules("FOO", entry, 120.0, day_high=120.0, atr_pct=4.0)
        self.assertTrue(any("TARGET 1" in e["message"] for e in events))
        self.assertTrue(entry["target1_hit"])

        # Second tick above T1 — no re-fire
        events2, _ = am.apply_paper_rules("FOO", entry, 125.0, day_high=125.0, atr_pct=4.0)
        self.assertFalse(any("TARGET 1" in e["message"] for e in events2))

    def test_trail_30pct_fires_and_raises_stop(self):
        entry = self._entry(
            highest_price_seen=130.0, peak_gain_pct=30.0,
            breakeven_activated=True, target1_hit=True, stop_price=100.5,
        )
        events, _ = am.apply_paper_rules("FOO", entry, 130.0, day_high=130.0, atr_pct=4.0)
        # 10% trail = 117
        self.assertAlmostEqual(entry["stop_price"], 117.0, places=2)
        self.assertTrue(any("trailing stop raised" in e["message"] for e in events))

    def test_fade_fires_when_price_drops_one_atr(self):
        entry = self._entry(
            highest_price_seen=125.0, peak_gain_pct=25.0,
            breakeven_activated=True, target1_hit=True, stop_price=100.5,
        )
        # atr_pct=4% on entry_price=100 → atr$=4. High 125, price 120 = 5 below → fires
        events, _ = am.apply_paper_rules("FOO", entry, 120.0, day_high=125.0, atr_pct=4.0)
        self.assertTrue(any("fading" in e["message"] for e in events))

    def test_fade_does_not_fire_within_one_atr(self):
        entry = self._entry(
            highest_price_seen=125.0, peak_gain_pct=25.0,
            breakeven_activated=True, target1_hit=True, stop_price=100.5,
        )
        # High 125, price 123 = 2 below, ATR$=4 → still within 1×ATR
        events, _ = am.apply_paper_rules("FOO", entry, 123.0, day_high=125.0, atr_pct=4.0)
        self.assertFalse(any("fading" in e["message"] for e in events))

    def test_day_high_updates_highest_price_seen(self):
        entry = self._entry()
        # current=115, day_high=130 → highest_price_seen should be 130
        am.apply_paper_rules("FOO", entry, 115.0, day_high=130.0, atr_pct=4.0)
        self.assertEqual(entry["highest_price_seen"], 130.0)
        self.assertAlmostEqual(entry["peak_gain_pct"], 30.0, places=1)


if __name__ == "__main__":
    unittest.main()
