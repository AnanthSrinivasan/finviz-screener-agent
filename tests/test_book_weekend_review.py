"""Tests for agents.utils.book_weekend_review — Weekly Review §3."""

import unittest

from agents.utils.book_weekend_review import (
    build_book_review_rows,
    render_book_review_html,
    render_book_review_slack,
    _verdict_rank,
)


class BuildRowsTests(unittest.TestCase):
    def test_basic_row_fields(self):
        positions = [{
            "ticker": "AAA", "entry_price": 100, "current_gain_pct": 8.0,
            "peak_gain_pct": 15.0, "stop_price": 102,
        }]
        rows = build_book_review_rows(positions)
        r = rows[0]
        self.assertEqual(r["ticker"], "AAA")
        self.assertEqual(r["gain_pct"], 8.0)
        self.assertEqual(r["peak_pct"], 15.0)
        # current price = 100 * 1.08 = 108; dist to stop = (108-102)/108
        self.assertAlmostEqual(r["dist_to_stop_pct"], 5.6, places=1)
        self.assertIsInstance(r["verdict"], str)

    def test_action_first_sort(self):
        positions = [
            {"ticker": "HOLD", "entry_price": 100, "current_gain_pct": 5.0,
             "peak_gain_pct": 6.0, "stop_price": 96},
            {"ticker": "CUT", "entry_price": 100, "current_gain_pct": -8.0,
             "peak_gain_pct": 2.0, "stop_price": 90},
            {"ticker": "PEEL", "entry_price": 100, "current_gain_pct": 25.0,
             "peak_gain_pct": 30.0, "stop_price": 110},
        ]
        rows = build_book_review_rows(positions)
        order = [r["ticker"] for r in rows]
        # CUT (gain -8 → cut) first, PEEL (>=20 → peel) second, HOLD last
        self.assertEqual(order[0], "CUT")
        self.assertEqual(order[-1], "HOLD")

    def test_tech_lookup_feeds_verdict(self):
        positions = [{"ticker": "X", "entry_price": 100, "current_gain_pct": 8.0,
                      "peak_gain_pct": 9.0, "stop_price": 99}]
        rows = build_book_review_rows(
            positions, tech_lookup=lambda t: {"atr": 9.0, "s20": 25.0, "stage": "3"})
        # s20 > 20 and stage 3 should annotate the verdict
        self.assertIn("ext", rows[0]["verdict"].lower())
        self.assertIn("3", rows[0]["verdict"])

    def test_no_stop_price(self):
        rows = build_book_review_rows([
            {"ticker": "N", "entry_price": 100, "current_gain_pct": 3.0,
             "peak_gain_pct": 4.0}])
        self.assertIsNone(rows[0]["dist_to_stop_pct"])

    def test_empty(self):
        self.assertEqual(build_book_review_rows([]), [])


class VerdictRankTests(unittest.TestCase):
    def test_cut_most_urgent(self):
        self.assertLess(_verdict_rank("🚨 CUT — past stop zone"),
                        _verdict_rank("✅ working, hold"))

    def test_trim_before_trail(self):
        self.assertLess(_verdict_rank("💰 PEEL ½ (T1 rule)"),
                        _verdict_rank("🟢 trail tighter"))


class RenderTests(unittest.TestCase):
    def _rows(self):
        return build_book_review_rows([
            {"ticker": "AAA", "entry_price": 100, "current_gain_pct": 8.0,
             "peak_gain_pct": 15.0, "stop_price": 102},
            {"ticker": "BBB", "entry_price": 50, "current_gain_pct": -8.0,
             "peak_gain_pct": 1.0, "stop_price": 47},
        ])

    def test_html(self):
        html = render_book_review_html(self._rows())
        self.assertIn("Book Weekend Review", html)
        self.assertIn("AAA", html)
        self.assertIn("Verdict", html)
        self.assertNotIn("#0f172a", html)  # light theme

    def test_html_empty(self):
        self.assertIn("flat book", render_book_review_html([]))

    def test_slack(self):
        txt = render_book_review_slack(self._rows())
        self.assertIn("Book Weekend Review", txt)
        self.assertIn("to stop", txt)

    def test_slack_truncation(self):
        many = build_book_review_rows([
            {"ticker": f"T{i}", "entry_price": 100, "current_gain_pct": 1.0,
             "peak_gain_pct": 2.0, "stop_price": 95} for i in range(15)])
        txt = render_book_review_slack(many, max_rows=5)
        self.assertIn("more", txt)

    def test_slack_empty(self):
        self.assertIn("No open positions", render_book_review_slack([]))


if __name__ == "__main__":
    unittest.main()
