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

    def test_calibrated_warn_tighter_than_tier_applies(self):
        # Calibration may only tighten: warn 4.2 < tier 6.5 → calibrated wins
        self._write_calibration({
            "AAOI": {"calibrated": True, "warn": 4.2, "signal": 8.0},
        })
        warn, src = ae.get_entry_peel_warn(atr_pct=8.6, ticker="AAOI")
        self.assertAlmostEqual(warn, 4.2)
        self.assertEqual(src, "calibrated")

    def test_calibrated_warn_looser_than_tier_is_capped(self):
        # 2026-06-12 ALAB/MU bug: calibrated warn 11.8 > tier 6.5 (ATR 8.6 →
        # high tier) must be capped — calibration can never loosen the gate.
        self._write_calibration({
            "AAOI": {"calibrated": True, "warn": 11.8, "signal": 15.8},
        })
        warn, src = ae.get_entry_peel_warn(atr_pct=8.6, ticker="AAOI")
        self.assertAlmostEqual(warn, 6.5)
        self.assertEqual(src, "tier-cap")

    def test_alab_mu_2026_06_12_regression(self):
        # Real data from the dry run the user flagged ("you are bloody chasing"):
        # ALAB ATR 8.20 mult 7.16 (cal warn 10.3) · MU ATR 7.56 mult 6.57
        # (cal warn 8.7). Both must now be blocked by the tier cap (6.5).
        self._write_calibration({
            "ALAB": {"calibrated": True, "warn": 10.3, "signal": 13.7},
            "MU":   {"calibrated": True, "warn": 8.7,  "signal": 11.6},
        })
        warn_alab, src_alab = ae.get_entry_peel_warn(atr_pct=8.20, ticker="ALAB")
        warn_mu, src_mu     = ae.get_entry_peel_warn(atr_pct=7.56, ticker="MU")
        self.assertAlmostEqual(warn_alab, 6.5)
        self.assertEqual(src_alab, "tier-cap")
        self.assertTrue(7.16 > warn_alab)   # ALAB blocked
        self.assertAlmostEqual(warn_mu, 6.5)
        self.assertEqual(src_mu, "tier-cap")
        self.assertTrue(6.57 > warn_mu)     # MU blocked

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

    def test_gate_blocks_when_multiple_exceeds_warn(self):
        self._write_calibration({
            "AAOI": {"calibrated": True, "warn": 11.8, "signal": 15.8},
        })
        warn, _ = ae.get_entry_peel_warn(atr_pct=8.6, ticker="AAOI")
        atr_multiple = 13.2
        self.assertTrue(atr_multiple > warn)

    def test_gate_passes_when_multiple_below_warn(self):
        # Calibrated 4.8 ≤ tier 6.5 → calibrated applies; multiple 3.9 allowed
        self._write_calibration({
            "AAOI": {"calibrated": True, "warn": 4.8, "signal": 8.0},
        })
        warn, _ = ae.get_entry_peel_warn(atr_pct=8.6, ticker="AAOI")
        atr_multiple = 3.9
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
