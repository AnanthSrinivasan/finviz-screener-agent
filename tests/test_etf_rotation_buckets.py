"""Unit tests for ETF rotation bucket assignment + setup metrics + HTML render."""

import unittest

import pandas as pd

from agents.sector_rotation import (
    assign_bucket,
    compute_etf_setup,
    compute_etf_setups,
    render_etf_rotation_html,
)


def _metrics(**over):
    """Default metric dict — passes BASE."""
    base = {
        "last":        100.0,
        "atr_pct":     2.5,
        "mult50":      1.5,
        "pct50":       3.0,
        "dist52":      -5.0,
        "s50_rising":  True,
        "s200_rising": True,
        "range20":     6.0,
        "ret20":       2.0,
        "ema21d":      0.5,
        "rvol":        1.0,
    }
    base.update(over)
    return base


class TestAssignBucket(unittest.TestCase):
    def test_base_default(self):
        self.assertEqual(assign_bucket(_metrics()), "BASE")

    def test_extended_high_mult(self):
        self.assertEqual(assign_bucket(_metrics(mult50=6.0)), "EXTENDED")

    def test_extended_at_high(self):
        self.assertEqual(assign_bucket(_metrics(dist52=-1.0)), "EXTENDED")

    def test_broken_neg_mult(self):
        self.assertEqual(assign_bucket(_metrics(mult50=-2.0)), "BROKEN")

    def test_broken_200_falling(self):
        self.assertEqual(assign_bucket(_metrics(s200_rising=False)), "BROKEN")

    def test_pre_breakout_when_range_too_wide_for_base(self):
        # range20 = 13 (>12) so not BASE; mult50 < 4 and dist between -10..0 → PRE-BREAKOUT
        self.assertEqual(assign_bucket(_metrics(range20=13.0)), "PRE-BREAKOUT")

    def test_pre_breakout_near_high(self):
        # dist -1.5 — but rule has dist >-2 → EXTENDED; switch to -2.5 to bypass
        # Then with mult50 < 4, in range [-10,0] → either BASE or PRE-BREAKOUT
        self.assertEqual(assign_bucket(_metrics(dist52=-2.5, range20=14.0)), "PRE-BREAKOUT")

    def test_neutral_when_50_falling(self):
        # s50_rising False, s200 rising True, mult50 small positive
        m = _metrics(s50_rising=False)
        self.assertEqual(assign_bucket(m), "NEUTRAL")

    def test_base_boundary_range_below_12(self):
        self.assertEqual(assign_bucket(_metrics(range20=11.9)), "BASE")

    def test_extended_boundary_mult_above_5(self):
        self.assertEqual(assign_bucket(_metrics(mult50=5.01)), "EXTENDED")


class TestComputeEtfSetup(unittest.TestCase):
    def test_returns_none_for_insufficient_bars(self):
        df = pd.DataFrame([{"c": 100, "h": 101, "l": 99, "v": 1000}] * 50)
        self.assertIsNone(compute_etf_setup(df))

    def test_returns_metrics_for_uptrending_data(self):
        # 220-day synthetic uptrend
        rows = []
        for i in range(220):
            c = 50 + i * 0.5  # linearly rising
            rows.append({"c": c, "h": c + 0.5, "l": c - 0.5, "v": 1000 + i})
        df = pd.DataFrame(rows)
        m = compute_etf_setup(df)
        self.assertIsNotNone(m)
        self.assertGreater(m["pct50"], 0)
        self.assertTrue(m["s50_rising"])
        self.assertTrue(m["s200_rising"])


class TestComputeEtfSetups(unittest.TestCase):
    def test_assigns_kind_correctly(self):
        # synthetic universe with one sector + one thematic
        universe = {
            "sectors":   {"XLK": {"name": "Tech",  "theme": "growth"}},
            "thematics": {"SMH": {"name": "Semis", "theme": "growth-narrow"}},
        }
        rows = [{"c": 50 + i*0.3, "h": 51 + i*0.3, "l": 49 + i*0.3, "v": 1000} for i in range(220)]
        bars = {"XLK": pd.DataFrame(rows), "SMH": pd.DataFrame(rows)}
        setups = compute_etf_setups(bars, universe)
        kinds = {s["ticker"]: s["kind"] for s in setups}
        self.assertEqual(kinds["XLK"], "sector")
        self.assertEqual(kinds["SMH"], "thematic")

    def test_handles_missing_bars_gracefully(self):
        universe = {"sectors": {"XLK": {"name": "Tech", "theme": "growth"}},
                    "thematics": {}}
        # only 50 bars — insufficient
        bars = {"XLK": pd.DataFrame([{"c": 100, "h": 101, "l": 99, "v": 1000}] * 50)}
        setups = compute_etf_setups(bars, universe)
        self.assertEqual(len(setups), 1)
        self.assertIsNone(setups[0]["metrics"])
        self.assertEqual(setups[0]["bucket"], "NEUTRAL")


class TestRenderHtml(unittest.TestCase):
    def test_render_returns_html_with_sections(self):
        snapshot = {"date": "2026-05-17", "regime": "mid-rotation"}
        setups = [
            {"ticker": "XLRE", "name": "Real Estate", "theme": "rate-sensitive",
             "kind": "sector", "bucket": "BASE", "metrics": _metrics()},
            {"ticker": "XLK", "name": "Technology", "theme": "growth",
             "kind": "sector", "bucket": "EXTENDED", "metrics": _metrics(mult50=8.0, dist52=-1.0)},
            {"ticker": "GDX", "name": "Gold Miners", "theme": "precious-metal",
             "kind": "thematic", "bucket": "BROKEN", "metrics": _metrics(mult50=-2.0, s200_rising=False)},
        ]
        html = render_etf_rotation_html(snapshot, setups)
        self.assertIn("ETF Rotation Dashboard", html)
        self.assertIn("XLRE", html)
        self.assertIn("XLK", html)
        self.assertIn("GDX", html)
        # New layout: bucket counts strip + sortable full table (no per-bucket card sections)
        self.assertIn("Bucket counts", html)
        self.assertIn("BASE", html)
        self.assertIn("EXTENDED", html)
        self.assertIn("Full metrics", html)
        self.assertIn("mid-rotation", html)

    def test_render_handles_empty_buckets(self):
        snapshot = {"date": "2026-05-17", "regime": "early-rotation"}
        html = render_etf_rotation_html(snapshot, [])
        self.assertIn("ETF Rotation Dashboard", html)
        # Empty universe still renders the bucket-counts strip with zeros
        self.assertIn("BASE 0", html)
        self.assertIn("Full metrics — all 0 ETFs", html)


if __name__ == "__main__":
    unittest.main()
