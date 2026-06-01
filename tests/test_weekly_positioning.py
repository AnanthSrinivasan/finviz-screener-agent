"""Tests for agents.utils.weekly_positioning — Weekly Review §1."""

import unittest

from agents.utils.weekly_positioning import (
    book_health,
    build_positioning_summary,
    realized_pnl_for_week,
    render_positioning_html,
    render_positioning_slack,
)


class RealizedWeekTests(unittest.TestCase):
    def test_only_counts_sells_in_window(self):
        history = {
            "AAA": [
                {"date": "2026-05-01T10:00:00Z", "action": "BUY", "shares": 100, "price": 10.0},
                # SELL inside window: avg cost 10, sold at 12 → +200
                {"date": "2026-05-28T15:00:00Z", "action": "SELL", "shares": 100, "price": 12.0},
            ],
            "BBB": [
                {"date": "2026-05-02T10:00:00Z", "action": "BUY", "shares": 50, "price": 20.0},
                # SELL BEFORE window — must be excluded
                {"date": "2026-05-10T10:00:00Z", "action": "SELL", "shares": 50, "price": 25.0},
            ],
        }
        out = realized_pnl_for_week(history, since="2026-05-25", until="2026-05-31")
        self.assertAlmostEqual(out["total"], 200.0, places=2)
        self.assertEqual(out["wins"], 1)
        self.assertEqual(out["losses"], 0)
        self.assertIn("AAA", out["per_ticker"])
        self.assertNotIn("BBB", out["per_ticker"])

    def test_avg_cost_basis_uses_prior_buys(self):
        # Two buys before window establish weighted avg cost; sell in window
        history = {
            "CCC": [
                {"date": "2026-05-01T10:00:00Z", "action": "BUY", "shares": 100, "price": 10.0},
                {"date": "2026-05-02T10:00:00Z", "action": "BUY", "shares": 100, "price": 20.0},
                # avg cost = 15; sell 100 @ 18 in window → +300
                {"date": "2026-05-28T10:00:00Z", "action": "SELL", "shares": 100, "price": 18.0},
            ],
        }
        out = realized_pnl_for_week(history, since="2026-05-25", until="2026-05-31")
        self.assertAlmostEqual(out["per_ticker"]["CCC"], 300.0, places=2)

    def test_biggest_winner_and_loser(self):
        history = {
            "WIN": [
                {"date": "2026-05-01", "action": "BUY", "shares": 10, "price": 10.0},
                {"date": "2026-05-28", "action": "SELL", "shares": 10, "price": 20.0},  # +100
            ],
            "LOSE": [
                {"date": "2026-05-01", "action": "BUY", "shares": 10, "price": 10.0},
                {"date": "2026-05-28", "action": "SELL", "shares": 10, "price": 5.0},   # -50
            ],
        }
        out = realized_pnl_for_week(history, since="2026-05-25", until="2026-05-31")
        self.assertEqual(out["biggest_winner"][0], "WIN")
        self.assertAlmostEqual(out["biggest_winner"][1], 100.0)
        self.assertEqual(out["biggest_loser"][0], "LOSE")
        self.assertAlmostEqual(out["biggest_loser"][1], -50.0)

    def test_empty_history(self):
        out = realized_pnl_for_week({}, since="2026-05-25")
        self.assertEqual(out["total"], 0.0)
        self.assertIsNone(out["biggest_winner"])


class BookHealthTests(unittest.TestCase):
    def test_buckets(self):
        positions = [
            # green: gain > 0, above stop
            {"ticker": "G", "entry_price": 100, "current_gain_pct": 8.0,
             "stop_price": 95, "shares": 10},
            # underwater: gain <= 0 but above stop
            {"ticker": "U", "entry_price": 100, "current_gain_pct": -2.0,
             "stop_price": 90, "shares": 10},
            # past stop held: current price <= stop
            {"ticker": "S", "entry_price": 100, "current_gain_pct": -12.0,
             "stop_price": 92, "shares": 10},
        ]
        h = book_health(positions)
        self.assertEqual(h["green"], 1)
        self.assertEqual(h["underwater"], 1)
        self.assertEqual(h["past_stop_held"], 1)
        self.assertIn("S", h["leak_names"])
        # S: current 88 vs entry 100 over 10 shares = -120
        self.assertAlmostEqual(h["leak_usd"], -120.0, places=0)

    def test_past_stop_not_double_counted_as_underwater(self):
        positions = [
            {"ticker": "S", "entry_price": 100, "current_gain_pct": -12.0,
             "stop_price": 92, "shares": 10},
        ]
        h = book_health(positions)
        self.assertEqual(h["underwater"], 0)
        self.assertEqual(h["past_stop_held"], 1)

    def test_empty(self):
        h = book_health([])
        self.assertEqual(h, {"green": 0, "underwater": 0, "past_stop_held": 0,
                             "leak_usd": 0.0, "leak_names": []})


class SummaryAndRenderTests(unittest.TestCase):
    def _summary(self):
        positions = [
            {"ticker": "G", "entry_price": 100, "current_gain_pct": 8.0,
             "stop_price": 95, "shares": 10},
            {"ticker": "S", "entry_price": 100, "current_gain_pct": -12.0,
             "stop_price": 92, "shares": 10},
        ]
        history = {
            "X": [
                {"date": "2026-05-01", "action": "BUY", "shares": 10, "price": 10.0},
                {"date": "2026-05-28", "action": "SELL", "shares": 10, "price": 5.0},
            ],
        }
        return build_positioning_summary(
            positions, history, market_state="EXTENDED", etf_regime="late-rotation",
            position_cap=5, week_start="2026-05-25", week_end="2026-05-31",
        )

    def test_summary_over_cap_when_too_many(self):
        positions = [{"ticker": f"T{i}", "entry_price": 100,
                      "current_gain_pct": 1.0, "stop_price": 90, "shares": 1}
                     for i in range(8)]
        s = build_positioning_summary(positions, {}, "GREEN", None,
                                      position_cap=5, week_start="2026-05-25")
        self.assertTrue(s["over_cap"])

    def test_html_contains_key_facts(self):
        html = render_positioning_html(self._summary())
        self.assertIn("Positioning", html)
        self.assertIn("EXTENDED", html)
        self.assertIn("late-rotation", html)
        self.assertIn("past stop", html)
        # leak callout present
        self.assertIn("held past stop", html)
        # light theme — no dark bg hex
        self.assertNotIn("#0f172a", html)
        self.assertNotIn("#1e293b", html)

    def test_slack_contains_key_facts(self):
        txt = render_positioning_slack(self._summary())
        self.assertIn("Positioning & Book Risk", txt)
        self.assertIn("EXTENDED", txt)
        self.assertIn("Realized this week", txt)
        self.assertIn("Leak", txt)

    def test_renderers_empty_on_none(self):
        self.assertEqual(render_positioning_html(None), "")
        self.assertEqual(render_positioning_slack({}), "")


if __name__ == "__main__":
    unittest.main()
