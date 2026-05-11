"""Tests for the consolidated position-book Slack message."""

import unittest

from agents.trading import book_table


class ComputeStateTests(unittest.TestCase):
    def test_hold_default(self):
        pos = {"ticker": "TNA", "entry_price": 63.04, "stop_price": 61.90, "peak_gain_pct": 7.2}
        self.assertEqual(book_table.compute_state(pos, 65.11), book_table.STATE_HOLD)

    def test_stop_near_within_half_pct(self):
        pos = {"ticker": "GLW", "entry_price": 165.09, "stop_price": 186.92, "peak_gain_pct": 20.1}
        # current $186.91 vs stop $186.92 → diff < 0.5%
        self.assertEqual(book_table.compute_state(pos, 186.91), book_table.STATE_STOP_NEAR)

    def test_round_trip_giveback_over_18pp(self):
        pos = {"ticker": "CRWV", "entry_price": 115.71, "stop_price": 125.40, "peak_gain_pct": 19.46}
        # current 113.99 → -1.5%, peak +19.46 → giveback ~21pp ≥ 18 AND peak ≥ 15
        self.assertEqual(book_table.compute_state(pos, 113.99), book_table.STATE_ROUND_TRIP)

    def test_trim_when_t1_hit_and_giveback_over_10pp(self):
        pos = {
            "ticker": "AAOI", "entry_price": 124.75, "stop_price": 174.36,
            "peak_gain_pct": 53.61, "target1_hit": True,
        }
        # 148.99 → +19.4%, peak +53.6% → giveback ~34pp, peak ≥ 25, T1 hit → TRIM
        self.assertEqual(book_table.compute_state(pos, 148.99), book_table.STATE_TRIM)

    def test_stopped_when_flagged(self):
        pos = {"ticker": "X", "entry_price": 10, "stop_price": 9, "peak_gain_pct": 0}
        self.assertEqual(
            book_table.compute_state(pos, 9.0, stopped_this_run=True),
            book_table.STATE_STOPPED,
        )


class ActionBlockTests(unittest.TestCase):
    def test_actions_sorted_by_severity(self):
        rows = [
            ({"ticker": "TNA", "entry_price": 63, "peak_gain_pct": 7, "stop_price": 61.9}, 65.0,  book_table.STATE_HOLD),
            ({"ticker": "AAOI","entry_price": 124.75, "peak_gain_pct": 53.61, "stop_price": 174.36, "target1_hit": True}, 148.99, book_table.STATE_TRIM),
            ({"ticker": "GLW", "entry_price": 165.09, "peak_gain_pct": 20.1, "stop_price": 186.92}, 186.91, book_table.STATE_STOP_NEAR),
        ]
        out = book_table.build_action_block(rows)
        self.assertIn("ACTIONS TODAY", out)
        # STOP_NEAR (sev=1) must precede TRIM (sev=3); HOLD must not appear.
        self.assertLess(out.index("GLW"), out.index("AAOI"))
        self.assertNotIn("TNA", out)

    def test_no_actions_returns_empty(self):
        rows = [({"ticker": "X", "entry_price": 1}, 1, book_table.STATE_HOLD)]
        self.assertEqual(book_table.build_action_block(rows), "")


class EventsDigestTests(unittest.TestCase):
    def test_groups_by_ticker(self):
        events = [
            {"kind": "stop_hit", "ticker": "GLW", "message": "GLW stop hit", "ts": "2026-05-09T13:22Z"},
            {"kind": "fade",     "ticker": "AAOI", "message": "AAOI fading"},
        ]
        out = book_table.build_events_digest(events)
        self.assertIn("EVENTS SINCE LAST POST", out)
        self.assertIn("GLW", out)
        self.assertIn("AAOI", out)

    def test_empty_returns_empty(self):
        self.assertEqual(book_table.build_events_digest([]), "")

    def test_sections_ordered_stops_before_targets_before_new(self):
        events = [
            {"kind": "auto_added", "ticker": "COHR", "message": "COHR new position"},
            {"kind": "target1",    "ticker": "GLW",  "message": "GLW T1 hit"},
            {"kind": "stop_hit",   "ticker": "CRWV", "message": "CRWV stop hit"},
        ]
        out = book_table.build_events_digest(events)
        self.assertLess(out.index("Stops"), out.index("Target 1"))
        self.assertLess(out.index("Target 1"), out.index("New positions"))

    def test_t1_and_t2_separate_sections(self):
        events = [
            {"kind": "target1", "ticker": "GLW",  "message": "GLW T1 hit"},
            {"kind": "target2", "ticker": "AAOI", "message": "AAOI T2 hit"},
        ]
        out = book_table.build_events_digest(events)
        self.assertIn("Target 1", out)
        self.assertIn("Target 2", out)
        self.assertLess(out.index("Target 2"), out.index("Target 1"))

    def test_strips_slack_emoji_codes_and_newlines(self):
        events = [{
            "kind": "auto_added", "ticker": "COHR",
            "message": ":large_green_circle: AUTO-DETECTED NEW POSITION: COHR\n   20 shares @ $344.31",
            "ts": "2026-05-11T16:13:03.384850Z",
        }]
        out = book_table.build_events_digest(events)
        self.assertNotIn(":large_green_circle:", out)
        self.assertNotIn("\n   ", out)  # multi-line indent gone
        self.assertIn("20 shares", out)
        self.assertIn("[16:13]", out)  # short ts

    def test_alert_type_routes_to_warn_section(self):
        events = [
            {"kind": "info", "ticker": "ZVRA", "alert_type": "WARN_STOP",
             "message": "WARN_STOP — ZVRA ATR mult 2.01"},
        ]
        out = book_table.build_events_digest(events)
        self.assertIn("Warn", out)
        self.assertIn("ZVRA", out)

    def test_retro_patch_routed_by_message(self):
        events = [
            {"kind": "info", "ticker": "FIGS",
             "message": "RETRO-PATCHED CLOSE: FIGS — $10.92 → $11.30"},
        ]
        out = book_table.build_events_digest(events)
        self.assertIn("Retro-patched", out)
        self.assertIn("FIGS", out)

    def test_avg_up_and_partial_sell_separate(self):
        events = [
            {"kind": "share_drift_avg_up",       "ticker": "APLD", "message": "APLD 50 → 100"},
            {"kind": "share_drift_partial_sell", "ticker": "FOO",  "message": "FOO 200 → 100"},
        ]
        out = book_table.build_events_digest(events)
        self.assertIn("Avg up", out)
        self.assertIn("Partial sell", out)
        self.assertLess(out.index("Avg up"), out.index("Partial sell"))


class ComposeBookTests(unittest.TestCase):
    def test_full_message_renders_table_actions_digest(self):
        positions = [
            {
                "ticker": "AAOI", "shares": 100, "entry_price": 124.75,
                "stop_price": 174.36, "peak_gain_pct": 53.61, "target1_hit": True,
            },
            {
                "ticker": "TNA", "shares": 100, "entry_price": 63.04,
                "stop_price": 61.90, "peak_gain_pct": 7.2,
            },
        ]
        out = book_table.compose_book_message(
            positions,
            live_prices={"AAOI": 148.99, "TNA": 65.11},
            market_state="GREEN",
            sizing_mode="suspended",
            events_since_last=[
                {"kind": "stop_hit", "ticker": "AAOI", "message": "AAOI fade"},
            ],
            header_label="14:30 UTC",
        )
        self.assertIn("POSITION BOOK", out)
        self.assertIn("AAOI", out)
        self.assertIn("TNA", out)
        self.assertIn("ACTIONS TODAY", out)
        self.assertIn("EVENTS SINCE LAST POST", out)
        self.assertIn("Market: GREEN", out)
        self.assertIn("SUSPENDED", out)


if __name__ == "__main__":
    unittest.main()
