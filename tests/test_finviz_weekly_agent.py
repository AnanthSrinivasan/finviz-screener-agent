"""
Unit tests for agents/screener/finviz_weekly_agent.py helpers.

Covers:
- _heat bin boundaries for the Macro Snapshot heat-map
- fetch_macro_prev_month (mocked yfinance) — prior 30-day return enrichment
"""

import unittest
from unittest.mock import patch

import pandas as pd

from agents.screener.finviz_weekly_agent import _heat, fetch_macro_prev_month


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
        fetch_macro_prev_month(md)
        self.assertEqual(md, {})

    @patch("yfinance.download")
    def test_single_symbol_populates_prev_month(self, mock_dl):
        dates  = pd.date_range("2026-01-01", periods=80, freq="B")
        closes = pd.Series(range(100, 180), index=dates, dtype=float)
        mock_dl.return_value = pd.DataFrame({"Close": closes})

        md = {"SPY": {"name": "S&P 500"}}
        fetch_macro_prev_month(md)
        self.assertIn("perf_prev_month", md["SPY"])
        v = md["SPY"]["perf_prev_month"]
        self.assertIsNotNone(v)
        self.assertTrue(v.endswith("%"))
        self.assertTrue(v.startswith("+"), f"expected positive return, got {v}")

    @patch("yfinance.download")
    def test_yfinance_failure_leaves_none(self, mock_dl):
        mock_dl.side_effect = RuntimeError("network down")
        md = {"SPY": {"name": "S&P 500"}}
        fetch_macro_prev_month(md)
        self.assertIsNone(md["SPY"].get("perf_prev_month"))

    @patch("yfinance.download")
    def test_insufficient_history_skips(self, mock_dl):
        dates  = pd.date_range("2026-04-01", periods=10, freq="B")
        closes = pd.Series(range(100, 110), index=dates, dtype=float)
        mock_dl.return_value = pd.DataFrame({"Close": closes})

        md = {"SPY": {"name": "S&P 500"}}
        fetch_macro_prev_month(md)
        self.assertIsNone(md["SPY"].get("perf_prev_month"))


if __name__ == "__main__":
    unittest.main()
