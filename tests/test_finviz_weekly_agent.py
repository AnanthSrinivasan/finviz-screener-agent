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


class EmergingCandidatesTests(unittest.TestCase):
    """select_emerging_candidates surfaces names setting up to break out next
    week — Stage 2 + Q≥70 + a fresh catalyst, NOT in current Top 5 or held."""

    def _df(self, rows):
        return pd.DataFrame(rows).sort_values("Signal Score", ascending=False)

    def _row(self, ticker="X", signal=55, q=80, stage="Stage 2 perfect",
             ep=False, ipo=False, multi=False, high=False, cc_watch=False,
             watch=False, days=1):
        return {
            "Ticker": ticker, "Company": ticker + " Inc", "Sector": "Tech",
            "Industry": "Software", "Market Cap": "1B", "Days Seen": days,
            "Total Days": 5, "Dates": "2026-04-25",
            "Max ATR%": 4.0, "Max EPS%": 50.0, "Max Appearances": 2,
            "Screeners Hit": "screen-a", "Base Score": 50.0,
            "Signal Score": signal, "Q Rank": q, "Stage": stage,
            "Quality Mod": 0, "Watch": watch,
            "EP": ep, "IPO": ipo, "MULTI": multi, "HIGH": high, "CHAR": False,
            "CC_DEEP": False, "CC_WATCH": cc_watch,
        }

    def test_excludes_top5(self):
        from agents.screener.finviz_weekly_agent import select_emerging_candidates
        df = self._df([
            self._row(ticker="A", signal=99, ep=True),
            self._row(ticker="B", signal=90, ep=True),
            self._row(ticker="C", signal=85, ep=True),
            self._row(ticker="D", signal=80, ep=True),
            self._row(ticker="E", signal=75, ep=True),
            self._row(ticker="F", signal=60, ep=True),
        ])
        out = select_emerging_candidates(df)
        self.assertNotIn("A", out["Ticker"].tolist())
        self.assertIn("F", out["Ticker"].tolist())

    def test_requires_stage_2(self):
        from agents.screener.finviz_weekly_agent import select_emerging_candidates
        df = self._df([self._row(ticker=f"T{i}", signal=99 - i, ep=True) for i in range(5)]
                      + [self._row(ticker="X1", signal=50, ep=True, stage="Stage 1")])
        out = select_emerging_candidates(df)
        self.assertNotIn("X1", out["Ticker"].tolist())

    def test_requires_q_rank_70(self):
        from agents.screener.finviz_weekly_agent import select_emerging_candidates
        df = self._df([self._row(ticker=f"T{i}", signal=99 - i, ep=True) for i in range(5)]
                      + [self._row(ticker="X1", signal=50, ep=True, q=65)])
        out = select_emerging_candidates(df)
        self.assertNotIn("X1", out["Ticker"].tolist())

    def test_requires_catalyst(self):
        from agents.screener.finviz_weekly_agent import select_emerging_candidates
        df = self._df([self._row(ticker=f"T{i}", signal=99 - i, ep=True) for i in range(5)]
                      + [self._row(ticker="X1", signal=50)])
        out = select_emerging_candidates(df)
        self.assertNotIn("X1", out["Ticker"].tolist())

    def test_excludes_held_positions(self):
        from agents.screener.finviz_weekly_agent import select_emerging_candidates
        df = self._df([self._row(ticker=f"T{i}", signal=99 - i, ep=True) for i in range(5)]
                      + [self._row(ticker="HELD",  signal=60, ep=True),
                         self._row(ticker="FRESH", signal=55, ep=True)])
        out = select_emerging_candidates(df, excluded_tickers={"HELD"})
        self.assertNotIn("HELD", out["Ticker"].tolist())
        self.assertIn("FRESH", out["Ticker"].tolist())

    def test_cc_watch_outranks_ep_at_same_q(self):
        from agents.screener.finviz_weekly_agent import select_emerging_candidates
        df = self._df([self._row(ticker=f"T{i}", signal=99 - i, ep=True) for i in range(5)]
                      + [self._row(ticker="EP_ONLY",       signal=55, ep=True,       q=80),
                         self._row(ticker="CC_WATCH_ONLY", signal=50, cc_watch=True, q=80)])
        out = select_emerging_candidates(df)
        tickers = out["Ticker"].tolist()
        self.assertIn("EP_ONLY", tickers)
        self.assertIn("CC_WATCH_ONLY", tickers)
        self.assertLess(tickers.index("CC_WATCH_ONLY"), tickers.index("EP_ONLY"))


if __name__ == "__main__":
    unittest.main()
