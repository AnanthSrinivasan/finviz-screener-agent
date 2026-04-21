"""
Unit tests for sector rotation helpers in agents/screener/finviz_agent.py.

Covers:
  - compute_sector_rotation: volume × quality × Stage 2 bonus scoring
  - compute_rotating_in: avg_q ranking with count floor (surfaces high-Q
    emerging clusters the volume-weighted view hides, e.g. Basic Materials
    Q90 ranking above Technology Q67 despite fewer tickers)
  - _sector_slug: stable HTML data-attribute slug used to wire click filters
"""

import unittest

import pandas as pd

from agents.screener.finviz_agent import (
    _sector_slug,
    compute_rotating_in,
    compute_sector_rotation,
)


def _row(ticker, sector, qs, stage=2, vcp=False, eps=None):
    return {
        "Ticker": ticker,
        "Sector": sector,
        "Quality Score": qs,
        "Stage": {"stage": stage},
        "VCP": {"vcp_possible": vcp},
        "EPS Y/Y TTM": eps,
    }


class SectorRotationScoreTests(unittest.TestCase):
    def test_volume_beats_quality(self):
        # 78 Tech at Q67 should outrank 17 Basic Materials at Q90 under
        # the volume-weighted composite, even though BM has higher avg_q.
        rows = [_row(f"TECH{i}", "Technology", 67) for i in range(78)] + \
               [_row(f"BM{i}",   "Basic Materials", 90) for i in range(17)]
        out = compute_sector_rotation(pd.DataFrame(rows))
        self.assertEqual(out[0]["sector"], "Technology")
        self.assertEqual(out[1]["sector"], "Basic Materials")
        # Tech score must clearly exceed BM score
        self.assertGreater(out[0]["score"], out[1]["score"] * 2)

    def test_stage2_bonus(self):
        # Two equal-count, equal-Q sectors; one all Stage 2 should win.
        rows = [_row(f"A{i}", "Alpha", 80, stage=2) for i in range(10)] + \
               [_row(f"B{i}", "Beta",  80, stage=1) for i in range(10)]
        out = compute_sector_rotation(pd.DataFrame(rows))
        self.assertEqual(out[0]["sector"], "Alpha")

    def test_empty_and_missing_sector(self):
        self.assertEqual(compute_sector_rotation(pd.DataFrame()), [])
        # rows with blank / nan sector are skipped
        rows = [_row("X", "", 90), _row("Y", "nan", 90), _row("Z", "Energy", 70)]
        out = compute_sector_rotation(pd.DataFrame(rows))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["sector"], "Energy")


class RotatingInTests(unittest.TestCase):
    def test_quality_ranks_above_volume(self):
        # Technology is volume leader; Basic Materials has higher avg_q and count ≥ floor.
        sector_data = compute_sector_rotation(pd.DataFrame(
            [_row(f"TECH{i}", "Technology", 67) for i in range(78)] +
            [_row(f"BM{i}",   "Basic Materials", 90) for i in range(17)] +
            [_row(f"FIN{i}",  "Financial", 80) for i in range(20)]
        ))
        ri = compute_rotating_in(sector_data, count_floor=10)
        self.assertEqual(ri[0]["sector"], "Basic Materials")
        self.assertEqual(ri[1]["sector"], "Financial")
        self.assertEqual(ri[2]["sector"], "Technology")

    def test_floor_excludes_small_sectors(self):
        sector_data = compute_sector_rotation(pd.DataFrame(
            [_row(f"A{i}", "Alpha", 95) for i in range(5)] +  # below floor
            [_row(f"B{i}", "Beta",  70) for i in range(15)]
        ))
        ri = compute_rotating_in(sector_data, count_floor=10)
        self.assertEqual([s["sector"] for s in ri], ["Beta"])

    def test_top_n_cap(self):
        sector_data = compute_sector_rotation(pd.DataFrame(
            [_row(f"{name}{i}", name, q) for name, q in [
                ("Alpha", 90), ("Beta", 85), ("Gamma", 80),
                ("Delta", 75), ("Epsilon", 70),
            ] for i in range(12)]
        ))
        ri = compute_rotating_in(sector_data, count_floor=10, top_n=3)
        self.assertEqual([s["sector"] for s in ri], ["Alpha", "Beta", "Gamma"])

    def test_empty_when_no_sector_meets_floor(self):
        sector_data = compute_sector_rotation(pd.DataFrame(
            [_row("X", "Alpha", 90)]
        ))
        self.assertEqual(compute_rotating_in(sector_data, count_floor=10), [])


class SectorSlugTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(_sector_slug("Basic Materials"), "basic-materials")
        self.assertEqual(_sector_slug("Technology"),      "technology")
        self.assertEqual(_sector_slug("Consumer Cyclical"), "consumer-cyclical")

    def test_empty_and_sentinel(self):
        self.assertEqual(_sector_slug(""), "")
        self.assertEqual(_sector_slug("—"), "")
        self.assertEqual(_sector_slug("nan"), "")
        self.assertEqual(_sector_slug(None), "")

    def test_special_chars_stripped(self):
        self.assertEqual(_sector_slug("Health-Care & Pharma"), "health-care-pharma")


if __name__ == "__main__":
    unittest.main()
