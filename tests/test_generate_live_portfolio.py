"""
Smoke tests for utils/generators/generate_live_portfolio.py.
Mock SnapTrade + Finviz so we don't hit the network.
"""

import os
import tempfile
import unittest
from unittest import mock

from utils.generators import generate_live_portfolio as glp


class VerdictTests(unittest.TestCase):
    def test_cut_zone(self):
        self.assertIn("CUT", glp.verdict_for(-6.0, 5.0, 5.0, "2P"))

    def test_peel_at_20(self):
        self.assertIn("PEEL", glp.verdict_for(22.0, 5.0, 5.0, "2P"))

    def test_high_vol_extended_peel(self):
        self.assertIn("peel", glp.verdict_for(8.0, 8.5, 5.0, "2P").lower())

    def test_dead_weight(self):
        self.assertIn("dead", glp.verdict_for(0.3, 5.0, 5.0, "2P", held=5).lower())

    def test_new_buy_not_dead_weight(self):
        self.assertNotIn("dead", glp.verdict_for(0.3, 5.0, 5.0, "2P", held=0).lower())

    def test_off_stage_annotation(self):
        v = glp.verdict_for(6.0, 5.0, 5.0, "3")
        self.assertIn("⚠ 3", v)


class RenderHtmlTests(unittest.TestCase):
    def test_renders_ticker_rows_and_verdicts(self):
        account = {"equity": 100000.0, "cash": -5000.0, "buying_power": 80000.0}
        rows = [
            {"ticker": "AAA", "shares": 100, "avg": 50.0, "live": 60.0,
             "gain": 20.0, "pl": 1000.0, "mv": 6000.0,
             "atr": 4.0, "s20": 3.0, "stage": "2P"},
            {"ticker": "BBB", "shares": 50, "avg": 100.0, "live": 90.0,
             "gain": -10.0, "pl": -500.0, "mv": 4500.0,
             "atr": 6.0, "s20": 1.0, "stage": "2"},
        ]
        html = glp.render_html(account, rows)
        self.assertIn("AAA", html)
        self.assertIn("BBB", html)
        self.assertIn("PEEL", html)  # +20% gain → peel verdict
        self.assertIn("CUT", html)   # -10% gain → cut verdict
        # one-design-system markers (theme.BASE_CSS — cx-rehaul §4)
        self.assertIn("BASE_CSS v1", html)
        self.assertIn("--bg:#0b1220", html)
        # SnapTrade equity rendered
        self.assertIn("$100,000", html)
        # leverage computed from negative cash
        self.assertIn("5%", html)


class WritePageTests(unittest.TestCase):
    def test_write_page_writes_html_with_mocked_fetch(self):
        positions = [
            {"ticker": "AAA", "shares": 100, "avg_cost": 50.0, "current_price": 60.0},
        ]
        account = {"equity": 50000.0, "cash": 1000.0, "buying_power": 49000.0}
        with tempfile.TemporaryDirectory() as tmp:
            glp.DATA_DIR    = tmp
            glp.OUTPUT_PATH = os.path.join(tmp, "live_portfolio.html")
            with mock.patch.object(glp, "fetch_live_price", return_value=60.0), \
                 mock.patch.object(glp, "_technicals", return_value={
                     "atr": 4.0, "s20": 3.0, "s50": 2.0, "dist52": -1.0,
                     "rvol": 1.0, "stage": "2P"}), \
                 mock.patch("agents.trading.position_monitor.fetch_positions",
                            return_value=positions), \
                 mock.patch.object(glp, "_fetch_account_balances",
                                   return_value=account):
                path = glp.write_page()
            self.assertTrue(os.path.exists(path))
            with open(path) as f:
                html = f.read()
            self.assertIn("AAA", html)
            self.assertIn("Live SnapTrade Portfolio", html)


class ClassifyActionTests(unittest.TestCase):
    def test_cut(self):
        self.assertEqual(glp.classify_action(-6.0, 3.0), "cut")

    def test_peel_at_20(self):
        self.assertEqual(glp.classify_action(22.0, 3.0), "peel")

    def test_peel_high_vol_extended(self):
        # gain in [7,10) with high vol → peel ⅓
        self.assertEqual(glp.classify_action(8.0, 8.0), "peel")

    def test_gain_over_10_is_trail_not_peel(self):
        # mirrors verdict_for: 🟢 trail tighter is checked before the peel clause
        self.assertEqual(glp.classify_action(11.0, 8.0), "trail")

    def test_dead_weight(self):
        self.assertEqual(glp.classify_action(0.4, 3.0, held=5), "dead")
        self.assertEqual(glp.classify_action(0.4, 3.0, held=0), "hold")

    def test_hold(self):
        self.assertEqual(glp.classify_action(5.0, 3.0), "hold")


class SummaryConsistencyTests(unittest.TestCase):
    """The action summary counts must equal what the rows are tagged with."""

    def _row(self, ticker, gain, atr=3.0, mv=1000.0):
        return {"ticker": ticker, "shares": 100, "avg": 10.0, "live": 11.0,
                "gain": gain, "pl": 100.0, "mv": mv, "entry_date": "2026-06-01",
                "held": "18d", "atr": atr, "s20": 1.0, "stage": "2P"}

    def test_counts_match_row_tags(self):
        rows = [self._row("PEELX", 25), self._row("CUTX", -6),
                self._row("DEADX", 0.4), self._row("HOLDX", 5)]
        html = glp.render_html({"equity": 100000, "cash": 0, "buying_power": 0}, rows)
        self.assertIn("PEEL: 1", html)
        self.assertIn("CUT: 1", html)
        self.assertIn("dead weight: 1", html)
        self.assertEqual(html.count("data-action='peel'"), 1)
        self.assertEqual(html.count("data-action='cut'"), 1)
        self.assertEqual(html.count("data-action='dead'"), 1)

    def test_high_gain_high_vol_is_trail_not_peel(self):
        # DAVE-class: +11% with high ATR must show "trail tighter" and NOT be
        # counted as a peel candidate (the bug the summary used to have).
        rows = [self._row("DAVE", 11.0, atr=8.0)]
        html = glp.render_html({"equity": 100000, "cash": 0, "buying_power": 0}, rows)
        self.assertIn("PEEL: 0", html)
        self.assertIn("data-action='trail'", html)
        self.assertIn("trail tighter", html)

    def test_entry_date_and_legend_present(self):
        rows = [self._row("AAA", 5)]
        html = glp.render_html({"equity": 100000, "cash": 0, "buying_power": 0}, rows)
        self.assertIn("2026-06-01", html)
        self.assertIn("18d", html)
        self.assertIn("Price vs its 20-day moving average", html)  # tooltip carries the definition (legend is a one-liner now)
        self.assertIn("filterAction", html)


class HeldDaysTests(unittest.TestCase):
    def test_none_returns_dash(self):
        self.assertEqual(glp._held_days(None), "—")

    def test_bad_date_returns_dash(self):
        self.assertEqual(glp._held_days("not-a-date"), "—")


if __name__ == "__main__":
    unittest.main()
