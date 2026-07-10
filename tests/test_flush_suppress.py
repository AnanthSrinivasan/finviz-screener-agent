"""
Flush-suppress stop filter — spec docs/specs/flush-suppress-stop-filter.md.

Reference cases (7/8/2026 close):
  - TEM:  stop breached on an index flush, held its 21 EMA → suppress (save).
  - DAVE: high-momentum (Perf Month ≥ 40) breached 2 closes, held 8 EMA → suppress.
  - TNA:  broke stop AND both EMAs → exit (real loser).
Non-activation regressions: June-2026-selloff-style tape (SPY below 50 SMA)
and elevated VIX must never activate the window.
"""

import unittest

from agents.trading import rules
import agents.trading.position_monitor as pm


def make_history(down4_by_day, spy_sma50_pct=1.5, vix_close=16.0):
    """Build a market_monitor_history-style record list, oldest first.
    `down4_by_day` = list of down_4_today counts; index/context fields only on
    records because flush_window_active reads structure off the latest one."""
    hist = []
    for i, d4 in enumerate(down4_by_day):
        hist.append({
            "date": "2026-07-0" + str(i + 1),
            "down_4_today": d4,
            "spy_sma50_pct": spy_sma50_pct,
            "vix_close": vix_close,
        })
    return hist


class TestFlushWindowActive(unittest.TestCase):

    def test_flush_today_is_day_1(self):
        ctx = rules.flush_window_active(make_history([100, 90, 454]))
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx["day"], 1)
        self.assertEqual(ctx["down_4"], 454)

    def test_flush_yesterday_is_day_2(self):
        # TEM reference: 7/7 flush (454 down-4%), breach evaluated 7/8 close.
        ctx = rules.flush_window_active(make_history([100, 454, 71]))
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx["day"], 2)

    def test_flush_two_sessions_ago_is_day_3(self):
        ctx = rules.flush_window_active(make_history([454, 100, 90]))
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx["day"], 3)

    def test_flush_three_sessions_ago_expired(self):
        ctx = rules.flush_window_active(make_history([454, 100, 90, 80]))
        self.assertIsNone(ctx)

    def test_below_threshold_no_window(self):
        ctx = rules.flush_window_active(make_history([399, 100, 90]))
        self.assertIsNone(ctx)

    def test_spy_below_50sma_never_activates(self):
        # June-2026-selloff regression: real breakdown, stops behave as today.
        ctx = rules.flush_window_active(
            make_history([100, 600, 500], spy_sma50_pct=-2.0))
        self.assertIsNone(ctx)

    def test_elevated_vix_never_activates(self):
        ctx = rules.flush_window_active(
            make_history([100, 600, 500], vix_close=24.0))
        self.assertIsNone(ctx)

    def test_missing_fields_never_activate(self):
        self.assertIsNone(rules.flush_window_active([]))
        self.assertIsNone(rules.flush_window_active(
            [{"date": "2026-07-01", "down_4_today": 600}]))


class TestShouldSuppressStopExit(unittest.TestCase):

    def setUp(self):
        self.ctx = {"day": 2, "max_days": 3, "flush_date": "2026-07-07"}

    def test_tem_class_above_21ema_suppresses(self):
        # Flat closes ~54 → 21 EMA ≈ 54; close 57.31 above it, in profit.
        closes = [54.0] * 30
        ok, reason = rules.should_suppress_stop_exit(
            closes, 57.31, 4.0, 50.0, 14.0, self.ctx)
        self.assertTrue(ok)
        self.assertIn("21 EMA", reason)
        self.assertIn("flush day 2/3", reason)

    def test_dave_class_high_momentum_uses_8ema(self):
        closes = [370.0] * 30
        ok, reason = rules.should_suppress_stop_exit(
            closes, 380.0, 6.0, 282.0, 44.0, self.ctx)
        self.assertTrue(ok)
        self.assertIn("8 EMA", reason)

    def test_tna_class_below_ema_exits(self):
        closes = [100.0 - i for i in range(30)]  # declining — EMA above close
        ok, reason = rules.should_suppress_stop_exit(
            closes, 70.0, 5.0, 60.0, 10.0, self.ctx)
        self.assertFalse(ok)
        self.assertIn("below", reason)

    def test_profit_floor_price_below_entry_exits(self):
        closes = [54.0] * 30
        ok, reason = rules.should_suppress_stop_exit(
            closes, 49.0, 4.0, 50.0, 14.0, self.ctx)
        self.assertFalse(ok)
        self.assertIn("never widens a loss", reason)

    def test_no_flush_window_exits(self):
        ok, _ = rules.should_suppress_stop_exit(
            [54.0] * 30, 57.31, 4.0, 50.0, 14.0, None)
        self.assertFalse(ok)

    def test_insufficient_closes_exits(self):
        ok, reason = rules.should_suppress_stop_exit(
            [54.0] * 10, 57.31, 4.0, 50.0, 14.0, self.ctx)
        self.assertFalse(ok)
        self.assertIn("insufficient closes", reason)

    def test_momentum_threshold_boundary(self):
        self.assertEqual(rules.flush_structure_ema_span(39.9), 21)
        self.assertEqual(rules.flush_structure_ema_span(40.0), 8)


class TestEvaluateFlushSuppress(unittest.TestCase):

    def setUp(self):
        self.ctx = {"day": 2, "max_days": 3}

    def test_suppress_when_all_conditions_hold(self):
        action, _ = rules.evaluate_flush_suppress(
            [54.0] * 30, 57.31, 4.0, 50.0, 14.0, self.ctx,
            already_active=False, is_post_close=False)
        self.assertEqual(action, "suppress")

    def test_defer_intraday_ema_wobble_when_active(self):
        # Structure momentarily below EMA intraday during active suppression —
        # close-based rule: hold this run, post-close decides.
        closes = [100.0 - i for i in range(30)]
        action, reason = rules.evaluate_flush_suppress(
            closes, 70.0, 5.0, 60.0, 10.0, self.ctx,
            already_active=True, is_post_close=False)
        self.assertEqual(action, "defer")
        self.assertIn("post-close", reason)

    def test_post_close_below_ema_exits_no_second_chance(self):
        closes = [100.0 - i for i in range(30)]
        action, _ = rules.evaluate_flush_suppress(
            closes, 70.0, 5.0, 60.0, 10.0, self.ctx,
            already_active=True, is_post_close=True)
        self.assertEqual(action, "exit")

    def test_intraday_collapse_below_entry_exits_even_when_active(self):
        closes = [54.0] * 30
        action, _ = rules.evaluate_flush_suppress(
            closes, 49.0, 4.0, 50.0, 14.0, self.ctx,
            already_active=True, is_post_close=False)
        self.assertEqual(action, "exit")

    def test_no_window_exits(self):
        action, _ = rules.evaluate_flush_suppress(
            [54.0] * 30, 57.31, 4.0, 50.0, 14.0, None,
            already_active=True, is_post_close=False)
        self.assertEqual(action, "exit")


class TestPositionMonitorFlushIntegration(unittest.TestCase):
    """apply_minervini_rules routes the stop breach through flush suppress."""

    def _pos(self):
        return {
            "ticker": "TEM",
            "entry_price": 50.0,
            "stop_price": 57.35,
            "highest_price_seen": 58.0,
            "peak_gain_pct": 16.0,
            "target1": 57.5, "target1_hit": True,
            "target2": 65.0,
            "status": "active",
        }

    def _ctx(self):
        return {"day": 2, "max_days": 3, "flush_date": "2026-07-07"}

    def test_flush_suppress_event_replaces_stop_hit(self):
        pos = self._pos()
        alerts, modified, events = pm.apply_minervini_rules(
            pos, current_price=57.31, atr=2.0,
            daily_closes=[54.0] * 30, flush_ctx=self._ctx(),
            perf_month=14.0, is_post_close=False)
        kinds = [e["kind"] for e in events]
        self.assertIn("flush_suppress", kinds)
        self.assertNotIn("stop_hit", kinds)
        self.assertTrue(pos.get("flush_suppress_active"))
        self.assertEqual(pos.get("flush_suppress_day"), 2)
        self.assertTrue(modified)
        self.assertTrue(any("FLUSH SUPPRESS" in a for a in alerts))

    def test_flush_suppress_alert_dedups_same_day(self):
        pos = self._pos()
        pm.apply_minervini_rules(
            pos, current_price=57.31, atr=2.0,
            daily_closes=[54.0] * 30, flush_ctx=self._ctx(),
            perf_month=14.0)
        _, _, events2 = pm.apply_minervini_rules(
            pos, current_price=57.31, atr=2.0,
            daily_closes=[54.0] * 30, flush_ctx=self._ctx(),
            perf_month=14.0)
        self.assertNotIn("flush_suppress", [e["kind"] for e in events2])
        self.assertTrue(pos.get("flush_suppress_active"))

    def test_post_close_structure_break_fires_stop_hit(self):
        pos = self._pos()
        pos["flush_suppress_active"] = True
        pos["flush_suppress_day"] = 1
        alerts, _, events = pm.apply_minervini_rules(
            pos, current_price=57.31, atr=2.0,
            daily_closes=[70.0] * 30,  # EMA far above price — structure broken
            flush_ctx=self._ctx(), perf_month=14.0, is_post_close=True)
        kinds = [e["kind"] for e in events]
        self.assertIn("stop_hit", kinds)
        self.assertNotIn("flush_suppress", kinds)
        self.assertFalse(pos.get("flush_suppress_active"))
        self.assertTrue(any("flush suppress ended" in a for a in alerts))

    def test_window_expiry_clears_state(self):
        pos = self._pos()
        pos["flush_suppress_active"] = True
        pos["flush_suppress_day"] = 3
        pos["flush_suppress_alerted_date"] = "2026-07-09"
        # Price recovered above stop, window gone — clean exit from suppression.
        _, modified, events = pm.apply_minervini_rules(
            pos, current_price=59.0, atr=2.0, flush_ctx=None)
        self.assertFalse(pos.get("flush_suppress_active"))
        self.assertNotIn("flush_suppress_day", pos)
        self.assertNotIn("stop_hit", [e["kind"] for e in events])

    def test_no_flush_ctx_behaves_as_before(self):
        pos = self._pos()
        _, _, events = pm.apply_minervini_rules(
            pos, current_price=57.31, atr=8.0, flush_ctx=None)
        self.assertIn("stop_hit", [e["kind"] for e in events])

    def test_flush_suppress_is_critical_event_kind(self):
        self.assertIn("flush_suppress", rules.CRITICAL_EVENT_KINDS)


if __name__ == "__main__":
    unittest.main()
