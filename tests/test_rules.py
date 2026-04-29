"""
Tests for agents/trading/rules.py — the shared per-position rules engine.
"""

import unittest

from agents.trading import rules


def _entry(**overrides):
    base = {
        "stop_price": 90.0,
        "entry_price": 100.0,
        "atr_pct": 4.0,
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


class ApplyPositionRulesTests(unittest.TestCase):
    def test_atr_trail_silent_pre_breakeven(self):
        e = _entry()
        alerts, _ = rules.apply_position_rules("FOO", e, 110.0, 110.0, 4.0)
        # 110 - 2*4 = 102
        self.assertEqual(e["stop_price"], 102.0)
        self.assertFalse(any("breakeven" in a.lower() for a in alerts))

    def test_breakeven_triggered_by_peak_not_live_gain(self):
        # Price has already pulled back below +20%, but peak hit it.
        e = _entry(highest_price_seen=125.0, peak_gain_pct=25.0)
        alerts, _ = rules.apply_position_rules("FOO", e, 115.0, 115.0, 4.0)
        self.assertTrue(e["breakeven_activated"])
        self.assertGreaterEqual(e["stop_price"], 100.5)
        self.assertTrue(any("breakeven" in a.lower() for a in alerts))

    def test_breakeven_does_not_fire_when_peak_under_20(self):
        e = _entry(highest_price_seen=119.0, peak_gain_pct=19.0)
        alerts, _ = rules.apply_position_rules("FOO", e, 119.0, 119.0, 4.0)
        self.assertFalse(e["breakeven_activated"])
        self.assertFalse(any("breakeven" in a.lower() for a in alerts))

    def test_breakeven_locks_one_way(self):
        e = _entry(highest_price_seen=125.0, peak_gain_pct=25.0,
                   breakeven_activated=True, stop_price=100.5)
        # Even on a higher high tick, ATR trail should NOT lower stop nor un-lock
        rules.apply_position_rules("FOO", e, 130.0, 130.0, 4.0)
        self.assertTrue(e["breakeven_activated"])
        self.assertGreaterEqual(e["stop_price"], 100.5)

    def test_target1_alert_once(self):
        e = _entry()
        alerts, _ = rules.apply_position_rules("FOO", e, 120.0, 120.0, 4.0)
        self.assertTrue(any("TARGET 1" in a for a in alerts))
        self.assertTrue(e["target1_hit"])
        alerts2, _ = rules.apply_position_rules("FOO", e, 125.0, 125.0, 4.0)
        self.assertFalse(any("TARGET 1" in a for a in alerts2))

    def test_30pct_trail_raises_stop(self):
        e = _entry(highest_price_seen=130.0, peak_gain_pct=30.0,
                   breakeven_activated=True, target1_hit=True, stop_price=100.5)
        alerts, _ = rules.apply_position_rules("FOO", e, 130.0, 130.0, 4.0)
        self.assertAlmostEqual(e["stop_price"], 117.0, places=2)
        self.assertTrue(any("trailing stop raised" in a for a in alerts))

    def test_fade_alert_one_atr_below_peak(self):
        e = _entry(highest_price_seen=125.0, peak_gain_pct=25.0,
                   breakeven_activated=True, target1_hit=True, stop_price=100.5)
        alerts, _ = rules.apply_position_rules("FOO", e, 120.0, 125.0, 4.0)
        self.assertTrue(any("fading" in a for a in alerts))

    def test_fade_does_not_fire_within_one_atr(self):
        e = _entry(highest_price_seen=125.0, peak_gain_pct=25.0,
                   breakeven_activated=True, target1_hit=True, stop_price=100.5)
        alerts, _ = rules.apply_position_rules("FOO", e, 123.0, 125.0, 4.0)
        self.assertFalse(any("fading" in a for a in alerts))

    def test_label_prefix(self):
        e = _entry(highest_price_seen=125.0, peak_gain_pct=25.0)
        alerts, _ = rules.apply_position_rules("FOO", e, 115.0, 115.0, 4.0,
                                                label_prefix="PAPER")
        self.assertTrue(any("[PAPER]" in a for a in alerts))


class MaTrailAlertTests(unittest.TestCase):
    def test_red_regime_disables(self):
        out = rules.check_ma_trail_alert([100, 99, 98], "RED", atr_pct=4.0,
                                         highest_price_seen=110.0)
        self.assertIsNone(out)

    def test_high_vol_pct_trail_fires(self):
        out = rules.check_ma_trail_alert([89.0], "GREEN", atr_pct=10.0,
                                         highest_price_seen=100.0)
        self.assertIsNotNone(out)
        self.assertEqual(out["tier"], "high_vol")
        self.assertEqual(out["last_ema"], 90.0)  # 10% trail from 100

    def test_high_vol_pct_trail_does_not_fire_above_floor(self):
        out = rules.check_ma_trail_alert([91.0], "GREEN", atr_pct=10.0,
                                         highest_price_seen=100.0)
        self.assertIsNone(out)

    def test_mid_vol_8ema_close_below(self):
        # Closes ascending then sharp drop — ensures latest close < 8EMA
        closes = [100.0] * 30 + [80.0]
        out = rules.check_ma_trail_alert(closes, "GREEN", atr_pct=6.0)
        self.assertIsNotNone(out)
        self.assertEqual(out["ma_type"], "8EMA")
        self.assertEqual(out["tier"], "mid_vol")

    def test_low_vol_21ema_needs_two_consecutive_in_green(self):
        # Single close below not enough — GREEN/low-vol needs 2 consecutive
        closes = [100.0] * 30 + [80.0]
        out = rules.check_ma_trail_alert(closes, "GREEN", atr_pct=3.0)
        self.assertIsNone(out)
        closes = [100.0] * 30 + [80.0, 75.0]
        out = rules.check_ma_trail_alert(closes, "GREEN", atr_pct=3.0)
        self.assertIsNotNone(out)
        self.assertEqual(out["ma_type"], "21EMA")
        self.assertEqual(out["consecutive"], 2)

    def test_low_vol_cooling_uses_8ema_one_close(self):
        closes = [100.0] * 30 + [80.0]
        out = rules.check_ma_trail_alert(closes, "COOLING", atr_pct=3.0)
        self.assertIsNotNone(out)
        self.assertEqual(out["ma_type"], "8EMA")


class SizingModeTests(unittest.TestCase):
    def test_three_losses_suspended(self):
        ts = {"consecutive_losses": 3, "consecutive_wins": 0,
              "current_sizing_mode": "normal"}
        alerts = rules.update_sizing_mode(ts, "GREEN")
        self.assertEqual(ts["current_sizing_mode"], "suspended")
        self.assertTrue(any("SUSPENDED" in a for a in alerts))

    def test_two_losses_reduced(self):
        ts = {"consecutive_losses": 2, "consecutive_wins": 0,
              "current_sizing_mode": "normal"}
        rules.update_sizing_mode(ts, "GREEN")
        self.assertEqual(ts["current_sizing_mode"], "reduced")

    def test_two_wins_green_aggressive(self):
        ts = {"consecutive_losses": 0, "consecutive_wins": 2,
              "current_sizing_mode": "normal"}
        rules.update_sizing_mode(ts, "GREEN")
        self.assertEqual(ts["current_sizing_mode"], "aggressive")

    def test_two_wins_red_not_aggressive(self):
        ts = {"consecutive_losses": 0, "consecutive_wins": 2,
              "current_sizing_mode": "normal"}
        rules.update_sizing_mode(ts, "RED")
        self.assertEqual(ts["current_sizing_mode"], "normal")


class RecordTradeResultTests(unittest.TestCase):
    def test_win_increments_streak(self):
        ts = {}
        rules.record_trade_result(ts, "AAA", 5.0, "2026-04-29")
        self.assertEqual(ts["consecutive_wins"], 1)
        self.assertEqual(ts["consecutive_losses"], 0)
        self.assertEqual(ts["total_wins"], 1)
        self.assertEqual(ts["recent_trades"][-1]["result"], "win")

    def test_loss_resets_wins(self):
        ts = {"consecutive_wins": 3}
        rules.record_trade_result(ts, "AAA", -5.0, "2026-04-29")
        self.assertEqual(ts["consecutive_wins"], 0)
        self.assertEqual(ts["consecutive_losses"], 1)

    def test_neutral_does_not_bump_streak(self):
        ts = {"consecutive_wins": 2, "consecutive_losses": 0}
        rules.record_trade_result(ts, "AAA", 0.5, "2026-04-29")
        self.assertEqual(ts["consecutive_wins"], 2)
        self.assertEqual(ts["consecutive_losses"], 0)
        self.assertEqual(ts["recent_trades"][-1]["result"], "neutral")


if __name__ == "__main__":
    unittest.main()
