"""Unit tests for _compute_rs_ratings (Phase 2 RS Rating)."""

import unittest

import pandas as pd

from agents.screener.finviz_agent import _compute_rs_ratings


def _df(*rows):
    return pd.DataFrame(rows)


class TestComputeRsRatings(unittest.TestCase):

    def test_rank_ordering(self):
        """Stronger 3m/6m/12m performance → higher RS Rating."""
        df = _df(
            {"Ticker": "STRONG", "Perf Quarter": 30.0, "Perf Half Y": 50.0, "Perf Year": 80.0},
            {"Ticker": "MID",    "Perf Quarter": 10.0, "Perf Half Y": 20.0, "Perf Year": 30.0},
            {"Ticker": "WEAK",   "Perf Quarter": -5.0, "Perf Half Y": -10.0, "Perf Year": -20.0},
        )
        ratings = _compute_rs_ratings(df)
        self.assertGreater(ratings["STRONG"], ratings["MID"])
        self.assertGreater(ratings["MID"],    ratings["WEAK"])

    def test_range_0_to_99(self):
        """All ratings must be in [0, 99]."""
        df = _df(
            {"Ticker": "A", "Perf Quarter": 5.0,  "Perf Half Y": 10.0, "Perf Year": 15.0},
            {"Ticker": "B", "Perf Quarter": -2.0, "Perf Half Y": -5.0, "Perf Year": -8.0},
            {"Ticker": "C", "Perf Quarter": 20.0, "Perf Half Y": 40.0, "Perf Year": 60.0},
        )
        ratings = _compute_rs_ratings(df)
        for t, r in ratings.items():
            self.assertGreaterEqual(r, 0,  f"{t} rating {r} below 0")
            self.assertLessEqual(r,   99, f"{t} rating {r} above 99")

    def test_single_ticker_gets_99(self):
        """Single ticker in universe always gets rating 99 (top of 1)."""
        df = _df({"Ticker": "SOLO", "Perf Quarter": 10.0, "Perf Half Y": 20.0, "Perf Year": 30.0})
        ratings = _compute_rs_ratings(df)
        self.assertEqual(ratings["SOLO"], 99)

    def test_all_same_composite_spread_across_rank(self):
        """Tickers with identical composite → lowest gets 0, highest gets 99."""
        df = _df(
            {"Ticker": "X", "Perf Quarter": 10.0, "Perf Half Y": 10.0, "Perf Year": 10.0},
            {"Ticker": "Y", "Perf Quarter": 10.0, "Perf Half Y": 10.0, "Perf Year": 10.0},
            {"Ticker": "Z", "Perf Quarter": 10.0, "Perf Half Y": 10.0, "Perf Year": 10.0},
        )
        ratings = _compute_rs_ratings(df)
        # All equal → ranks evenly spaced; last sorted == 99, first == 0
        self.assertEqual(min(ratings.values()), 0)
        self.assertEqual(max(ratings.values()), 99)

    def test_missing_perf_fields_default_to_zero(self):
        """NaN / None perf fields treated as 0 — ticker ranks at bottom."""
        df = _df(
            {"Ticker": "GOOD",    "Perf Quarter": 20.0, "Perf Half Y": 40.0, "Perf Year": 60.0},
            {"Ticker": "MISSING", "Perf Quarter": None,  "Perf Half Y": None,  "Perf Year": None},
        )
        ratings = _compute_rs_ratings(df)
        self.assertGreater(ratings["GOOD"], ratings["MISSING"])
        self.assertEqual(ratings["MISSING"], 0)

    def test_empty_df_returns_empty_dict(self):
        ratings = _compute_rs_ratings(pd.DataFrame())
        self.assertEqual(ratings, {})

    def test_nine_month_approximation_weight(self):
        """
        Formula: composite = p3×0.4 + p6×0.3 + p12×0.3  (9m approx folds into 6m+12m).
        A ticker with only high p3 should outscore one with only high p12.
        """
        df = _df(
            {"Ticker": "MOMENTUM", "Perf Quarter": 50.0, "Perf Half Y": 0.0,  "Perf Year": 0.0},
            {"Ticker": "LONGTERM", "Perf Quarter": 0.0,  "Perf Half Y": 0.0,  "Perf Year": 50.0},
        )
        ratings = _compute_rs_ratings(df)
        # MOMENTUM: 50×0.4=20  LONGTERM: 50×0.3=15  → MOMENTUM > LONGTERM
        self.assertGreater(ratings["MOMENTUM"], ratings["LONGTERM"])

    def test_returns_integers(self):
        """All rating values must be integers."""
        df = _df(
            {"Ticker": "A", "Perf Quarter": 5.5,  "Perf Half Y": 11.1, "Perf Year": 22.2},
            {"Ticker": "B", "Perf Quarter": -3.3, "Perf Half Y": -7.7, "Perf Year": -9.9},
        )
        ratings = _compute_rs_ratings(df)
        for t, r in ratings.items():
            self.assertIsInstance(r, int, f"{t} rating is not int: {type(r)}")
