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
    # --- ATR tiered trail (continuous) -----------------------------------

    def test_atr_trail_2x_when_peak_below_10(self):
        # entry 100, atr% 4, peak 108 (+8%). Tier: 2.0×. Trail = 108 - 8 = 100.
        # Loss-cap: peak ≥5 → max(97, 98) = 98. Final stop = max(90, 98, 100) = 100.
        e = _entry()
        rules.apply_position_rules("FOO", e, 108.0, 108.0, 4.0)
        self.assertEqual(e["stop_price"], 100.0)
        self.assertFalse(e["breakeven_activated"])

    def test_atr_trail_15x_when_peak_10_to_20(self):
        # peak 110 (+10%). Tier: 1.5×. Trail = 110 - 1.5×4 = 104.
        e = _entry()
        rules.apply_position_rules("FOO", e, 110.0, 110.0, 4.0)
        self.assertEqual(e["stop_price"], 104.0)

    def test_atr_trail_1x_when_peak_20_plus(self):
        # peak 122 (+22%). Tier: 1.0×. Trail = 122 - 4 = 118.
        e = _entry()
        rules.apply_position_rules("FOO", e, 122.0, 122.0, 4.0)
        self.assertEqual(e["stop_price"], 118.0)
        self.assertTrue(e["breakeven_activated"])

    def test_trail_uses_highest_price_seen_not_current_price(self):
        # VIK regression: peak captured via day_high but current_price is below.
        # Hourly snapshot caught a price below the intraday peak; trail must
        # still ratchet from the recorded peak, not the snapshot.
        e = _entry(highest_price_seen=130.0, peak_gain_pct=30.0,
                   breakeven_activated=True, stop_price=100.5)
        rules.apply_position_rules("FOO", e, 120.0, 125.0, 4.0)
        # peak stays at 130 (current 120 + day_high 125 < 130). 1.0× trail
        # off prev_high: 130 - 4 = 126.
        self.assertGreaterEqual(e["stop_price"], 126.0)

    def test_trail_only_ratchets_up(self):
        # Existing stop above ATR trail level — must not lower.
        e = _entry(stop_price=115.0)
        rules.apply_position_rules("FOO", e, 110.0, 110.0, 4.0)
        self.assertEqual(e["stop_price"], 115.0)

    # --- Loss-cap floor (hybrid α/β) -------------------------------------

    def test_loss_cap_does_not_apply_below_5pct_peak(self):
        # peak 104 (+4%). Loss-cap doesn't engage. ATR 2.0× trail does:
        # 104 - 8 = 96. Loss-cap floor would have been entry × 0.97 = 97 (higher),
        # so we verify it stayed at the ATR-trail value, not the floor.
        e = _entry()
        rules.apply_position_rules("FOO", e, 104.0, 104.0, 4.0)
        self.assertEqual(e["stop_price"], 96.0)
        # And confirm: had peak been ≥5%, floor (97) would have been applied.
        self.assertLess(e["stop_price"], 97.0)

    def test_loss_cap_low_vol_uses_beta_tighter(self):
        # 3% ATR, peak +5%. β = entry - 0.5×3 = 98.5. α = 97. max = 98.5.
        # ATR trail tier 2.0×: 105 - 6 = 99. Stop = max(90, 98.5, 99) = 99.
        e = _entry(atr_pct=3.0)
        rules.apply_position_rules("FOO", e, 105.0, 105.0, 3.0)
        self.assertEqual(e["stop_price"], 99.0)

    def test_loss_cap_high_vol_capped_at_alpha_3pct(self):
        # 10% ATR, peak +5%. β = entry - 0.5×10 = 95 (-5%). α = 97 (-3%).
        # max = 97. ATR trail tier 2.0×: 105 - 20 = 85 (loose).
        # Loss-cap saves us: stop = max(90, 97, 85) = 97.
        e = _entry(atr_pct=10.0, stop_price=80.0)
        rules.apply_position_rules("FOO", e, 105.0, 105.0, 10.0)
        self.assertEqual(e["stop_price"], 97.0)

    # --- Breakeven flag (informational only) -----------------------------

    def test_breakeven_flag_set_at_peak_20(self):
        e = _entry(highest_price_seen=125.0, peak_gain_pct=25.0)
        events, _ = rules.apply_position_rules("FOO", e, 115.0, 115.0, 4.0)
        self.assertTrue(e["breakeven_activated"])
        self.assertTrue(any(ev["kind"] == "breakeven" for ev in events))

    def test_breakeven_flag_not_set_under_20(self):
        e = _entry(highest_price_seen=119.0, peak_gain_pct=19.0)
        events, _ = rules.apply_position_rules("FOO", e, 119.0, 119.0, 4.0)
        self.assertFalse(e["breakeven_activated"])
        self.assertFalse(any(ev["kind"] == "breakeven" for ev in events))

    def test_breakeven_event_fires_only_once(self):
        e = _entry(highest_price_seen=125.0, peak_gain_pct=25.0,
                   breakeven_activated=True, stop_price=120.0)
        events, _ = rules.apply_position_rules("FOO", e, 125.0, 125.0, 4.0)
        self.assertFalse(any(ev["kind"] == "breakeven" for ev in events))

    def test_breakeven_keeps_locked_one_way(self):
        # Even on a higher tick after BE, flag stays set; trail keeps ratcheting up.
        e = _entry(highest_price_seen=125.0, peak_gain_pct=25.0,
                   breakeven_activated=True, stop_price=100.5)
        rules.apply_position_rules("FOO", e, 130.0, 130.0, 4.0)
        self.assertTrue(e["breakeven_activated"])
        # peak now 130. 1.0× trail = 130 - 4 = 126.
        self.assertGreaterEqual(e["stop_price"], 126.0)

    # --- +30% floor ------------------------------------------------------

    def test_30pct_floor_wins_for_high_vol(self):
        # ATR 15% (very high vol). peak 130 (+30%). 1.0× ATR trail = 130 - 15 = 115.
        # 10%-from-peak floor = 117. Floor wins.
        e = _entry(atr_pct=15.0, stop_price=100.0,
                   highest_price_seen=130.0, peak_gain_pct=30.0,
                   breakeven_activated=True, target1_hit=True)
        events, _ = rules.apply_position_rules("FOO", e, 130.0, 130.0, 15.0)
        self.assertEqual(e["stop_price"], 117.0)
        self.assertTrue(any(ev["kind"] == "trailing_stop" for ev in events))

    def test_30pct_floor_redundant_for_low_vol(self):
        # ATR 4%. peak 130 (+30%). 1.0× trail = 126 > 117 floor → ATR wins.
        # No trailing_stop event since floor doesn't raise stop.
        e = _entry(stop_price=100.0,
                   highest_price_seen=130.0, peak_gain_pct=30.0,
                   breakeven_activated=True, target1_hit=True)
        events, _ = rules.apply_position_rules("FOO", e, 130.0, 130.0, 4.0)
        self.assertEqual(e["stop_price"], 126.0)
        self.assertFalse(any(ev["kind"] == "trailing_stop" for ev in events))

    # --- Targets ---------------------------------------------------------

    def test_target1_alert_once(self):
        e = _entry()
        events, _ = rules.apply_position_rules("FOO", e, 120.0, 120.0, 4.0)
        self.assertTrue(any("TARGET 1" in ev["message"] for ev in events))
        self.assertTrue(e["target1_hit"])
        events2, _ = rules.apply_position_rules("FOO", e, 125.0, 125.0, 4.0)
        self.assertFalse(any("TARGET 1" in ev["message"] for ev in events2))

    # --- Fade alert ------------------------------------------------------

    def test_fade_alert_one_atr_below_peak(self):
        e = _entry(highest_price_seen=125.0, peak_gain_pct=25.0,
                   breakeven_activated=True, target1_hit=True, stop_price=100.5)
        events, _ = rules.apply_position_rules("FOO", e, 120.0, 125.0, 4.0)
        self.assertTrue(any("fading" in ev["message"] for ev in events))

    def test_fade_does_not_fire_within_one_atr(self):
        e = _entry(highest_price_seen=125.0, peak_gain_pct=25.0,
                   breakeven_activated=True, target1_hit=True, stop_price=100.5)
        events, _ = rules.apply_position_rules("FOO", e, 123.0, 125.0, 4.0)
        self.assertFalse(any("fading" in ev["message"] for ev in events))

    # --- Label prefix ----------------------------------------------------

    def test_label_prefix(self):
        e = _entry(highest_price_seen=125.0, peak_gain_pct=25.0)
        events, _ = rules.apply_position_rules("FOO", e, 115.0, 115.0, 4.0,
                                                label_prefix="PAPER")
        self.assertTrue(any("[PAPER]" in ev["message"] for ev in events))

    # --- VIK regression --------------------------------------------------

    def test_vik_regression_intraday_peak_locks_correctly(self):
        # VIK Apr 2026: entry 77.14, ATR% 4.73, intraday peak 86.75 (+12.46%).
        # Hourly snapshot caught $83.65 only. Old rule: stop $76.35.
        # New rule: trail off prev_high (86.75) at 1.5× tier (peak +12.46%).
        # trail = 86.75 - 1.5 × 0.0473 × 77.14 = 81.27.
        e = _entry(entry_price=77.14, atr_pct=4.73, stop_price=69.84,
                   highest_price_seen=86.75, peak_gain_pct=12.46,
                   target1=92.57, target2=108.0)
        rules.apply_position_rules("VIK", e, 83.65, 83.65, 4.73)
        self.assertAlmostEqual(e["stop_price"], 81.27, places=1)


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
        closes = [100.0] * 30 + [80.0]
        out = rules.check_ma_trail_alert(closes, "GREEN", atr_pct=6.0)
        self.assertIsNotNone(out)
        self.assertEqual(out["ma_type"], "8EMA")
        self.assertEqual(out["tier"], "mid_vol")

    def test_low_vol_21ema_needs_two_consecutive_in_green(self):
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
