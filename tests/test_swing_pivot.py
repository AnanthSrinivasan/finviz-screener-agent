"""Unit tests for agents.utils.swing_pivot."""

import unittest
from unittest.mock import patch, MagicMock

from agents.utils import swing_pivot


def _bar(t, h, c=None):
    return {"t": t, "h": h, "l": h * 0.95, "o": h * 0.98, "c": c if c is not None else h * 0.99, "v": 1000}


class TestComputeFromRows(unittest.TestCase):
    def test_empty_rows_returns_none(self):
        self.assertIsNone(swing_pivot._compute_from_rows([], days=90, exclude_last=5))

    def test_too_few_rows_returns_none(self):
        rows = [_bar(f"2026-04-{i:02d}T00:00:00Z", 10.0) for i in range(1, 6)]
        self.assertIsNone(swing_pivot._compute_from_rows(rows, days=90, exclude_last=5))

    def test_swing_high_excludes_last_n_days(self):
        # Build 30 bars: pivot=20 on day 5, today's high=50 on day 30.
        # exclude_last=5 → swing_high should be 20, NOT 50 (last 5 excluded).
        rows = [_bar(f"2026-04-{i:02d}T00:00:00Z", 10.0) for i in range(1, 31)]
        rows[4]["h"] = 20.0  # day 5 = pivot
        for i in range(25, 30):
            rows[i]["h"] = 50.0  # last 5 bars huge run-up
        rows[-1]["c"] = 19.0  # last close just below pivot
        res = swing_pivot._compute_from_rows(rows, days=90, exclude_last=5)
        self.assertIsNotNone(res)
        self.assertEqual(res["swing_high"], 20.0)
        self.assertAlmostEqual(res["dist_from_swing_high_pct"], (19.0 - 20.0) / 20.0 * 100.0, places=4)

    def test_dist_pct_positive_when_above_swing(self):
        rows = [_bar(f"2026-04-{i:02d}T00:00:00Z", 10.0) for i in range(1, 21)]
        rows[5]["h"] = 100.0  # pivot
        rows[-1]["c"] = 105.0  # current above pivot
        res = swing_pivot._compute_from_rows(rows, days=90, exclude_last=5)
        self.assertIsNotNone(res)
        self.assertEqual(res["swing_high"], 100.0)
        self.assertAlmostEqual(res["dist_from_swing_high_pct"], 5.0, places=4)


class TestFetchSwingPivotsBatch(unittest.TestCase):
    def test_skips_hyphen_tickers(self):
        with patch.dict("os.environ", {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s"}):
            with patch("agents.utils.swing_pivot.requests.get") as mock_get:
                mock_get.return_value = MagicMock(status_code=200, json=lambda: {"bars": {}})
                res = swing_pivot.fetch_swing_pivots_batch(["BF-B", "BRK.B"])
                self.assertEqual(res, {})
                # Hyphen + dot tickers filtered out → no API call needed
                self.assertFalse(mock_get.called)

    def test_returns_empty_when_no_creds(self):
        with patch.dict("os.environ", {}, clear=True):
            res = swing_pivot.fetch_swing_pivots_batch(["AAPL"])
            self.assertEqual(res, {})

    def test_handles_http_error(self):
        with patch.dict("os.environ", {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s"}):
            with patch("agents.utils.swing_pivot.requests.get") as mock_get:
                mock_get.return_value = MagicMock(status_code=500, text="err")
                res = swing_pivot.fetch_swing_pivots_batch(["AAPL"])
                self.assertEqual(res, {})


class TestComputeSwingPivotSingle(unittest.TestCase):
    def test_hyphen_ticker_returns_none(self):
        self.assertIsNone(swing_pivot.compute_swing_pivot("BF-B"))

    def test_empty_ticker_returns_none(self):
        self.assertIsNone(swing_pivot.compute_swing_pivot(""))


if __name__ == "__main__":
    unittest.main()
