"""
Unit tests for helpers added in the macro heat-map / prev-month / portfolio tab session.

Covers:
- agents.screener.finviz_weekly_agent._heat bin boundaries
- agents.screener.finviz_weekly_agent.fetch_macro_prev_month (mocked yfinance)
- utils.generators.generate_portfolio helpers (_fmt_money, _fmt_pct, _heat_class,
  build_equity_curve_js, generate_html smoke)
"""

import json
import unittest
from unittest.mock import patch, MagicMock

import pandas as pd

from agents.screener.finviz_weekly_agent import _heat, fetch_macro_prev_month
from utils.generators import generate_portfolio as gp


class HeatBinTests(unittest.TestCase):
    def test_strong_positive(self):
        self.assertEqual(_heat("2.0%"), "heat-pos-strong")
        self.assertEqual(_heat("5.4%"), "heat-pos-strong")

    def test_mild_positive(self):
        self.assertEqual(_heat("0.01%"), "heat-pos")
        self.assertEqual(_heat("1.99%"), "heat-pos")

    def test_zero(self):
        self.assertEqual(_heat("0%"), "heat-zero")
        self.assertEqual(_heat("0.0%"), "heat-zero")

    def test_mild_negative(self):
        self.assertEqual(_heat("-0.5%"), "heat-neg")
        self.assertEqual(_heat("-1.99%"), "heat-neg")

    def test_strong_negative(self):
        self.assertEqual(_heat("-2.0%"), "heat-neg-strong")
        self.assertEqual(_heat("-5.4%"), "heat-neg-strong")

    def test_non_numeric(self):
        self.assertEqual(_heat("n/a"), "heat-zero")
        self.assertEqual(_heat(""), "heat-zero")
        self.assertEqual(_heat("-"), "heat-zero")


class PrevMonthTests(unittest.TestCase):
    def test_empty_macro_data_noop(self):
        md = {}
        fetch_macro_prev_month(md)  # must not raise
        self.assertEqual(md, {})

    @patch("yfinance.download")
    def test_single_symbol_populates_prev_month(self, mock_dl):
        # Simulate ~80 trading days of rising closes. t-42 close ~= 100, t-21 close ~= 110.
        # Prev-month return should be +10%.
        dates  = pd.date_range("2026-01-01", periods=80, freq="B")
        closes = pd.Series(range(100, 180), index=dates, dtype=float)
        mock_dl.return_value = pd.DataFrame({"Close": closes})

        md = {"SPY": {"name": "S&P 500"}}
        fetch_macro_prev_month(md)
        self.assertIn("perf_prev_month", md["SPY"])
        v = md["SPY"]["perf_prev_month"]
        self.assertIsNotNone(v)
        self.assertTrue(v.endswith("%"))
        # Directional: closes are rising, prev-month return must be positive
        self.assertTrue(v.startswith("+"), f"expected positive return, got {v}")

    @patch("yfinance.download")
    def test_yfinance_failure_leaves_none(self, mock_dl):
        mock_dl.side_effect = RuntimeError("network down")
        md = {"SPY": {"name": "S&P 500"}}
        fetch_macro_prev_month(md)
        # perf_prev_month should remain absent or None, never raise
        self.assertIsNone(md["SPY"].get("perf_prev_month"))

    @patch("yfinance.download")
    def test_insufficient_history_skips(self, mock_dl):
        # Only 10 days of history — below the 45 trading-day floor
        dates  = pd.date_range("2026-04-01", periods=10, freq="B")
        closes = pd.Series(range(100, 110), index=dates, dtype=float)
        mock_dl.return_value = pd.DataFrame({"Close": closes})

        md = {"SPY": {"name": "S&P 500"}}
        fetch_macro_prev_month(md)
        self.assertIsNone(md["SPY"].get("perf_prev_month"))


class PortfolioHelperTests(unittest.TestCase):
    def test_fmt_money(self):
        self.assertEqual(gp._fmt_money(1234), "$1,234")
        self.assertEqual(gp._fmt_money(-9876), "-$9,876")
        self.assertEqual(gp._fmt_money(0), "$0")

    def test_fmt_pct(self):
        self.assertEqual(gp._fmt_pct(1.23), "+1.23%")
        self.assertEqual(gp._fmt_pct(-4.56), "-4.56%")
        self.assertEqual(gp._fmt_pct(0), "+0.00%")

    def test_heat_class_bins(self):
        self.assertEqual(gp._heat_class(5.0), "heat-pos-strong")
        self.assertEqual(gp._heat_class(10.0), "heat-pos-strong")
        self.assertEqual(gp._heat_class(4.9), "heat-pos")
        self.assertEqual(gp._heat_class(0.01), "heat-pos")
        self.assertEqual(gp._heat_class(0), "heat-zero")
        self.assertEqual(gp._heat_class(-0.5), "heat-neg")
        self.assertEqual(gp._heat_class(-4.99), "heat-neg")
        self.assertEqual(gp._heat_class(-5.0), "heat-neg-strong")
        self.assertEqual(gp._heat_class(-12.0), "heat-neg-strong")

    def test_build_equity_curve_js_empty(self):
        self.assertEqual(gp.build_equity_curve_js({}), "[]")
        self.assertEqual(gp.build_equity_curve_js(None), "[]")

    def test_build_equity_curve_js_valid(self):
        hist = {
            "timestamp": [1704067200, 1704153600],  # 2024-01-01, 2024-01-02
            "equity":    [100000.0, 101234.56],
        }
        out = json.loads(gp.build_equity_curve_js(hist))
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["x"], "2024-01-01")
        self.assertEqual(out[0]["y"], 100000.0)
        self.assertEqual(out[1]["y"], 101234.56)

    def test_build_equity_curve_js_skips_none_equity(self):
        hist = {"timestamp": [1704067200, 1704153600], "equity": [None, 100.0]}
        out = json.loads(gp.build_equity_curve_js(hist))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["y"], 100.0)

    def test_generate_html_smoke(self):
        account = {"equity": "100000", "last_equity": "99500",
                   "cash": "20000", "buying_power": "20000"}
        positions = [{
            "symbol": "NVDA", "qty": "10",
            "avg_entry_price": "100", "current_price": "110",
            "market_value": "1100", "cost_basis": "1000",
            "unrealized_pl": "100", "unrealized_plpc": "0.10",
        }]
        html = gp.generate_html(account, positions, {})
        # Key sections present
        self.assertIn("Claude Model Portfolio", html)
        self.assertIn("Equity Curve", html)
        self.assertIn("NVDA", html)
        # Heat classes applied
        self.assertIn("heat-pos", html)
        # Allocation computed (1100 / 100000 = 1.1%)
        self.assertIn("1.1%", html)
        # Today P&L delta text present (+$500)
        self.assertIn("$500", html)

    def test_generate_html_empty_positions(self):
        account = {"equity": "100000", "last_equity": "100000",
                   "cash": "100000", "buying_power": "100000"}
        html = gp.generate_html(account, [], {})
        self.assertIn("No open positions", html)


if __name__ == "__main__":
    unittest.main()
