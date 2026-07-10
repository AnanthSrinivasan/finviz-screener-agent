"""
21 EMA trail mode (paper lab) — spec docs/specs/ema21-trail-mode.md.

Reference cases:
  - VIK 2026-07-02: ATR 4.23%, peak +24% → mode activates; the old ATR trail
    fired intraday at $100.07 but VIK closed ABOVE its 21 EMA → ema21 holds.
  - VIK 2026-07-10: intraday low tagged the EMA (98.44 vs 98.45) with no close
    below → holds, breach counter stays 0 (a tag is not a close).
  - OSCR shakeout pattern: close below the EMA that holds above prior lows is
    a shakeout → HOLD. Only a close that also takes out the 10-session swing
    low is trend damage → exit.
  - Camping fallback: 3 consecutive closes below the EMA on equal lows → exit.
  - Floors always win: gap below peak×0.90 exits regardless of mode.
"""

import json
import os
import tempfile
import unittest

from agents.trading import rules
from agents.trading import alpaca_monitor as am


def flat_fixture(n=30, close=100.0, low=98.0):
    """Flat series — EMA21 ≈ close, prior-10-session swing low = `low`."""
    return [close] * n, [low] * n


class TestActivation(unittest.TestCase):

    def test_vik_class_activates(self):
        # VIK 7/2: ATR 4.23%, peak +24.0%
        self.assertTrue(rules.ema21_trail_activates(4.23, 24.0))
        v = rules.ema21_trail_verdict([], [], 0.0, 4.23, 24.0, breach_count=None)
        self.assertEqual(v["action"], "activate")
        self.assertEqual(v["new_breach_count"], 0)

    def test_atr_6_never_activates(self):
        # ATR 6% name at +25% peak → tier trail as today
        self.assertFalse(rules.ema21_trail_activates(6.0, 25.0))
        v = rules.ema21_trail_verdict([], [], 0.0, 6.0, 25.0, breach_count=None)
        self.assertEqual(v["action"], "hold")

    def test_peak_below_20_never_activates(self):
        self.assertFalse(rules.ema21_trail_activates(4.0, 15.0))

    def test_atr_zero_never_activates(self):
        self.assertFalse(rules.ema21_trail_activates(0.0, 30.0))

    def test_boundaries_inclusive(self):
        self.assertTrue(rules.ema21_trail_activates(5.0, 20.0))


class TestTrailVerdict(unittest.TestCase):

    def test_close_above_ema_holds_vik_0702(self):
        # VIK 7/2 class: close sits above the 21 EMA → hold, counter resets.
        closes, lows = flat_fixture()
        closes[-1] = 102.0  # closes above the ~100 EMA
        v = rules.ema21_trail_verdict(closes, lows, 102.0, 4.23, 24.0,
                                      breach_count=1)
        self.assertEqual(v["action"], "hold")
        self.assertEqual(v["new_breach_count"], 0)

    def test_intraday_ema_tag_is_not_a_close(self):
        # VIK 7/10: low tags the EMA intraday but the close never breaks it.
        closes, lows = flat_fixture()
        lows[-1] = 95.0     # deep intraday tag — today's low is excluded anyway
        v = rules.ema21_trail_verdict(closes, lows, 100.0, 4.23, 24.0,
                                      breach_count=0)
        self.assertEqual(v["action"], "hold")
        self.assertEqual(v["new_breach_count"], 0)

    def test_close_below_ema_above_swing_low_is_shakeout_hold(self):
        # OSCR-etched pattern: EMA break that holds prior lows → HOLD.
        closes, lows = flat_fixture()
        closes[-1] = 99.0   # below EMA (~99.9), above swing low 98
        v = rules.ema21_trail_verdict(closes, lows, 99.0, 4.23, 24.0,
                                      breach_count=0)
        self.assertEqual(v["action"], "hold")
        self.assertEqual(v["new_breach_count"], 1)
        self.assertIn("shakeout", v["reason"])
        self.assertEqual(v["swing_low"], 98.0)

    def test_close_below_ema_and_lower_low_exits(self):
        closes, lows = flat_fixture()
        closes[-1] = 97.0   # below EMA AND below swing low 98
        v = rules.ema21_trail_verdict(closes, lows, 97.0, 4.23, 24.0,
                                      breach_count=0)
        self.assertEqual(v["action"], "exit")
        self.assertIn("lower low", v["reason"])

    def test_camping_three_closes_exits_on_third(self):
        # Equal lows throughout — no lower low, exit purely on the counter.
        count = 0
        for day, expected in ((1, "hold"), (2, "hold"), (3, "exit")):
            closes, lows = flat_fixture()
            for i in range(day):
                closes[-(day - i)] = 99.0  # last `day` closes sit below EMA
            v = rules.ema21_trail_verdict(closes, lows, 99.0, 4.23, 24.0,
                                          breach_count=count)
            count = v["new_breach_count"]
            self.assertEqual(v["action"], expected, "day " + str(day))
            self.assertEqual(count, day)
        self.assertIn("camping", v["reason"])

    def test_reclaim_resets_counter(self):
        # 2 closes below, then a close back above → hold, counter back to 0.
        closes, lows = flat_fixture()
        closes[-1] = 101.0
        v = rules.ema21_trail_verdict(closes, lows, 101.0, 4.23, 24.0,
                                      breach_count=2)
        self.assertEqual(v["action"], "hold")
        self.assertEqual(v["new_breach_count"], 0)

    def test_insufficient_closes_holds_and_keeps_counter(self):
        v = rules.ema21_trail_verdict([100.0] * 10, [98.0] * 10, 99.0,
                                      4.23, 24.0, breach_count=2)
        self.assertEqual(v["action"], "hold")
        self.assertEqual(v["new_breach_count"], 2)


class TestFloors(unittest.TestCase):

    def test_gap_below_peak_090_floor_exits_regardless_of_mode(self):
        # +35% peak → disaster floor = 135 × 0.90 = 121.5; a gap to 120 is
        # under the floor and the intraday floor check exits — mode irrelevant.
        floor = rules.ema21_mode_floor(entry_price=100.0, atr_pct=4.0,
                                       peak_gain_pct=35.0,
                                       highest_price_seen=135.0)
        self.assertEqual(floor, 121.5)
        self.assertLessEqual(120.0, floor)

    def test_breakeven_floor_below_30_peak(self):
        # Peak +24% (< 30): floor = max(BE 100.5, loss-cap 98) = 100.5.
        floor = rules.ema21_mode_floor(entry_price=100.0, atr_pct=4.0,
                                       peak_gain_pct=24.0,
                                       highest_price_seen=124.0)
        self.assertEqual(floor, 100.5)

    def test_no_entry_no_floor(self):
        self.assertEqual(rules.ema21_mode_floor(0.0, 4.0, 24.0, 124.0), 0.0)


class TestABLog(unittest.TestCase):

    def test_append_creates_and_appends(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "trail_mode_ab.json")
            orig = am.TRAIL_AB_FILE
            am.TRAIL_AB_FILE = path
            try:
                am.append_trail_ab_record({"date": "2026-07-10", "ticker": "VIK",
                                           "close": 100.1, "ema21": 98.45,
                                           "atr_stop": 101.2,
                                           "atr_would_exit": True,
                                           "ema21_exited": False,
                                           "breach_count": 0})
                am.append_trail_ab_record({"date": "2026-07-11", "ticker": "VIK",
                                           "close": 101.0, "ema21": 98.6,
                                           "atr_stop": 101.2,
                                           "atr_would_exit": True,
                                           "ema21_exited": False,
                                           "breach_count": 0})
                with open(path) as f:
                    records = json.load(f)
                self.assertEqual(len(records), 2)
                self.assertTrue(records[0]["atr_would_exit"])
                self.assertFalse(records[0]["ema21_exited"])
            finally:
                am.TRAIL_AB_FILE = orig


if __name__ == "__main__":
    unittest.main()
