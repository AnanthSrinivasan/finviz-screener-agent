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
        self.assertIn("dead", glp.verdict_for(0.3, 5.0, 5.0, "2P").lower())

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
        # light theme markers
        self.assertIn("#f8f9fc", html)  # background
        self.assertIn("#111827", html)  # text
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


if __name__ == "__main__":
    unittest.main()
