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


class ExtendedAndSteadyUptrendTests(unittest.TestCase):
    """v2 ladder additions (May 2026): EXTENDED + STEADY-UPTREND.

    EXTENDED is the "no chase" guardrail — fires when SPY/QQQ stretch far
    above their 50MA. Overrides all bullish tiers (THRUST/GREEN/CAUTION).
    STEADY-UPTREND fills the gap between thrust-day bullish tiers and RED —
    half-size entries on a trending tape; guarded so a bear bounce can't
    sneak in.
    """

    def _date(self, m=5, d=14):
        return datetime.date(2026, m, d)

    def _metrics(self, ratio_5=1.0, ratio_10=1.0, thrust=False, spy_above=True):
        return {
            "ratio_today":   ratio_5,
            "ratio_5day":    ratio_5,
            "ratio_10day":   ratio_10,
            "thrust":        thrust,
            "spy_above_200d": spy_above,
        }

    def _today(self, up=10, down=10, spy_atr_mult=None,
               spy_sma50=None, qqq_atr_mult=None):
        return {
            "up_4_today":      up,
            "down_4_today":    down,
            "spy_atr_mult_50": spy_atr_mult,
            "spy_sma50_pct":   spy_sma50,
            "qqq_atr_mult_50": qqq_atr_mult,
        }

    def test_is_extended_helpers(self):
        # SPY ATR mult trigger
        self.assertTrue(mm.is_extended(7.0, 5.0, 4.0))
        self.assertFalse(mm.is_extended(6.99, 5.0, 4.0))
        # SPY %above-50 trigger
        self.assertTrue(mm.is_extended(3.0, 8.0, 4.0))
        # QQQ ATR mult trigger (SPY moderate)
        self.assertTrue(mm.is_extended(3.0, 5.0, 9.0))
        # All None → not extended
        self.assertFalse(mm.is_extended(None, None, None))

    def test_extended_overrides_thrust(self):
        # May 6 2026 scenario: THRUST day at parabolic levels — must NOT chase
        state, msg, _ = mm.classify_market_state(
            self._metrics(thrust=True, ratio_5=1.5),
            fg=70, spy_price=748, spy_above_200d=True,
            today_data=self._today(up=600, down=100,
                                   spy_atr_mult=7.5, spy_sma50=7.0,
                                   qqq_atr_mult=9.5),
            date=self._date(),
            prev_state="CAUTION",
        )
        self.assertEqual(state, "EXTENDED")
        self.assertIn("Parabolic", msg)

    def test_extended_overrides_green(self):
        state, _, _ = mm.classify_market_state(
            self._metrics(ratio_5=2.5, ratio_10=1.8),
            fg=70, spy_price=748, spy_above_200d=True,
            today_data=self._today(up=300, down=100,
                                   spy_atr_mult=8.0, spy_sma50=8.5,
                                   qqq_atr_mult=10.0),
            date=self._date(),
            prev_state="GREEN",
        )
        self.assertEqual(state, "EXTENDED")

    def test_extended_today_2026_05_14_inputs(self):
        # Live inputs from data/market_monitor_2026-05-14.json + Alpaca backtest.
        # v4 (May 2026): EXTENDED cannot trip directly from RED — must come up
        # through THRUST/CAUTION first. The historic path into the parabolic
        # phase actually ran via CAUTION; this test pins the metric-trip case
        # against an allowed prev_state.
        state, _, _ = mm.classify_market_state(
            self._metrics(ratio_5=0.95, ratio_10=1.17, thrust=False),
            fg=66.1, spy_price=748.17, spy_above_200d=True,
            today_data=self._today(up=252, down=181,
                                   spy_atr_mult=8.6, spy_sma50=8.6,
                                   qqq_atr_mult=10.04),
            date=self._date(m=5, d=14),
            prev_state="CAUTION",
        )
        self.assertEqual(state, "EXTENDED")

    def test_danger_still_beats_extended(self):
        # Collapse day at extended levels → DANGER wins
        state, _, _ = mm.classify_market_state(
            self._metrics(ratio_5=0.4),
            fg=50, spy_price=700, spy_above_200d=True,
            today_data=self._today(up=20, down=600,
                                   spy_atr_mult=8.0, spy_sma50=8.5,
                                   qqq_atr_mult=10.0),
            date=self._date(),
            prev_state="GREEN",
        )
        self.assertEqual(state, "DANGER")

    def test_extended_skipped_during_blackout(self):
        state, _, _ = mm.classify_market_state(
            self._metrics(ratio_5=1.0),
            fg=70, spy_price=700, spy_above_200d=True,
            today_data=self._today(up=100, down=100,
                                   spy_atr_mult=9.0, spy_sma50=9.0),
            date=datetime.date(2026, 9, 15),
            prev_state="GREEN",
        )
        self.assertEqual(state, "BLACKOUT")

    def test_steady_uptrend_fires_from_caution(self):
        # SPY > 200d, > 50d, F&G ≥ 50, up≥dn, ratio_5d ≥ 0.9, not extended,
        # prev=CAUTION → STEADY-UPTREND.
        state, _, _ = mm.classify_market_state(
            self._metrics(ratio_5=1.0, ratio_10=1.1),
            fg=55, spy_price=500, spy_above_200d=True,
            today_data=self._today(up=120, down=100,
                                   spy_atr_mult=3.0, spy_sma50=3.0,
                                   qqq_atr_mult=4.0),
            date=self._date(),
            prev_state="CAUTION",
        )
        self.assertEqual(state, "STEADY-UPTREND")

    def test_steady_blocked_from_red(self):
        # Same metrics but prev=RED → not allowed; path out of RED stays
        # via THRUST.
        state, _, _ = mm.classify_market_state(
            self._metrics(ratio_5=1.0, ratio_10=1.1),
            fg=55, spy_price=500, spy_above_200d=True,
            today_data=self._today(up=120, down=100,
                                   spy_atr_mult=3.0, spy_sma50=3.0,
                                   qqq_atr_mult=4.0),
            date=self._date(),
            prev_state="RED",
        )
        self.assertEqual(state, "RED")

    def test_steady_blocked_when_below_50ma(self):
        # spy_sma50_pct = -1 → not above 50MA → no STEADY
        state, _, _ = mm.classify_market_state(
            self._metrics(ratio_5=1.0),
            fg=55, spy_price=500, spy_above_200d=True,
            today_data=self._today(up=120, down=100,
                                   spy_atr_mult=-0.5, spy_sma50=-1.0,
                                   qqq_atr_mult=0.0),
            date=self._date(),
            prev_state="CAUTION",
        )
        self.assertNotEqual(state, "STEADY-UPTREND")

    def test_steady_blocked_when_fg_below_50(self):
        state, _, _ = mm.classify_market_state(
            self._metrics(ratio_5=1.0),
            fg=45, spy_price=500, spy_above_200d=True,
            today_data=self._today(up=120, down=100,
                                   spy_atr_mult=3.0, spy_sma50=3.0,
                                   qqq_atr_mult=4.0),
            date=self._date(),
            prev_state="CAUTION",
        )
        self.assertNotEqual(state, "STEADY-UPTREND")

    def test_steady_blocked_when_down_exceeds_up(self):
        state, _, _ = mm.classify_market_state(
            self._metrics(ratio_5=0.95),
            fg=55, spy_price=500, spy_above_200d=True,
            today_data=self._today(up=100, down=150,
                                   spy_atr_mult=3.0, spy_sma50=3.0,
                                   qqq_atr_mult=4.0),
            date=self._date(),
            prev_state="CAUTION",
        )
        self.assertNotEqual(state, "STEADY-UPTREND")

    def test_thrust_still_fires_off_bottom(self):
        # Regression: ensure regular THRUST path still works at non-extended levels
        state, _, _ = mm.classify_market_state(
            self._metrics(ratio_5=1.0, ratio_10=1.0, thrust=True),
            fg=30, spy_price=500, spy_above_200d=False,
            today_data=self._today(up=550, down=50,
                                   spy_atr_mult=0.5, spy_sma50=0.5,
                                   qqq_atr_mult=0.5),
            date=self._date(),
            prev_state="RED",
        )
        self.assertEqual(state, "THRUST")


class TrendFollowHelperTests(unittest.TestCase):
    """Unit tests for the three v3 helper functions."""

    def test_sma50_slope_10d_rising(self):
        # 11-point series, rising linearly
        series = [None] * 49 + [100 + i for i in range(11)]  # 100, 101, ..., 110
        # last index minus 10 = 100, last = 110 → +10%
        self.assertEqual(mm.compute_sma50_slope_10d(series), 10.0)

    def test_sma50_slope_10d_flat(self):
        series = [100.0] * 20
        self.assertEqual(mm.compute_sma50_slope_10d(series), 0.0)

    def test_sma50_slope_10d_falling(self):
        series = [110 - i for i in range(11)]  # 110 → 100
        # prior=110, today=100 → -9.09%
        result = mm.compute_sma50_slope_10d(series)
        self.assertIsNotNone(result)
        self.assertLess(result, 0)

    def test_sma50_slope_10d_too_short(self):
        self.assertIsNone(mm.compute_sma50_slope_10d([1, 2, 3]))

    def test_pct_from_20d_high_at_high(self):
        highs = [10.0] * 19 + [12.0]
        closes = [10.0] * 19 + [12.0]
        self.assertEqual(mm.pct_from_20d_high(highs, closes), 0.0)

    def test_pct_from_20d_high_below(self):
        highs = [10.0] * 19 + [12.0]
        closes = [10.0] * 19 + [11.4]  # 5% below 12
        self.assertEqual(mm.pct_from_20d_high(highs, closes), -5.0)

    def test_pct_from_20d_high_short_window(self):
        self.assertIsNone(mm.pct_from_20d_high([1, 2], [1, 2]))

    def test_participation_proxy(self):
        # 360 / 3000 = 12%
        td = {"universe_size": 3000, "up_25_quarter": 360}
        self.assertEqual(mm.compute_participation_proxy(td), 12.0)

    def test_participation_proxy_missing(self):
        self.assertIsNone(mm.compute_participation_proxy({"universe_size": 0}))


class TrendFollowTests(unittest.TestCase):
    """All 6 TREND-FOLLOW gates + priority interactions."""

    def _date(self, m=4, d=28):
        return datetime.date(2026, m, d)

    def _metrics(self, ratio_5=1.0, ratio_10=1.0, thrust=False, spy_above=True):
        return {
            "ratio_today":    ratio_5,
            "ratio_5day":     ratio_5,
            "ratio_10day":    ratio_10,
            "thrust":         thrust,
            "spy_above_200d": spy_above,
        }

    def _today(self, **overrides):
        """Default = all 6 TREND-FOLLOW gates pass."""
        base = {
            "up_4_today":              50,
            "down_4_today":            50,
            "spy_atr_mult_50":         2.0,
            "spy_sma50_pct":           2.0,
            "spy_sma200_pct":          5.0,
            "spy_sma50_slope_10d":     1.5,
            "spy_pct_from_20d_high":   -1.0,
            "qqq_atr_mult_50":         2.5,
            "pct_above_50ma":          12.0,
            "vix_close":               18.0,
            "vix_change_pct":          -2.0,
            "universe_size":           3000,
            "up_25_quarter":           360,
        }
        base.update(overrides)
        return base

    def test_all_gates_pass(self):
        state, _, _ = mm.classify_market_state(
            self._metrics(), fg=55, spy_price=500, spy_above_200d=True,
            today_data=self._today(), date=self._date(), prev_state="CAUTION",
        )
        self.assertEqual(state, "TREND-FOLLOW")

    def test_negative_slope_blocks(self):
        state, _, _ = mm.classify_market_state(
            self._metrics(), fg=55, spy_price=500, spy_above_200d=True,
            today_data=self._today(spy_sma50_slope_10d=-0.5),
            date=self._date(), prev_state="CAUTION",
        )
        self.assertNotEqual(state, "TREND-FOLLOW")

    def test_far_below_20d_high_blocks(self):
        state, _, _ = mm.classify_market_state(
            self._metrics(), fg=55, spy_price=500, spy_above_200d=True,
            today_data=self._today(spy_pct_from_20d_high=-5.0),
            date=self._date(), prev_state="CAUTION",
        )
        self.assertNotEqual(state, "TREND-FOLLOW")

    def test_low_participation_blocks(self):
        state, _, _ = mm.classify_market_state(
            self._metrics(), fg=55, spy_price=500, spy_above_200d=True,
            today_data=self._today(pct_above_50ma=4.0),
            date=self._date(), prev_state="CAUTION",
        )
        self.assertNotEqual(state, "TREND-FOLLOW")

    def test_vix_high_and_rising_blocks(self):
        state, _, _ = mm.classify_market_state(
            self._metrics(), fg=55, spy_price=500, spy_above_200d=True,
            today_data=self._today(vix_close=28.0, vix_change_pct=5.0),
            date=self._date(), prev_state="CAUTION",
        )
        self.assertNotEqual(state, "TREND-FOLLOW")

    def test_vix_high_but_falling_allows(self):
        # VIX > 25 OR VIX down — falling VIX is OK
        state, _, _ = mm.classify_market_state(
            self._metrics(), fg=55, spy_price=500, spy_above_200d=True,
            today_data=self._today(vix_close=28.0, vix_change_pct=-3.0),
            date=self._date(), prev_state="CAUTION",
        )
        self.assertEqual(state, "TREND-FOLLOW")

    def test_extended_beats_trend_follow(self):
        # All gates pass BUT SPY is parabolic → EXTENDED wins (priority 3 > 7)
        state, _, _ = mm.classify_market_state(
            self._metrics(), fg=55, spy_price=500, spy_above_200d=True,
            today_data=self._today(spy_atr_mult_50=8.0, spy_sma50_pct=10.0),
            date=self._date(), prev_state="CAUTION",
        )
        self.assertEqual(state, "EXTENDED")

    def test_green_beats_trend_follow(self):
        # GREEN thrust-day conditions also satisfied → GREEN (priority 6 > 7)
        state, _, _ = mm.classify_market_state(
            self._metrics(ratio_5=2.5, ratio_10=1.8),
            fg=55, spy_price=500, spy_above_200d=True,
            today_data=self._today(),
            date=self._date(), prev_state="CAUTION",
        )
        self.assertEqual(state, "GREEN")

    def test_apr30_replay_trend_follow(self):
        # Apr 30 2026 reference: steady grind-up, breadth ~ neutral, but trend
        # intact. v4 prev_state guard rejects COOLING — historical path into
        # Apr 30 was actually via CAUTION (recovery tape).
        state, _, _ = mm.classify_market_state(
            self._metrics(ratio_5=1.1, ratio_10=1.0, thrust=False),
            fg=58, spy_price=500, spy_above_200d=True,
            today_data=self._today(
                spy_sma50_pct=3.0, spy_sma200_pct=6.0,
                spy_sma50_slope_10d=2.1, spy_pct_from_20d_high=-0.5,
                pct_above_50ma=12.0, vix_close=18.0, vix_change_pct=-1.0,
            ),
            date=datetime.date(2026, 4, 30),
            prev_state="CAUTION",
        )
        self.assertEqual(state, "TREND-FOLLOW")


class StateMachineV4Tests(unittest.TestCase):
    """State machine v4 — EXTENDED stickiness, TREND-FOLLOW guard, DANGER widening.

    Spec: docs/specs/state-machine-v4-extended-stickiness.md
    """

    def _date(self, m=5, d=15):
        return datetime.date(2026, m, d)

    def _metrics(self, ratio_5=1.0, ratio_10=1.0, thrust=False, spy_above=True):
        return {
            "ratio_today":    ratio_5,
            "ratio_5day":     ratio_5,
            "ratio_10day":    ratio_10,
            "thrust":         thrust,
            "spy_above_200d": spy_above,
        }

    def _today(self, up=100, down=100, atr_mult=None, sma50_pct=None,
               qqq_mult=None, close=None, sma50_price=None, ema21=None,
               high_20d=None):
        return {
            "up_4_today":      up,
            "down_4_today":    down,
            "spy_atr_mult_50": atr_mult,
            "spy_sma50_pct":   sma50_pct,
            "qqq_atr_mult_50": qqq_mult,
            "spy_close":       close,
            "spy_sma50":       sma50_price,
            "spy_21ema":       ema21,
            "spy_20d_high":    high_20d,
        }

    # ---------------------------------------------------------- Change A
    def test_extended_sticky_through_metric_drop(self):
        # 05-15-style case: prev=EXTENDED, ATR mult cooled to 6.85, %above50
        # 7.12, QQQ 8.24 — all under is_extended() trigger. SPY close above
        # 21 EMA → stickiness keeps EXTENDED. (Calmer breadth used to avoid
        # tripping the v4 widened DANGER 3× single-day path; that interaction
        # is covered by test_danger_catastrophic_distribution separately.)
        state, _, ctx = mm.classify_market_state(
            self._metrics(ratio_5=0.95, ratio_10=1.0),
            fg=60, spy_price=739.17, spy_above_200d=True,
            today_data=self._today(
                up=200, down=180,
                atr_mult=6.85, sma50_pct=7.12, qqq_mult=8.24,
                close=739.17, sma50_price=695.0, ema21=730.0,
                high_20d=748.17,
            ),
            date=self._date(),
            prev_state="EXTENDED",
            extended_since_date="2026-04-30",
            days_below_21ema=0,
        )
        self.assertEqual(state, "EXTENDED")
        self.assertEqual(ctx["days_below_21ema"], 0)
        self.assertEqual(ctx["extended_since_date"], "2026-04-30")

    def test_extended_exit_to_cooling_on_3_closes_below_21ema(self):
        # prev=EXTENDED, today is the 3rd consecutive close below 21 EMA
        # (days_below_21ema=2 carried in, +1 today → 3). SPY > 50 SMA → COOLING.
        state, _, ctx = mm.classify_market_state(
            self._metrics(ratio_5=1.0),
            fg=55, spy_price=720.0, spy_above_200d=True,
            today_data=self._today(
                up=150, down=180,
                atr_mult=3.0, sma50_pct=2.5, qqq_mult=4.0,
                close=720.0, sma50_price=700.0, ema21=725.0,
                high_20d=748.0,
            ),
            date=self._date(),
            prev_state="EXTENDED",
            extended_since_date="2026-04-30",
            days_below_21ema=2,
        )
        self.assertEqual(state, "COOLING")
        self.assertEqual(ctx["days_below_21ema"], 0)
        self.assertIsNone(ctx["extended_since_date"])

    def test_extended_exit_to_red_on_50sma_break(self):
        # prev=EXTENDED, single close below 50 SMA → straight to RED.
        state, _, ctx = mm.classify_market_state(
            self._metrics(ratio_5=0.9),
            fg=50, spy_price=690.0, spy_above_200d=True,
            today_data=self._today(
                up=120, down=200,
                atr_mult=-1.0, sma50_pct=-1.0, qqq_mult=0.0,
                close=690.0, sma50_price=700.0, ema21=720.0,
                high_20d=748.0,
            ),
            date=self._date(),
            prev_state="EXTENDED",
            extended_since_date="2026-04-30",
            days_below_21ema=1,
        )
        self.assertEqual(state, "RED")
        self.assertEqual(ctx["days_below_21ema"], 0)
        self.assertIsNone(ctx["extended_since_date"])

    def test_extended_false_breakdown_reclaim(self):
        # prev=EXTENDED, yesterday closed below 21 EMA (counter=1), today
        # reclaimed above 21 EMA → counter resets to 0, stays EXTENDED.
        state, _, ctx = mm.classify_market_state(
            self._metrics(ratio_5=1.1),
            fg=60, spy_price=735.0, spy_above_200d=True,
            today_data=self._today(
                up=200, down=150,
                atr_mult=5.0, sma50_pct=5.5, qqq_mult=6.0,
                close=735.0, sma50_price=695.0, ema21=730.0,
                high_20d=748.0,
            ),
            date=self._date(),
            prev_state="EXTENDED",
            extended_since_date="2026-04-30",
            days_below_21ema=1,
        )
        self.assertEqual(state, "EXTENDED")
        self.assertEqual(ctx["days_below_21ema"], 0)

    def test_extended_re_entry_from_cooling(self):
        # prev=COOLING, ATR mult ≥ 7, SPY closes at new 20d high → EXTENDED.
        state, _, ctx = mm.classify_market_state(
            self._metrics(ratio_5=1.5),
            fg=65, spy_price=750.0, spy_above_200d=True,
            today_data=self._today(
                up=300, down=120,
                atr_mult=7.5, sma50_pct=8.0, qqq_mult=8.5,
                close=750.0, sma50_price=695.0, ema21=730.0,
                high_20d=750.0,
            ),
            date=self._date(),
            prev_state="COOLING",
        )
        self.assertEqual(state, "EXTENDED")
        self.assertEqual(ctx["extended_since_date"], self._date().isoformat())
        self.assertEqual(ctx["days_below_21ema"], 0)

    # ---------------------------------------------------------- Change B
    def test_trend_follow_blocked_after_extended(self):
        # All 6 TREND-FOLLOW gates pass, but prev=EXTENDED → blocked.
        # Use cooled metrics so EXTENDED stickiness exits cleanly first; here
        # we test the standalone helper to keep it focused.
        td = {
            "up_4_today":              100,
            "down_4_today":            80,
            "spy_atr_mult_50":         2.0,
            "spy_sma50_pct":           2.0,
            "spy_sma200_pct":          5.0,
            "spy_sma50_slope_10d":     1.5,
            "spy_pct_from_20d_high":   -1.0,
            "qqq_atr_mult_50":         2.5,
            "pct_above_50ma":          12.0,
            "vix_close":               18.0,
            "vix_change_pct":          -1.0,
            "universe_size":           3000,
            "up_25_quarter":           360,
        }
        self.assertFalse(mm.is_trend_follow(td, fg=55, prev_state="EXTENDED"))
        self.assertFalse(mm.is_trend_follow(td, fg=55, prev_state="RED"))
        self.assertFalse(mm.is_trend_follow(td, fg=55, prev_state="DANGER"))
        self.assertFalse(mm.is_trend_follow(td, fg=55, prev_state="BLACKOUT"))
        self.assertFalse(mm.is_trend_follow(td, fg=55, prev_state="COOLING"))
        # Allowed prev states still pass:
        self.assertTrue(mm.is_trend_follow(td, fg=55, prev_state="CAUTION"))
        self.assertTrue(mm.is_trend_follow(td, fg=55, prev_state="GREEN"))

    def test_trend_follow_blocked_on_distribution_day(self):
        # All 6 gates pass + prev=GREEN, but dn4=535 vs up4=110 (4.86×) →
        # breadth-sanity rejects.
        td = {
            "up_4_today":              110,
            "down_4_today":            535,
            "spy_atr_mult_50":         2.0,
            "spy_sma50_pct":           2.0,
            "spy_sma200_pct":          5.0,
            "spy_sma50_slope_10d":     1.5,
            "spy_pct_from_20d_high":   -1.0,
            "qqq_atr_mult_50":         2.5,
            "pct_above_50ma":          12.0,
            "vix_close":               18.0,
            "vix_change_pct":          -1.0,
            "universe_size":           3000,
            "up_25_quarter":           360,
        }
        self.assertFalse(mm.is_trend_follow(td, fg=55, prev_state="GREEN"))
        # Sanity: 05-18 case (307 vs 236, 1.30×) still passes:
        td["up_4_today"] = 236
        td["down_4_today"] = 307
        self.assertTrue(mm.is_trend_follow(td, fg=55, prev_state="GREEN"))

    # ---------------------------------------------------------- Change C
    def test_danger_catastrophic_distribution(self):
        # dn4=535, up4=110, 5d=0.89 → DANGER fires via the 3× single-day path.
        state, _, _ = mm.classify_market_state(
            self._metrics(ratio_5=0.89, ratio_10=1.0),
            fg=55, spy_price=739.0, spy_above_200d=True,
            today_data=self._today(up=110, down=535),
            date=self._date(),
            prev_state="EXTENDED",
        )
        self.assertEqual(state, "DANGER")

    def test_danger_sustained_weakness(self):
        # dn4=520, up4=300, 5d=0.45 → DANGER fires via original 5d path
        # (3× single-day check would NOT fire: 520 < 3*300).
        state, _, _ = mm.classify_market_state(
            self._metrics(ratio_5=0.45),
            fg=30, spy_price=500, spy_above_200d=True,
            today_data=self._today(up=300, down=520),
            date=self._date(),
            prev_state="RED",
        )
        self.assertEqual(state, "DANGER")


if __name__ == "__main__":
    unittest.main()
