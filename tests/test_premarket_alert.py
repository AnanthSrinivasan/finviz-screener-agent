"""
Unit tests for agents/alerts/premarket_alert.py helpers. Focus:
  - _load_conviction walks back up to Q_RANK_FALLBACK_DAYS files when a ticker
    drops out of today's Finviz screener (BE scenario)
  - _q_label renders staleness suffix only when q_rank recovered from an older file
  - _sizing_label thresholds (quick sanity check)
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from agents.alerts import premarket_alert as pa


class ConvictionFallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._patcher = patch.object(pa, "DATA_DIR", self.tmp.name)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def _write_quality(self, date: str, payload: dict):
        path = os.path.join(self.tmp.name, f"daily_quality_{date}.json")
        with open(path, "w") as f:
            json.dump(payload, f)

    def _write_screener(self, date: str, rows):
        path = os.path.join(self.tmp.name, f"finviz_screeners_{date}.csv")
        pd.DataFrame(rows).to_csv(path, index=False)

    def test_fresh_hit(self):
        self._write_quality("2026-04-18", {"BE": {"q_rank": 81}})
        self._write_screener("2026-04-18", [{"Ticker": "BE", "Appearances": 3}])
        q, apps, stale = pa._load_conviction("BE")
        self.assertEqual(q, 81)
        self.assertEqual(apps, 3)
        self.assertEqual(stale, 0)

    def test_stale_fallback_two_days(self):
        # Today's file has no BE; file 2 days back does.
        self._write_quality("2026-04-16", {"BE": {"q_rank": 81}})
        self._write_quality("2026-04-17", {"OTHER": {"q_rank": 70}})
        self._write_quality("2026-04-18", {"OTHER": {"q_rank": 70}})
        q, _apps, stale = pa._load_conviction("BE")
        self.assertEqual(q, 81)
        self.assertEqual(stale, 2)

    def test_never_seen_within_window(self):
        for d in ("2026-04-10", "2026-04-11", "2026-04-12"):
            self._write_quality(d, {"OTHER": {"q_rank": 60}})
        q, _apps, stale = pa._load_conviction("GHOST")
        self.assertEqual(q, 0)
        self.assertEqual(stale, -1)

    def test_fallback_window_capped(self):
        # 15 files exist, BE only in the oldest 5 → lookback of 10 should NOT find it.
        for i, d in enumerate([
            "2026-04-01", "2026-04-02", "2026-04-03", "2026-04-04", "2026-04-05",
            "2026-04-06", "2026-04-07", "2026-04-08", "2026-04-09", "2026-04-10",
            "2026-04-11", "2026-04-12", "2026-04-13", "2026-04-14", "2026-04-15",
        ]):
            payload = {"BE": {"q_rank": 75}} if i < 5 else {"OTHER": {"q_rank": 60}}
            self._write_quality(d, payload)
        q, _apps, stale = pa._load_conviction("BE")
        self.assertEqual(q, 0)
        self.assertEqual(stale, -1)

    def test_appearances_from_today_only(self):
        # Appearances should come from today's screener CSV regardless of q fallback.
        self._write_quality("2026-04-15", {"BE": {"q_rank": 90}})
        self._write_quality("2026-04-18", {"OTHER": {"q_rank": 70}})
        self._write_screener("2026-04-18", [{"Ticker": "BE", "Appearances": 4}])
        q, apps, stale = pa._load_conviction("BE")
        self.assertEqual(q, 90)
        self.assertEqual(apps, 4)
        self.assertGreater(stale, 0)


class QLabelTests(unittest.TestCase):
    def test_fresh_label(self):
        self.assertEqual(pa._q_label(81, 0), "Q:81")

    def test_stale_label(self):
        self.assertEqual(pa._q_label(81, 2), "Q:81 (2d)")

    def test_zero_label(self):
        self.assertEqual(pa._q_label(0, -1), "Q:0")
        self.assertEqual(pa._q_label(0, 5), "Q:0")


class SizingLabelTests(unittest.TestCase):
    def test_aggressive(self):
        self.assertIn("AGGRESSIVE", pa._sizing_label(90, 1))
        self.assertIn("AGGRESSIVE", pa._sizing_label(0, 4))

    def test_normal(self):
        self.assertIn("NORMAL", pa._sizing_label(75, 1))
        self.assertIn("NORMAL", pa._sizing_label(0, 2))

    def test_reduced(self):
        self.assertIn("REDUCED", pa._sizing_label(60, 1))
        self.assertIn("REDUCED", pa._sizing_label(0, 0))


if __name__ == "__main__":
    unittest.main()
