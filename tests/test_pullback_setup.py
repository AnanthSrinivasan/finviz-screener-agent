"""Tests for agents.utils.pullback_setup — 21 EMA pullback classifier."""

import unittest

import pandas as pd

from agents.utils.pullback_setup import (
    build_pullback_rows,
    classify_pullback_setup,
    compute_21ema,
)


class ComputeEma21Tests(unittest.TestCase):
    def test_returns_none_when_under_22_bars(self):
        self.assertIsNone(compute_21ema([100.0] * 21))
        self.assertIsNone(compute_21ema([]))

    def test_constant_series_returns_constant(self):
        ema = compute_21ema([50.0] * 30)
        self.assertIsNotNone(ema)
        self.assertAlmostEqual(ema, 50.0, places=4)

    def test_rising_series_ema_below_last(self):
        closes = [float(i) for i in range(1, 31)]  # 1..30
        ema = compute_21ema(closes)
        self.assertIsNotNone(ema)
        self.assertLess(ema, closes[-1])
        self.assertGreater(ema, closes[0])


class ClassifyPullbackTests(unittest.TestCase):
    def _kw(self, **over):
        base = dict(price=100.0, ema21=100.0, sma50_pct=10.0, peel_warn=8.0,
                    q=85, rs=80, atr_pct=4.0, dist_from_high=-5.0)
        base.update(over)
        return base

    def test_entry_zone_at_ema(self):
        self.assertEqual(classify_pullback_setup(**self._kw()), "entry_zone")
        self.assertEqual(classify_pullback_setup(**self._kw(price=101.4)), "entry_zone")
        self.assertEqual(classify_pullback_setup(**self._kw(price=98.6)), "entry_zone")

    def test_watching_band(self):
        self.assertEqual(classify_pullback_setup(**self._kw(price=103.0)), "watching")
        self.assertEqual(classify_pullback_setup(**self._kw(price=104.0)), "watching")

    def test_mid_flight(self):
        self.assertEqual(classify_pullback_setup(**self._kw(price=110.0)), "mid_flight")

    def test_below_ema_dropped(self):
        self.assertEqual(classify_pullback_setup(**self._kw(price=95.0)), "below_ema")

    def test_quality_skip(self):
        self.assertEqual(classify_pullback_setup(**self._kw(q=70)), "skip")
        self.assertEqual(classify_pullback_setup(**self._kw(rs=50)), "skip")
        self.assertEqual(classify_pullback_setup(**self._kw(atr_pct=8.0)), "skip")
        self.assertEqual(classify_pullback_setup(**self._kw(atr_pct=2.5)), "skip")
        self.assertEqual(classify_pullback_setup(**self._kw(dist_from_high=2.0)), "skip")
        self.assertEqual(classify_pullback_setup(**self._kw(dist_from_high=-15.0)), "skip")

    def test_rs_60_passes(self):
        self.assertEqual(classify_pullback_setup(**self._kw(rs=60)), "entry_zone")
        self.assertEqual(classify_pullback_setup(**self._kw(rs=59)), "skip")

    def test_atr_3_passes(self):
        self.assertEqual(classify_pullback_setup(**self._kw(atr_pct=3.0, sma50_pct=8.0)), "entry_zone")
        self.assertEqual(classify_pullback_setup(**self._kw(atr_pct=2.99)), "skip")

    def test_extended_via_peel(self):
        # peel_mult = sma50_pct / atr_pct = 40/4 = 10x > peel_warn 8x
        self.assertEqual(
            classify_pullback_setup(**self._kw(sma50_pct=40.0, peel_warn=8.0)),
            "extended",
        )

    def test_missing_ema_returns_skip(self):
        self.assertEqual(classify_pullback_setup(**self._kw(ema21=None)), "skip")


class BuildPullbackRowsTests(unittest.TestCase):
    def test_buckets_route_correctly(self):
        persistence = pd.DataFrame([
            {"Ticker": "AAA"},
            {"Ticker": "BBB"},
            {"Ticker": "CCC"},
        ])

        latest = pd.DataFrame([
            {"Ticker": "AAA", "Company": "A Inc", "Sector": "Tech",
             "ATR%": 4.0, "SMA50%": 10.0, "Quality Score": 85,
             "RS Rating": 80, "Dist From High%": -5.0},
            {"Ticker": "BBB", "Company": "B Inc", "Sector": "Tech",
             "ATR%": 4.0, "SMA50%": 10.0, "Quality Score": 85,
             "RS Rating": 80, "Dist From High%": -5.0},
            {"Ticker": "CCC", "Company": "C Inc", "Sector": "Tech",
             "ATR%": 4.0, "SMA50%": 40.0, "Quality Score": 85,
             "RS Rating": 80, "Dist From High%": -5.0},
        ])

        # AAA: price ≈ ema21 → entry_zone
        # BBB: price ≈ +3% above ema21 → watching
        # CCC: peel_mult 10x > warn 8x → extended
        bars_by_ticker = {
            "AAA": [{"c": 100.0}] * 22 + [{"c": 100.5}] * 8,
            "BBB": [{"c": 100.0}] * 29 + [{"c": 103.0}],  # last close 3% above stable EMA
            "CCC": [{"c": 100.0}] * 22 + [{"c": 100.5}] * 8,
        }

        def fake_fetch(ticker, limit):
            return bars_by_ticker.get(ticker, [])

        def fake_peel(atr_pct, ticker):
            return (8.0, "tier")

        out = build_pullback_rows(persistence, latest, fake_fetch, fake_peel)

        entry_tickers = {r["ticker"] for r in out["entry_zone"]}
        watching_tickers = {r["ticker"] for r in out["watching"]}
        extended_tickers = {r["ticker"] for r in out["extended"]}

        self.assertIn("AAA", entry_tickers)
        self.assertIn("BBB", watching_tickers)
        self.assertIn("CCC", extended_tickers)

    def test_empty_persistence_returns_empty_buckets(self):
        out = build_pullback_rows(
            pd.DataFrame(), pd.DataFrame(), lambda t, n: [], lambda a, t: (8.0, "tier"),
        )
        self.assertEqual(out, {"entry_zone": [], "watching": [],
                                "mid_flight": [], "extended": []})


if __name__ == "__main__":
    unittest.main()
