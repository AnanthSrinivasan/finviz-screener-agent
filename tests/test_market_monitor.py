"""
Unit tests for agents/market/market_monitor.py — the state classifier that gates
all new entries. These are the core rules: the system's "master switch" for
when new positions are allowed.
"""

import datetime
import unittest

from agents.market import market_monitor as mm


class BlackoutTests(unittest.TestCase):
    """Blackout = February (all month) + September (all month)."""

    def test_september_is_blackout(self):
        self.assertTrue(mm.is_blackout(datetime.date(2026, 9, 1)))
        self.assertTrue(mm.is_blackout(datetime.date(2026, 9, 15)))
        self.assertTrue(mm.is_blackout(datetime.date(2026, 9, 30)))

    def test_february_is_blackout(self):
        self.assertTrue(mm.is_blackout(datetime.date(2026, 2, 1)))
        self.assertTrue(mm.is_blackout(datetime.date(2026, 2, 15)))
        self.assertTrue(mm.is_blackout(datetime.date(2026, 2, 28)))
        # Leap year end-of-Feb
        self.assertTrue(mm.is_blackout(datetime.date(2024, 2, 29)))

    def test_october_is_not_blackout(self):
        # Prior code had Oct 1-15 as blackout — removed.
        self.assertFalse(mm.is_blackout(datetime.date(2026, 10, 1)))
        self.assertFalse(mm.is_blackout(datetime.date(2026, 10, 15)))
        self.assertFalse(mm.is_blackout(datetime.date(2026, 10, 31)))

    def test_march_not_blackout(self):
        # Prior code had Mar 1-15 as blackout — removed (profitable in 2024+2025).
        self.assertFalse(mm.is_blackout(datetime.date(2026, 3, 1)))
        self.assertFalse(mm.is_blackout(datetime.date(2026, 3, 15)))

    def test_other_months_not_blackout(self):
        for m in (1, 3, 4, 5, 6, 7, 8, 10, 11, 12):
            self.assertFalse(mm.is_blackout(datetime.date(2026, m, 15)),
                             f"month {m} unexpectedly flagged blackout")


class CalculateMetricsTests(unittest.TestCase):
    def test_ratio_today(self):
        today = {"up_4_today": 200, "down_4_today": 50, "spy_sma200_pct": 2.0}
        out = mm.calculate_metrics([], today)
        self.assertEqual(out["ratio_today"], 4.0)

    def test_thrust_detection(self):
        # THRUST_THRESHOLD is 500
        today = {"up_4_today": 500, "down_4_today": 100, "spy_sma200_pct": 2.0}
        self.assertTrue(mm.calculate_metrics([], today)["thrust"])
        today = {"up_4_today": 499, "down_4_today": 100, "spy_sma200_pct": 2.0}
        self.assertFalse(mm.calculate_metrics([], today)["thrust"])

    def test_spy_above_200d(self):
        today = {"up_4_today": 10, "down_4_today": 10, "spy_sma200_pct": 1.5}
        self.assertTrue(mm.calculate_metrics([], today)["spy_above_200d"])
        today["spy_sma200_pct"] = -1.5
        self.assertFalse(mm.calculate_metrics([], today)["spy_above_200d"])
        today["spy_sma200_pct"] = None
        self.assertFalse(mm.calculate_metrics([], today)["spy_above_200d"])

    def test_divide_by_zero_safe(self):
        # No decliners — ratio becomes up/max(down,1), should not explode
        today = {"up_4_today": 100, "down_4_today": 0, "spy_sma200_pct": 2.0}
        out = mm.calculate_metrics([], today)
        self.assertEqual(out["ratio_today"], 100.0)

    def test_5day_and_10day_include_today(self):
        history = [{"up_4_today": 50, "down_4_today": 50} for _ in range(10)]
        today = {"up_4_today": 200, "down_4_today": 100, "spy_sma200_pct": 1.0}
        out = mm.calculate_metrics(history, today)
        # 5d: (50*4 + 200) / (50*4 + 100) = 400 / 300 = 1.33
        self.assertAlmostEqual(out["ratio_5day"], 1.33, places=2)
        # 10d: (50*9 + 200) / (50*9 + 100) = 650 / 550 = 1.18
        self.assertAlmostEqual(out["ratio_10day"], 1.18, places=2)


class ClassifyMarketStateTests(unittest.TestCase):
    """All 7 states, checked in priority order."""

    def _date(self, m=4, d=15):
        return datetime.date(2026, m, d)

    def _metrics(self, ratio_5=2.5, ratio_10=1.8, thrust=False, spy_above=True):
        return {
            "ratio_today":    ratio_5,
            "ratio_5day":     ratio_5,
            "ratio_10day":    ratio_10,
            "thrust":         thrust,
            "spy_above_200d": spy_above,
        }

    def _today(self, up=10, down=10):
        return {"up_4_today": up, "down_4_today": down}

    def test_blackout_overrides_everything(self):
        # Even with THRUST conditions, Sep = BLACKOUT
        state, _, _ = mm.classify_market_state(
            self._metrics(thrust=True), fg=80,
            spy_price=500, spy_above_200d=True,
            today_data=self._today(up=600), date=self._date(m=9, d=15),
        )
        self.assertEqual(state, "BLACKOUT")

    def test_danger_fires_on_heavy_down_day(self):
        # 500+ down + 5d ratio < 0.5 → DANGER
        state, _, _ = mm.classify_market_state(
            self._metrics(ratio_5=0.4, thrust=False),
            fg=30, spy_price=500, spy_above_200d=True,
            today_data=self._today(up=10, down=600),
            date=self._date(),
        )
        self.assertEqual(state, "DANGER")

    def test_danger_beats_thrust_on_collapse_day(self):
        # A single day can show both — DANGER must win (checked first)
        state, _, _ = mm.classify_market_state(
            self._metrics(ratio_5=0.4, thrust=True),
            fg=30, spy_price=500, spy_above_200d=True,
            today_data=self._today(up=600, down=600),
            date=self._date(),
        )
        self.assertEqual(state, "DANGER")

    def test_cooling_from_green(self):
        # Previous state was GREEN, conditions deteriorated → COOLING
        state, _, _ = mm.classify_market_state(
            self._metrics(ratio_5=1.5, ratio_10=1.2),  # below GREEN thresholds
            fg=40, spy_price=500, spy_above_200d=True,
            today_data=self._today(),
            date=self._date(),
            prev_state="GREEN",
        )
        self.assertEqual(state, "COOLING")

    def test_cooling_does_not_fire_if_prev_not_green(self):
        # Same weakened conditions but coming from RED — should not be COOLING
        state, _, _ = mm.classify_market_state(
            self._metrics(ratio_5=1.5, ratio_10=1.2),
            fg=40, spy_price=500, spy_above_200d=True,
            today_data=self._today(),
            date=self._date(),
            prev_state="RED",
        )
        self.assertNotEqual(state, "COOLING")

    def test_thrust_single_day_500(self):
        state, _, _ = mm.classify_market_state(
            self._metrics(ratio_5=1.0, ratio_10=1.0, thrust=True),
            fg=30, spy_price=500, spy_above_200d=False,
            today_data=self._today(up=550),
            date=self._date(),
        )
        self.assertEqual(state, "THRUST")

    def test_green_full_conditions(self):
        state, _, _ = mm.classify_market_state(
            self._metrics(ratio_5=2.5, ratio_10=1.8),
            fg=50, spy_price=500, spy_above_200d=True,
            today_data=self._today(),
            date=self._date(),
        )
        self.assertEqual(state, "GREEN")

    def test_caution_half_size_building(self):
        state, _, _ = mm.classify_market_state(
            self._metrics(ratio_5=1.6, ratio_10=1.0),  # below GREEN 5d=2.0 but above CAUTION 1.5
            fg=30, spy_price=500, spy_above_200d=True,
            today_data=self._today(),
            date=self._date(),
        )
        self.assertEqual(state, "CAUTION")

    def test_red_default_when_below_200d(self):
        state, _, _ = mm.classify_market_state(
            self._metrics(ratio_5=2.5, ratio_10=1.8),  # strong breadth
            fg=70, spy_price=400, spy_above_200d=False,  # but SPY below 200d
            today_data=self._today(),
            date=self._date(),
        )
        self.assertEqual(state, "RED")

    def test_red_when_ratios_weak(self):
        state, _, _ = mm.classify_market_state(
            self._metrics(ratio_5=0.9, ratio_10=0.9),
            fg=30, spy_price=500, spy_above_200d=True,
            today_data=self._today(),
            date=self._date(),
        )
        self.assertEqual(state, "RED")


class ConfidenceLayerTests(unittest.TestCase):
    """Layer 1 (post-THRUST floor), Layer 2a (extreme greed), Layer 2b (extreme fear)."""

    def _date(self, y=2026, m=4, d=15):
        return datetime.date(y, m, d)

    def _metrics(self, ratio_5=0.9, ratio_10=0.9, thrust=False, spy_above=True):
        return {
            "ratio_today":    ratio_5,
            "ratio_5day":     ratio_5,
            "ratio_10day":    ratio_10,
            "thrust":         thrust,
            "spy_above_200d": spy_above,
        }

    def _today(self, up=10, down=10):
        return {"up_4_today": up, "down_4_today": down}

    # ------------------------------------------------------------------ Layer 1
    def test_layer1_thrust_floor_overrides_red_day1(self):
        # THRUST on Apr 30, RED conditions on May 1 (1 calendar day) → CAUTION
        thrust_date = "2026-04-30"
        state, msg, ctx = mm.classify_market_state(
            self._metrics(ratio_5=0.8),
            fg=40, spy_price=500, spy_above_200d=True,
            today_data=self._today(up=50, down=200),
            date=self._date(m=5, d=1),
            prev_state="THRUST",
            last_thrust_date=thrust_date,
        )
        self.assertEqual(state, "CAUTION")
        self.assertTrue(ctx["post_thrust_floor_active"])
        self.assertIn("Post-THRUST floor", msg)

    def test_layer1_thrust_floor_day2(self):
        # THRUST on Apr 30, RED conditions on May 2 (2 days) → still CAUTION
        state, _, ctx = mm.classify_market_state(
            self._metrics(ratio_5=0.8),
            fg=40, spy_price=500, spy_above_200d=True,
            today_data=self._today(up=50, down=200),
            date=self._date(m=5, d=2),
            prev_state="RED",
            last_thrust_date="2026-04-30",
        )
        self.assertEqual(state, "CAUTION")
        self.assertTrue(ctx["post_thrust_floor_active"])

    def test_layer1_thrust_floor_day3(self):
        # 3 calendar days after THRUST — floor still active
        state, _, ctx = mm.classify_market_state(
            self._metrics(ratio_5=0.8),
            fg=40, spy_price=500, spy_above_200d=True,
            today_data=self._today(up=50, down=200),
            date=self._date(m=5, d=3),
            prev_state="CAUTION",
            last_thrust_date="2026-04-30",
        )
        self.assertEqual(state, "CAUTION")
        self.assertTrue(ctx["post_thrust_floor_active"])

    def test_layer1_thrust_floor_expired_day4(self):
        # 4 calendar days after THRUST — floor expired → RED
        state, _, ctx = mm.classify_market_state(
            self._metrics(ratio_5=0.8),
            fg=40, spy_price=500, spy_above_200d=True,
            today_data=self._today(up=50, down=200),
            date=self._date(m=5, d=4),
            prev_state="CAUTION",
            last_thrust_date="2026-04-30",
        )
        self.assertEqual(state, "RED")
        self.assertFalse(ctx["post_thrust_floor_active"])

    def test_layer1_danger_bypasses_floor(self):
        # DANGER overrides the post-THRUST floor — emergency signal always wins
        state, _, ctx = mm.classify_market_state(
            self._metrics(ratio_5=0.4),
            fg=40, spy_price=500, spy_above_200d=True,
            today_data=self._today(up=10, down=600),
            date=self._date(m=5, d=1),
            prev_state="THRUST",
            last_thrust_date="2026-04-30",
        )
        self.assertEqual(state, "DANGER")
        self.assertFalse(ctx["post_thrust_floor_active"])

    # ------------------------------------------------------------------ Layer 2a
    def test_layer2a_extreme_greed_green_to_cooling_day1(self):
        # GREEN + F&G=80, 1 bad breadth day → COOLING fires (same as normal, no skip on day 1)
        state, _, ctx = mm.classify_market_state(
            self._metrics(ratio_5=1.5, ratio_10=1.2),  # below GREEN
            fg=80, spy_price=500, spy_above_200d=True,
            today_data=self._today(),
            date=self._date(),
            prev_state="GREEN",
        )
        self.assertEqual(state, "COOLING")
        self.assertEqual(ctx["confidence_context"], "extreme_greed_caution")

    def test_layer2a_extreme_greed_skips_cooling_buffer(self):
        # COOLING prev + extreme greed: 3b buffer does NOT apply → falls to RED
        # (normal F&G would sustain COOLING for 2nd day; extreme greed skips that)
        state, _, _ = mm.classify_market_state(
            self._metrics(ratio_5=0.8, spy_above=True),
            fg=80, spy_price=500, spy_above_200d=True,
            today_data=self._today(up=50, down=200),
            date=self._date(),
            prev_state="COOLING",
            consecutive_weak_days=1,
        )
        # With extreme greed and cwd=1, rule 3b is skipped → RED
        self.assertEqual(state, "RED")

    def test_layer2a_normal_fg_sustains_cooling_buffer(self):
        # Same scenario but F&G=50 (normal range) → 3b buffer fires → COOLING sustained
        state, _, _ = mm.classify_market_state(
            self._metrics(ratio_5=0.8, spy_above=True),
            fg=50, spy_price=500, spy_above_200d=True,
            today_data=self._today(up=50, down=200),
            date=self._date(),
            prev_state="COOLING",
            consecutive_weak_days=1,
        )
        self.assertEqual(state, "COOLING")

    def test_layer2a_cooling_buffer_expires_at_2(self):
        # Normal F&G but consecutive_weak_days=2 → buffer exhausted → RED
        state, _, _ = mm.classify_market_state(
            self._metrics(ratio_5=0.8, spy_above=True),
            fg=50, spy_price=500, spy_above_200d=True,
            today_data=self._today(up=50, down=200),
            date=self._date(),
            prev_state="COOLING",
            consecutive_weak_days=2,
        )
        self.assertEqual(state, "RED")

    # ------------------------------------------------------------------ Layer 2b
    def test_layer2b_extreme_fear_thrust_from_red(self):
        # RED + F&G=20, THRUST day → CAUTION + high_confidence_recovery tag
        state, msg, ctx = mm.classify_market_state(
            self._metrics(ratio_5=1.0, thrust=True),
            fg=20, spy_price=500, spy_above_200d=False,
            today_data=self._today(up=550),
            date=self._date(),
            prev_state="RED",
        )
        self.assertEqual(state, "CAUTION")
        self.assertEqual(ctx["confidence_context"], "high_confidence_recovery")
        self.assertIn("High-confidence recovery", msg)
        self.assertIn("Extreme Fear", msg)

    def test_layer2b_extreme_fear_thrust_from_danger(self):
        # DANGER + F&G=18, THRUST day → CAUTION (same high-confidence path)
        state, _, ctx = mm.classify_market_state(
            self._metrics(ratio_5=1.0, thrust=True),
            fg=18, spy_price=500, spy_above_200d=False,
            today_data=self._today(up=550),
            date=self._date(),
            prev_state="DANGER",
        )
        self.assertEqual(state, "CAUTION")
        self.assertEqual(ctx["confidence_context"], "high_confidence_recovery")

    def test_layer2b_normal_fg_thrust_stays_thrust(self):
        # Normal F&G + THRUST from RED — no override, state stays THRUST
        state, _, ctx = mm.classify_market_state(
            self._metrics(ratio_5=1.0, thrust=True),
            fg=40, spy_price=500, spy_above_200d=False,
            today_data=self._today(up=550),
            date=self._date(),
            prev_state="RED",
        )
        self.assertEqual(state, "THRUST")
        self.assertIsNone(ctx["confidence_context"])

    def test_layer2b_extreme_fear_no_thrust_stays_red(self):
        # Extreme fear but NO THRUST — no Layer 2b upgrade, falls to RED
        state, _, ctx = mm.classify_market_state(
            self._metrics(ratio_5=0.8),
            fg=20, spy_price=500, spy_above_200d=True,
            today_data=self._today(up=100, down=200),
            date=self._date(),
            prev_state="RED",
        )
        self.assertEqual(state, "RED")
        self.assertIsNone(ctx["confidence_context"])


if __name__ == "__main__":
    unittest.main()
