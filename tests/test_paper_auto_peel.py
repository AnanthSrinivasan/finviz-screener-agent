"""
Unit tests for T1/T2 auto-peel in agents/trading/alpaca_monitor.py.
"""

import unittest

from agents.trading import alpaca_monitor as am


class ProcessTargetPeelsTests(unittest.TestCase):
    def setUp(self):
        self.sell_calls = []
        self.slack_msgs = []
        self.stop_info = {
            "entry_price": 100.0, "stop_price": 95.0,
            "t1_peeled": False, "t2_peeled": False,
        }

    def _sell_ok(self, ticker, qty):
        self.sell_calls.append((ticker, qty))
        return {"id": "ord-1", "status": "accepted"}

    def _sell_fail(self, ticker, qty):
        self.sell_calls.append((ticker, qty))
        return {}

    def _slack(self, msg):
        self.slack_msgs.append(msg)

    def test_t1_event_peels_half_and_raises_stop_to_breakeven(self):
        events = [{"kind": "target1", "ticker": "FOO", "message": "T1 hit"}]
        sells, remaining = am.process_target_peels(
            "FOO", events, self.stop_info,
            qty=10, current_price=120.0,
            sell_fn=self._sell_ok, slack_fn=self._slack,
        )
        self.assertEqual(sells, 1)
        self.assertEqual(remaining, 5)
        self.assertEqual(self.sell_calls, [("FOO", "5")])
        self.assertTrue(self.stop_info["t1_peeled"])
        self.assertEqual(self.stop_info["stop_price"], 100.5)  # entry × 1.005
        self.assertTrue(any("T1 AUTO-PEEL" in m for m in self.slack_msgs))

    def test_t2_event_peels_half_of_remaining(self):
        self.stop_info["t1_peeled"] = True  # already past T1
        events = [{"kind": "target2", "ticker": "FOO", "message": "T2 hit"}]
        sells, remaining = am.process_target_peels(
            "FOO", events, self.stop_info,
            qty=10, current_price=140.0,
            sell_fn=self._sell_ok, slack_fn=self._slack,
        )
        self.assertEqual(sells, 1)
        self.assertEqual(remaining, 5)
        self.assertEqual(self.sell_calls, [("FOO", "5")])
        self.assertTrue(self.stop_info["t2_peeled"])
        self.assertTrue(any("T2 AUTO-PEEL" in m for m in self.slack_msgs))

    def test_qty_one_skips_peel(self):
        events = [{"kind": "target1", "ticker": "FOO", "message": "T1 hit"}]
        sells, remaining = am.process_target_peels(
            "FOO", events, self.stop_info,
            qty=1, current_price=120.0,
            sell_fn=self._sell_ok, slack_fn=self._slack,
        )
        self.assertEqual(sells, 0)
        self.assertEqual(remaining, 1)
        self.assertEqual(self.sell_calls, [])
        self.assertFalse(self.stop_info["t1_peeled"])

    def test_sub_notional_lot_skipped(self):
        # 2 sh × $20 = $40 < $50 floor → skip
        events = [{"kind": "target1", "ticker": "FOO", "message": "T1 hit"}]
        sells, remaining = am.process_target_peels(
            "FOO", events, self.stop_info,
            qty=4, current_price=20.0,
            sell_fn=self._sell_ok, slack_fn=self._slack,
        )
        self.assertEqual(sells, 0)
        self.assertEqual(self.sell_calls, [])
        self.assertFalse(self.stop_info["t1_peeled"])

    def test_t1_idempotent_when_already_peeled(self):
        self.stop_info["t1_peeled"] = True
        events = [{"kind": "target1", "ticker": "FOO", "message": "T1 hit"}]
        sells, remaining = am.process_target_peels(
            "FOO", events, self.stop_info,
            qty=10, current_price=120.0,
            sell_fn=self._sell_ok, slack_fn=self._slack,
        )
        self.assertEqual(sells, 0)
        self.assertEqual(self.sell_calls, [])

    def test_sell_failure_keeps_t1_unpeeled(self):
        events = [{"kind": "target1", "ticker": "FOO", "message": "T1 hit"}]
        sells, remaining = am.process_target_peels(
            "FOO", events, self.stop_info,
            qty=10, current_price=120.0,
            sell_fn=self._sell_fail, slack_fn=self._slack,
        )
        self.assertEqual(sells, 0)
        self.assertFalse(self.stop_info["t1_peeled"])

    def test_non_peel_events_forwarded_to_slack(self):
        events = [
            {"kind": "breakeven", "ticker": "FOO", "message": "BE locked"},
            {"kind": "fade",      "ticker": "FOO", "message": "fading"},
        ]
        sells, remaining = am.process_target_peels(
            "FOO", events, self.stop_info,
            qty=10, current_price=120.0,
            sell_fn=self._sell_ok, slack_fn=self._slack,
        )
        self.assertEqual(sells, 0)
        self.assertEqual(self.slack_msgs, ["BE locked", "fading"])


class MigrateAddsPeelFlagsTests(unittest.TestCase):
    def test_migrate_seeds_t1_t2_peeled_false(self):
        entry = {"entry_price": 100.0, "stop_price": 95.0}
        out = am.migrate_stop_entry("FOO", entry, 100.0)
        self.assertFalse(out["t1_peeled"])
        self.assertFalse(out["t2_peeled"])

    def test_migrate_preserves_existing_peel_flags(self):
        entry = {"entry_price": 100.0, "t1_peeled": True, "t2_peeled": False}
        out = am.migrate_stop_entry("FOO", entry, 100.0)
        self.assertTrue(out["t1_peeled"])
        self.assertFalse(out["t2_peeled"])


if __name__ == "__main__":
    unittest.main()
