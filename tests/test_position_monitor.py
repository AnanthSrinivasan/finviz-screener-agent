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
            "stop_price":         95.0,
            "target1":      120.0,
            "target2":      140.0,
            "highest_price_seen": 100.0,
            "current_gain_pct":   0,
            "status":       "open",
        }
        base.update(overrides)
        return base

    def test_stop_hit_fires_alert(self):
        pos = self._pos(stop_price=95.0)
        alerts, modified = pm.apply_minervini_rules(pos, current_price=94.50, atr=2.0)
        self.assertTrue(any("STOP HIT" in a for a in alerts))
        # Per spec B.1: status stays "active" (alert only, human decides).
        self.assertNotEqual(pos["status"], "stop_hit")

    def test_no_stop_alert_above_stop(self):
        pos = self._pos(stop=95.0)
        alerts, _ = pm.apply_minervini_rules(pos, current_price=96.0, atr=2.0)
        self.assertFalse(any("STOP HIT" in a for a in alerts))
        self.assertNotEqual(pos["status"], "stop_hit")

    def test_breakeven_activates_at_20_pct_gain(self):
        pos = self._pos(entry_price=100.0, stop=95.0,
                        target1=200.0, target2=300.0)  # lift targets out of range
        alerts, modified = pm.apply_minervini_rules(pos, current_price=120.0, atr=2.0)
        self.assertTrue(pos.get("breakeven_activated"))
        # Stop must be at or above breakeven (100.50). ATR trail may already have
        # raised it higher (price − 2×ATR = 116) — that's fine; breakeven is a floor.
        self.assertGreaterEqual(pos["stop_price"], 100.5)
        self.assertTrue(any("stop moved to breakeven" in a for a in alerts))
        self.assertTrue(modified)

    def test_breakeven_floor_applies_when_no_atr(self):
        # atr=0 skips ATR trail entirely → breakeven floor of 100.5 applies
        pos = self._pos(entry_price=100.0, stop=95.0,
                        target1=200.0, target2=300.0)
        alerts, _ = pm.apply_minervini_rules(pos, current_price=120.0, atr=0)
        self.assertTrue(pos.get("breakeven_activated"))
        self.assertAlmostEqual(pos["stop_price"], 100.5, places=2)
        self.assertTrue(any("stop moved to breakeven" in a for a in alerts))

    def test_trailing_stop_raises_at_30_pct_gain(self):
        pos = self._pos(entry_price=100.0, stop=100.5,
                        highest_price_seen=130.0,
                        target1=200.0, target2=300.0,
                        breakeven_activated=True)
        alerts, _ = pm.apply_minervini_rules(pos, current_price=130.0, atr=2.0)
        # 10% trail from 130 = 117
        self.assertAlmostEqual(pos["stop_price"], 117.0, places=2)
        self.assertTrue(any("trailing stop raised" in a for a in alerts))

    def test_atr_trail_silent_before_breakeven(self):
        pos = self._pos(entry_price=100.0, stop=95.0,
                        target1=200.0, target2=300.0)
        alerts, modified = pm.apply_minervini_rules(pos, current_price=110.0, atr=2.0)
        # price - 2*ATR = 110 - 4 = 106 → stop raised silently (no alert text for ATR trail)
        self.assertAlmostEqual(pos["stop_price"], 106.0, places=2)
        self.assertTrue(modified)
        self.assertFalse(any("ATR trail" in a for a in alerts))

    def test_fade_fires_when_price_drops_one_atr_below_high(self):
        # Peak was +25% (high=125), now price=122 (dropped 3 from high) with ATR=2 → fires
        pos = self._pos(entry_price=100.0, stop=100.5,
                        target1=200.0, target2=300.0,
                        highest_price_seen=125.0,
                        peak_gain_pct=25.0,
                        breakeven_activated=True)
        alerts, _ = pm.apply_minervini_rules(pos, current_price=122.0, atr=2.0)
        self.assertTrue(any("fading" in a for a in alerts))

    def test_fade_does_not_fire_within_one_atr_of_high(self):
        # Peak +25% (high=125), price=124 is only 1 below high, within 1×ATR — no fade
        pos = self._pos(entry_price=100.0, stop=100.5,
                        target1=200.0, target2=300.0,
                        highest_price_seen=125.0,
                        peak_gain_pct=25.0,
                        breakeven_activated=True)
        alerts, _ = pm.apply_minervini_rules(pos, current_price=124.0, atr=2.0)
        self.assertFalse(any("fading" in a for a in alerts))

    def test_fade_requires_peak_above_20_pct(self):
        # Peak only +15% → below threshold → no fade even if price dropped 2×ATR
        pos = self._pos(entry_price=100.0, stop=95.0,
                        target1=200.0, target2=300.0,
                        highest_price_seen=115.0,
                        peak_gain_pct=15.0)
        alerts, _ = pm.apply_minervini_rules(pos, current_price=110.0, atr=2.0)
        self.assertFalse(any("fading" in a for a in alerts))

    def test_fade_dedups_until_another_5pp_drop(self):
        # First call fires, second within 5pp of first should NOT re-fire
        pos = self._pos(entry_price=100.0, stop=100.5,
                        target1=200.0, target2=300.0,
                        highest_price_seen=150.0,
                        peak_gain_pct=50.0,
                        breakeven_activated=True)
        alerts1, _ = pm.apply_minervini_rules(pos, current_price=140.0, atr=2.0)
        self.assertTrue(any("fading" in a for a in alerts1))

        # Next tick: gain at 138/100 = +38% (2pp lower) — within 5pp dedup
        alerts2, _ = pm.apply_minervini_rules(pos, current_price=138.0, atr=2.0)
        self.assertFalse(any("fading" in a for a in alerts2))

        # Drop another 5pp → fires again
        alerts3, _ = pm.apply_minervini_rules(pos, current_price=132.0, atr=2.0)
        self.assertTrue(any("fading" in a for a in alerts3))

    def test_day_high_captures_intraday_peak(self):
        # current_price=150 but day_high=173 → highest_price_seen uses 173
        pos = self._pos(entry_price=100.0, highest_price_seen=100.0)
        pm.apply_minervini_rules(pos, current_price=150.0, atr=5.0, day_high=173.0)
        self.assertEqual(pos["highest_price_seen"], 173.0)
        self.assertAlmostEqual(pos["peak_gain_pct"], 73.0, places=1)

    def test_day_high_ignored_when_below_prior_high(self):
        pos = self._pos(entry_price=100.0, highest_price_seen=180.0)
        pm.apply_minervini_rules(pos, current_price=150.0, atr=5.0, day_high=160.0)
        self.assertEqual(pos["highest_price_seen"], 180.0)

    def test_peak_gain_pct_never_decreases(self):
        pos = self._pos(entry_price=100.0, highest_price_seen=150.0, peak_gain_pct=50.0)
        # Price drops — peak_gain_pct must stay at 50
        pm.apply_minervini_rules(pos, current_price=120.0, atr=2.0)
        self.assertEqual(pos["peak_gain_pct"], 50.0)

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


class PeakGainBreakevenTests(unittest.TestCase):
    """Breakeven and +30% trail must trigger off peak_gain_pct, not current gain.
    A brief intraday +20% touch should lock breakeven forever even if current snap
    catches price already below.
    """

    def _pos(self, **overrides):
        base = {
            "ticker": "PL",
            "entry_price": 100.0,
            "stop_price": 95.0,
            "target1": 200.0,
            "target2": 300.0,
            "highest_price_seen": 120.5,  # peak +20.5%
            "peak_gain_pct": 20.5,
            "current_gain_pct": 0,
            "status": "open",
        }
        base.update(overrides)
        return base

    def test_breakeven_locks_from_peak_even_when_current_below(self):
        # Current price is back at entry — current gain ~0, but peak was +20.5%.
        # Old code missed this; new code activates breakeven.
        pos = self._pos()
        alerts, modified = pm.apply_minervini_rules(pos, current_price=100.0, atr=2.0)
        self.assertTrue(pos.get("breakeven_activated"))
        self.assertGreaterEqual(pos["stop_price"], 100.5)
        self.assertTrue(any("breakeven" in a.lower() for a in alerts))
        self.assertTrue(modified)

    def test_breakeven_does_not_activate_below_peak_20(self):
        pos = self._pos(highest_price_seen=119.0, peak_gain_pct=19.0)
        pm.apply_minervini_rules(pos, current_price=100.0, atr=2.0)
        self.assertFalse(pos.get("breakeven_activated", False))

    def test_trailing_stop_locks_from_peak_30(self):
        # Peak +35% (high=135), current back at 110. 10% trail = 121.5.
        pos = self._pos(highest_price_seen=135.0, peak_gain_pct=35.0,
                        breakeven_activated=True, stop=100.5)
        pm.apply_minervini_rules(pos, current_price=110.0, atr=2.0)
        self.assertAlmostEqual(pos["stop_price"], 121.5, places=1)



class AutoCloseTests(unittest.TestCase):
    """Auto-close: real fill > live quote > peak fallback; neutral 1% band leaves
    streak/sizing untouched."""

    def _state(self, **over):
        base = {
            "open_positions": [],
            "closed_positions": [],
        }
        base.update(over)
        return base

    def _trading(self, **over):
        base = {
            "consecutive_wins": 0,
            "consecutive_losses": 0,
            "total_wins": 0,
            "total_losses": 0,
            "recent_trades": [],
            "current_sizing_mode": "normal",
            "last_updated": "",
        }
        base.update(over)
        return base

    def test_neutral_close_does_not_bump_streak(self):
        # Position at entry $100 closed at $100.5 (+0.5% < 1% band)
        pos = {"ticker": "PL", "shares": 100, "entry_price": 100.0,
               "highest_price_seen": 120.0, "stop_price": 95.0, "status": "active"}
        positions_data = self._state(open_positions=[pos])
        ts = self._trading(consecutive_wins=2)
        sell_fills = {"PL": {"price": 100.5, "date": "2026-04-24", "units": 100}}
        alerts = pm.sync_snaptrade_with_rules(
            snaptrade_positions=[],
            positions_data=positions_data,
            trading_state=ts,
            market_state="GREEN",
            sell_fills=sell_fills,
        )
        # Streak preserved (no win added, no loss added)
        self.assertEqual(ts["consecutive_wins"], 2)
        self.assertEqual(ts["consecutive_losses"], 0)
        self.assertEqual(ts["total_wins"], 0)
        self.assertEqual(ts["total_losses"], 0)
        self.assertTrue(any("BREAKEVEN" in a for a in alerts))
        # Recorded as neutral
        self.assertEqual(ts["recent_trades"][-1]["result"], "neutral")
        self.assertEqual(positions_data["closed_positions"][-1]["close_source"],
                         "snaptrade_fill")

    def test_real_win_uses_snaptrade_fill_not_peak(self):
        # Peak high was $150 but actual fill was $135 → result_pct uses 135, not 150
        pos = {"ticker": "AAOI", "shares": 75, "entry_price": 100.0,
               "highest_price_seen": 150.0, "stop_price": 95.0, "status": "active"}
        positions_data = self._state(open_positions=[pos])
        ts = self._trading()
        sell_fills = {"AAOI": {"price": 135.0, "date": "2026-04-24", "units": 75}}
        pm.sync_snaptrade_with_rules(
            snaptrade_positions=[], positions_data=positions_data,
            trading_state=ts, market_state="GREEN", sell_fills=sell_fills,
        )
        closed = positions_data["closed_positions"][-1]
        self.assertEqual(closed["close_price"], 135.0)
        self.assertEqual(closed["result_pct"], 35.0)
        self.assertEqual(closed["close_source"], "snaptrade_fill")
        self.assertEqual(ts["consecutive_wins"], 1)

    def test_loss_close_bumps_loss_streak(self):
        pos = {"ticker": "X", "shares": 10, "entry_price": 100.0,
               "highest_price_seen": 110.0, "stop_price": 95.0, "status": "active"}
        positions_data = self._state(open_positions=[pos])
        ts = self._trading(consecutive_wins=1)
        sell_fills = {"X": {"price": 90.0, "date": "2026-04-24", "units": 10}}
        pm.sync_snaptrade_with_rules(
            snaptrade_positions=[], positions_data=positions_data,
            trading_state=ts, market_state="GREEN", sell_fills=sell_fills,
        )
        self.assertEqual(ts["consecutive_losses"], 1)
        self.assertEqual(ts["consecutive_wins"], 0)

    def test_falls_back_to_quote_when_no_fill(self):
        pos = {"ticker": "Z", "shares": 10, "entry_price": 100.0,
               "highest_price_seen": 150.0, "stop_price": 95.0, "status": "active"}
        positions_data = self._state(open_positions=[pos])
        ts = self._trading()
        with patch.object(pm, "fetch_position_metrics",
                          return_value={"price": 105.0, "atr_pct": 4.0}):
            pm.sync_snaptrade_with_rules(
                snaptrade_positions=[], positions_data=positions_data,
                trading_state=ts, market_state="GREEN", sell_fills={},
            )
        closed = positions_data["closed_positions"][-1]
        self.assertEqual(closed["close_price"], 105.0)
        self.assertEqual(closed["close_source"], "live_quote")


class ShareDriftReconcileTests(unittest.TestCase):
    """When a ticker stays in both SnapTrade and positions.json but the share
    count diverges, we must reconcile or the rules engine sizes off stale data."""

    def _trading(self):
        return {"consecutive_wins": 0, "consecutive_losses": 0,
                "total_wins": 0, "total_losses": 0,
                "recent_trades": [], "current_sizing_mode": "normal",
                "last_updated": ""}

    def test_avg_up_recomputes_entry_and_targets(self):
        # GLW-style: positions.json has 30 shares @ $167.10, SnapTrade now reports 50 @ $170 weighted.
        rules_pos = {"ticker": "GLW", "shares": 30, "entry_price": 167.10,
                     "highest_price_seen": 178.99, "stop_price": 159.95,
                     "target1": 200.52, "target2": 233.94, "status": "active",
                     "breakeven_activated": False, "target1_hit": False}
        snap = [{"ticker": "GLW", "shares": 50, "avg_cost": 170.00,
                 "current_price": 175.0, "account_id": "a"}]
        positions_data = {"open_positions": [rules_pos], "closed_positions": []}
        ts = self._trading()
        alerts = pm.sync_snaptrade_with_rules(snap, positions_data, ts, "GREEN", sell_fills={})
        self.assertEqual(rules_pos["shares"], 50)
        self.assertEqual(rules_pos["entry_price"], 170.00)
        self.assertAlmostEqual(rules_pos["target1"], 204.0, places=1)
        self.assertAlmostEqual(rules_pos["target2"], 238.0, places=1)
        self.assertFalse(rules_pos["target1_hit"])
        self.assertFalse(rules_pos["breakeven_activated"])
        self.assertTrue(any("SHARES INCREASED" in a for a in alerts))

    def test_partial_sell_keeps_entry_and_targets(self):
        rules_pos = {"ticker": "X", "shares": 100, "entry_price": 50.0,
                     "highest_price_seen": 60.0, "stop_price": 47.5,
                     "target1": 60.0, "target2": 70.0, "status": "active",
                     "target1_hit": True, "breakeven_activated": True}
        snap = [{"ticker": "X", "shares": 50, "avg_cost": 50.0,
                 "current_price": 58.0, "account_id": "a"}]
        positions_data = {"open_positions": [rules_pos], "closed_positions": []}
        ts = self._trading()
        alerts = pm.sync_snaptrade_with_rules(snap, positions_data, ts, "GREEN", sell_fills={})
        self.assertEqual(rules_pos["shares"], 50)
        self.assertEqual(rules_pos["entry_price"], 50.0)
        self.assertEqual(rules_pos["target1"], 60.0)  # unchanged
        self.assertTrue(rules_pos["target1_hit"])     # preserved
        self.assertTrue(any("PARTIAL SELL" in a for a in alerts))


    def test_no_drift_no_alert(self):
        rules_pos = {"ticker": "X", "shares": 100, "entry_price": 50.0,
                     "highest_price_seen": 60.0, "stop_price": 47.5,
                     "target1": 60.0, "target2": 70.0, "status": "active"}
        snap = [{"ticker": "X", "shares": 100, "avg_cost": 50.0,
                 "current_price": 58.0, "account_id": "a"}]
        positions_data = {"open_positions": [rules_pos], "closed_positions": []}
        alerts = pm.sync_snaptrade_with_rules(snap, positions_data, self._trading(),
                                              "GREEN", sell_fills={})
        self.assertFalse(any("SHARES" in a or "PARTIAL" in a for a in alerts))


class RetroPatchClosedTests(unittest.TestCase):
    """Once SnapTrade catches up on lagged after-hours SELLs, retro-patch the
    closed_positions records that used fallback or user-reported pricing."""

    def _ts(self, **over):
        base = {"consecutive_wins": 0, "consecutive_losses": 0,
                "total_wins": 0, "total_losses": 0, "recent_trades": []}
        base.update(over)
        return base

    def _today(self):
        import datetime
        return datetime.date.today().isoformat()

    def test_breakeven_user_reported_flips_to_win_when_real_fill_arrives(self):
        # FLY/PL-style: was tagged user_reported_breakeven (0%); real SnapTrade
        # SELL came in later at $42 — should become a real win.
        cd = self._today()
        closed = {"ticker": "FLY", "entry_price": 34.44, "shares": 200,
                  "close_price": 34.44, "result_pct": 0.0, "close_date": cd,
                  "close_source": "user_reported_breakeven"}
        pd = {"open_positions": [], "closed_positions": [closed]}
        ts = self._ts(total_wins=0)
        ts["recent_trades"] = [{"ticker": "FLY", "result": "neutral",
                                "result_pct": 0.0, "date": cd, "side": "SELL"}]
        sell_fills = {"FLY": {"price": 42.00, "date": cd, "units": 200}}
        alerts = pm.retro_patch_closed_positions(pd, ts, sell_fills)
        self.assertEqual(closed["close_price"], 42.00)
        self.assertAlmostEqual(closed["result_pct"], 21.95, places=1)
        self.assertEqual(closed["close_source"], "snaptrade_fill_retro")
        self.assertEqual(ts["total_wins"], 1)  # neutral → win flip
        self.assertEqual(ts["recent_trades"][0]["result"], "win")
        self.assertTrue(any("RETRO-PATCHED" in a for a in alerts))

    def test_no_patch_when_already_real_source(self):
        cd = self._today()
        closed = {"ticker": "AMD", "entry_price": 246.06, "shares": 25,
                  "close_price": 293.09, "result_pct": 19.11, "close_date": cd,
                  "close_source": "snaptrade_fill"}
        pd = {"open_positions": [], "closed_positions": [closed]}
        sell_fills = {"AMD": {"price": 300.0, "date": cd, "units": 25}}
        alerts = pm.retro_patch_closed_positions(pd, self._ts(), sell_fills)
        self.assertEqual(closed["close_price"], 293.09)  # unchanged
        self.assertEqual(alerts, [])

    def test_no_patch_when_fill_missing(self):
        cd = self._today()
        closed = {"ticker": "X", "entry_price": 10, "shares": 100,
                  "close_price": 10, "result_pct": 0, "close_date": cd,
                  "close_source": "user_reported_breakeven"}
        pd = {"open_positions": [], "closed_positions": [closed]}
        alerts = pm.retro_patch_closed_positions(pd, self._ts(), sell_fills={})
        self.assertEqual(closed["close_source"], "user_reported_breakeven")
        self.assertEqual(alerts, [])

    def test_does_not_patch_old_closes_outside_lookback(self):
        # close_date 30 days ago, lookback 14 → skip
        import datetime
        old = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
        closed = {"ticker": "X", "entry_price": 10, "shares": 100,
                  "close_price": 10, "result_pct": 0, "close_date": old,
                  "close_source": "user_reported_breakeven"}
        pd = {"open_positions": [], "closed_positions": [closed]}
        sell_fills = {"X": {"price": 12, "date": old, "units": 100}}
        alerts = pm.retro_patch_closed_positions(pd, self._ts(), sell_fills, lookback_days=14)
        self.assertEqual(closed["close_price"], 10)
        self.assertEqual(alerts, [])


class RecentEventTests(unittest.TestCase):
    """_append_recent_event writes the correct shape and never raises."""

    def _run(self, tmp_dir, **kwargs):
        import os, json
        from utils.events import _append_recent_event
        with patch.dict(os.environ, {"DATA_DIR": tmp_dir}):
            _append_recent_event(**kwargs)
        with open(os.path.join(tmp_dir, "recent_events.json")) as f:
            return json.load(f)

    def test_writes_correct_shape(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            data = self._run(d, category="stop_hit", title="AAPL stop hit @ $150.00", severity="high")
        self.assertIn("events", data)
        self.assertIn("updated", data)
        ev = data["events"][-1]
        self.assertEqual(ev["category"], "stop_hit")
        self.assertEqual(ev["title"], "AAPL stop hit @ $150.00")
        self.assertEqual(ev["severity"], "high")
        self.assertIn("ts", ev)
        self.assertIn("date", ev)

    def test_detail_field_included_when_provided(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            data = self._run(d, category="position_close", title="AAPL closed +5%",
                             detail="Entry $140 → $147 (fill)")
        ev = data["events"][-1]
        self.assertEqual(ev["detail"], "Entry $140 → $147 (fill)")

    def test_detail_omitted_when_not_provided(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            data = self._run(d, category="breakeven", title="AAPL breakeven set")
        ev = data["events"][-1]
        self.assertNotIn("detail", ev)

    def test_rolling_cap_at_max_keep(self):
        import tempfile, os
        from utils.events import _append_recent_event
        with tempfile.TemporaryDirectory() as d:
            with patch.dict(os.environ, {"DATA_DIR": d}):
                for i in range(7):
                    _append_recent_event("market_state", f"event {i}", max_keep=5)
            import json
            with open(os.path.join(d, "recent_events.json")) as f:
                data = json.load(f)
        self.assertEqual(len(data["events"]), 5)
        self.assertEqual(data["events"][-1]["title"], "event 6")

    def test_never_raises_on_bad_dir(self):
        import os
        from utils.events import _append_recent_event
        # Should not raise even when DATA_DIR doesn't exist
        with patch.dict(os.environ, {"DATA_DIR": "/nonexistent/path/xyz"}):
            try:
                _append_recent_event("stop_hit", "test")
            except Exception as e:
                self.fail(f"_append_recent_event raised: {e}")




if __name__ == "__main__":
    unittest.main()
