"""
Unit tests for agents/trading/alpaca_executor.py — focus on the entry-gate
peel-warn helper. The gate blocks new entries when ATR multiple exceeds the
per-ticker calibrated warn threshold (or an ATR% tier fallback).
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from agents.trading import alpaca_executor as ae


class EntryPeelWarnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._patcher = patch.object(ae, "DATA_DIR", self.tmp.name)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        ae._PEEL_CALIBRATION_CACHE = None

    def _write_calibration(self, payload: dict):
        path = os.path.join(self.tmp.name, "peel_calibration.json")
        with open(path, "w") as f:
            json.dump(payload, f)

    def test_calibrated_ticker_uses_warn(self):
        self._write_calibration({
            "AAOI": {"calibrated": True, "warn": 11.8, "signal": 15.8},
        })
        warn, src = ae.get_entry_peel_warn(atr_pct=8.6, ticker="AAOI")
        self.assertAlmostEqual(warn, 11.8)
        self.assertEqual(src, "calibrated")

    def test_uncalibrated_ticker_falls_back_to_tier(self):
        self._write_calibration({
            "AAOI": {"calibrated": False, "reason": "insufficient_runs"},
        })
        # ATR 8.6% → high tier → warn 6.5
        warn, src = ae.get_entry_peel_warn(atr_pct=8.6, ticker="AAOI")
        self.assertAlmostEqual(warn, 6.5)
        self.assertEqual(src, "tier")

    def test_missing_ticker_uses_tier(self):
        self._write_calibration({})
        for atr, expected in [(3.0, 3.0), (6.0, 5.0), (9.0, 6.5), (15.0, 8.5)]:
            warn, src = ae.get_entry_peel_warn(atr_pct=atr, ticker="NONEXIST")
            self.assertAlmostEqual(warn, expected)
            self.assertEqual(src, "tier")

    def test_missing_calibration_file_uses_tier(self):
        # No peel_calibration.json at all
        warn, src = ae.get_entry_peel_warn(atr_pct=3.5, ticker="ANY")
        self.assertAlmostEqual(warn, 3.0)
        self.assertEqual(src, "tier")

    def test_gate_blocks_when_multiple_exceeds_calibrated_warn(self):
        # AAOI calibrated warn 11.8 → multiple 13.2 should block
        self._write_calibration({
            "AAOI": {"calibrated": True, "warn": 11.8, "signal": 15.8},
        })
        warn, _ = ae.get_entry_peel_warn(atr_pct=8.6, ticker="AAOI")
        atr_multiple = 13.2
        self.assertTrue(atr_multiple > warn)

    def test_gate_passes_when_multiple_below_calibrated_warn(self):
        # AAOI calibrated warn 11.8 → multiple 8.3 allowed (previously blocked at hardcoded 6)
        self._write_calibration({
            "AAOI": {"calibrated": True, "warn": 11.8, "signal": 15.8},
        })
        warn, _ = ae.get_entry_peel_warn(atr_pct=8.6, ticker="AAOI")
        atr_multiple = 8.3
        self.assertFalse(atr_multiple > warn)


class EffectiveMaxPositionsTests(unittest.TestCase):
    def test_green_returns_10(self):
        self.assertEqual(ae.effective_max_positions("GREEN"), 10)

    def test_thrust_returns_10(self):
        self.assertEqual(ae.effective_max_positions("THRUST"), 10)

    def test_caution_returns_7(self):
        self.assertEqual(ae.effective_max_positions("CAUTION"), 7)

    def test_cooling_returns_5(self):
        self.assertEqual(ae.effective_max_positions("COOLING"), 5)

    def test_red_returns_5(self):
        self.assertEqual(ae.effective_max_positions("RED"), 5)

    def test_danger_returns_5(self):
        self.assertEqual(ae.effective_max_positions("DANGER"), 5)

    def test_blackout_returns_5(self):
        self.assertEqual(ae.effective_max_positions("BLACKOUT"), 5)


class ScreenerCsvFallbackTests(unittest.TestCase):
    """Off-cycle executor runs (manual retry, late workflow_run) can fire before
    today's screener CSV exists — fall back to the most recent CSV ≤ today, but
    refuse data staler than MAX_SCREENER_STALE_DAYS."""

    HEADER = "Ticker,Quality Score,ATR%,SMA50%,Stage,VCP\n"
    ROW = "DAVE,85,4.0,5.0,{},{}\n"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._patcher = patch.object(ae, "DATA_DIR", self.tmp.name)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def _write_csv(self, date_str: str):
        path = os.path.join(self.tmp.name, f"finviz_screeners_{date_str}.csv")
        with open(path, "w") as f:
            f.write(self.HEADER)
            f.write(self.ROW)
        return path

    def test_today_present_uses_today(self):
        self._write_csv("2026-06-09")
        self.assertEqual(
            ae._resolve_screener_csv("2026-06-09"),
            os.path.join(self.tmp.name, "finviz_screeners_2026-06-09.csv"),
        )

    def test_today_absent_falls_back_to_recent(self):
        self._write_csv("2026-06-04")
        self._write_csv("2026-06-08")
        # 2026-06-09 absent → newest ≤ today is 06-08
        self.assertEqual(
            ae._resolve_screener_csv("2026-06-09"),
            os.path.join(self.tmp.name, "finviz_screeners_2026-06-08.csv"),
        )

    def test_future_dated_files_ignored(self):
        self._write_csv("2026-06-08")
        self._write_csv("2026-06-15")  # future — must not be picked
        self.assertEqual(
            ae._resolve_screener_csv("2026-06-09"),
            os.path.join(self.tmp.name, "finviz_screeners_2026-06-08.csv"),
        )

    def test_all_absent_returns_empty(self):
        self.assertEqual(ae._resolve_screener_csv("2026-06-09"), "")
        self.assertEqual(ae.load_screener_csv("2026-06-09"), [])

    def test_stale_data_refused(self):
        # Newest CSV more than MAX_SCREENER_STALE_DAYS old → refuse.
        self._write_csv("2026-05-01")
        self.assertEqual(ae._resolve_screener_csv("2026-06-09"), "")

    def test_load_returns_rows_from_fallback(self):
        self._write_csv("2026-06-08")
        rows = ae.load_screener_csv("2026-06-09")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Ticker"], "DAVE")


if __name__ == "__main__":
    unittest.main()
