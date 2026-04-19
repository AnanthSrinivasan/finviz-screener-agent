"""
Unit tests for utils/generators/generate_portfolio.py — the Claude Model
Portfolio HTML builder that runs inside alpaca_monitor.py.
"""

import json
import unittest

from utils.generators import generate_portfolio as gp


class FormatterTests(unittest.TestCase):
    def test_fmt_money(self):
        self.assertEqual(gp._fmt_money(1234), "$1,234")
        self.assertEqual(gp._fmt_money(-9876), "-$9,876")
        self.assertEqual(gp._fmt_money(0), "$0")

    def test_fmt_pct(self):
        self.assertEqual(gp._fmt_pct(1.23), "+1.23%")
        self.assertEqual(gp._fmt_pct(-4.56), "-4.56%")
        self.assertEqual(gp._fmt_pct(0), "+0.00%")


class HeatClassTests(unittest.TestCase):
    def test_bins(self):
        self.assertEqual(gp._heat_class(5.0), "heat-pos-strong")
        self.assertEqual(gp._heat_class(10.0), "heat-pos-strong")
        self.assertEqual(gp._heat_class(4.9), "heat-pos")
        self.assertEqual(gp._heat_class(0.01), "heat-pos")
        self.assertEqual(gp._heat_class(0), "heat-zero")
        self.assertEqual(gp._heat_class(-0.5), "heat-neg")
        self.assertEqual(gp._heat_class(-4.99), "heat-neg")
        self.assertEqual(gp._heat_class(-5.0), "heat-neg-strong")
        self.assertEqual(gp._heat_class(-12.0), "heat-neg-strong")


class EquityCurveTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(gp.build_equity_curve_js({}), "[]")
        self.assertEqual(gp.build_equity_curve_js(None), "[]")

    def test_valid(self):
        hist = {
            "timestamp": [1704067200, 1704153600],  # 2024-01-01, 2024-01-02
            "equity":    [100000.0, 101234.56],
        }
        out = json.loads(gp.build_equity_curve_js(hist))
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["x"], "2024-01-01")
        self.assertEqual(out[0]["y"], 100000.0)
        self.assertEqual(out[1]["y"], 101234.56)

    def test_skips_none_equity(self):
        hist = {"timestamp": [1704067200, 1704153600], "equity": [None, 100.0]}
        out = json.loads(gp.build_equity_curve_js(hist))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["y"], 100.0)


class GenerateHtmlTests(unittest.TestCase):
    def test_smoke_with_position(self):
        account = {"equity": "100000", "last_equity": "99500",
                   "cash": "20000", "buying_power": "20000"}
        positions = [{
            "symbol": "NVDA", "qty": "10",
            "avg_entry_price": "100", "current_price": "110",
            "market_value": "1100", "cost_basis": "1000",
            "unrealized_pl": "100", "unrealized_plpc": "0.10",
        }]
        html = gp.generate_html(account, positions, {})
        self.assertIn("Claude Model Portfolio", html)
        self.assertIn("Equity Curve", html)
        self.assertIn("NVDA", html)
        self.assertIn("heat-pos", html)
        self.assertIn("1.1%", html)
        self.assertIn("$500", html)

    def test_empty_positions(self):
        account = {"equity": "100000", "last_equity": "100000",
                   "cash": "100000", "buying_power": "100000"}
        html = gp.generate_html(account, [], {})
        self.assertIn("No open positions", html)


if __name__ == "__main__":
    unittest.main()
