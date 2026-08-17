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


def _row(**over):
    """Build a candidate row with sensible defaults for Ready-to-Enter."""
    base = {
        "Ticker": "MU",
        "Quality Score": 100.0,
        "Stage": {"stage": 2, "perfect": True},
        "VCP": {"confidence": 85, "vcp_possible": True},
        "Dist From High%": -5.0,
        "ATR%": 5.5,
        "Rel Volume": 0.8,
        "SMA20%": 1.0,
        "SMA50%": 4.0,
        "SMA200%": 10.0,
    }
    base.update(over)
    return base


class SelectCandidatesTests(unittest.TestCase):
    """
    Regression coverage for the 2026-08-17 fix: the executor's buy candidate
    pool must be the Ready-to-Enter gate, not a bare Q>=60 + Stage 2 filter.
    Pinned to the real 2026-05-18 DELL miss — DELL (Q99, clean setup) lost its
    slot to AMD (Q100) under the old raw-Q top-10 cut, while BTSG/MTSI/PWR/
    VRT/VIK were bought that day despite most failing VCP/RVol/dist/ATR gates.
    """

    def test_dell_class_setup_is_admitted(self):
        dell = _row(Ticker="DELL", **{
            "Quality Score": 99.0, "ATR%": 5.3, "Dist From High%": -9.8,
            "Rel Volume": 0.63, "VCP": {"confidence": 85, "vcp_possible": True},
            "SMA20%": 5.4, "SMA50%": 23.8, "SMA200%": 61.1,
        })
        out = ae.select_candidates([dell], set())
        self.assertEqual([r["Ticker"] for r in out], ["DELL"])

    def test_high_rvol_chase_is_rejected(self):
        # MTSI 2026-05-18: Q109, Stage 2, but RVol 1.64 > 1.2 cap.
        mtsi = _row(Ticker="MTSI", **{
            "Quality Score": 109.0, "ATR%": 5.3, "Dist From High%": -11.2,
            "Rel Volume": 1.64, "VCP": {"confidence": 55, "vcp_possible": True},
        })
        out = ae.select_candidates([mtsi], set())
        self.assertEqual(out, [])

    def test_weak_vcp_is_rejected(self):
        # VRT 2026-05-18: Q103, Stage 2, but VCP confidence 55 < 70.
        vrt = _row(Ticker="VRT", **{
            "Quality Score": 103.0, "ATR%": 5.5, "Dist From High%": -10.6,
            "Rel Volume": 1.08, "VCP": {"confidence": 55, "vcp_possible": True},
        })
        out = ae.select_candidates([vrt], set())
        self.assertEqual(out, [])

    def test_extended_atr_and_dist_is_rejected(self):
        # SNDK 2026-05-18: Q115 (#1 by raw Q) but ATR 7.8% > 7% cap and
        # dist -16.7% outside the -12% band — exactly the class the old
        # bare Q+Stage filter would rank first and buy.
        sndk = _row(Ticker="SNDK", **{
            "Quality Score": 115.0, "ATR%": 7.8, "Dist From High%": -16.7,
            "Rel Volume": 0.78, "VCP": {"confidence": 70, "vcp_possible": True},
        })
        out = ae.select_candidates([sndk], set())
        self.assertEqual(out, [])

    def test_higher_q_worse_setup_no_longer_beats_clean_setup(self):
        dell = _row(Ticker="DELL", **{
            "Quality Score": 99.0, "ATR%": 5.3, "Dist From High%": -9.8,
            "Rel Volume": 0.63, "VCP": {"confidence": 85, "vcp_possible": True},
        })
        sndk = _row(Ticker="SNDK", **{
            "Quality Score": 115.0, "ATR%": 7.8, "Dist From High%": -16.7,
            "Rel Volume": 0.78, "VCP": {"confidence": 70, "vcp_possible": True},
        })
        out = ae.select_candidates([sndk, dell], set())
        self.assertEqual([r["Ticker"] for r in out], ["DELL"])

    def test_watchlist_merged_row_missing_technicals_is_excluded(self):
        # merge_watchlist_rows() builds a minimal row with no Dist From
        # High%/SMA20%/VCP confidence — must fail the gate, not sneak in on
        # the old Q>=60+Stage2 check.
        wl_row = {
            "Ticker": "ABCD", "Quality Score": 90.0, "ATR%": 4.0,
            "Rel Volume": 1.0, "Appearances": 1.0, "Sector": "", "Screeners": "",
            "Stage": {"stage": 2, "perfect": True}, "VCP": {}, "_source": "watchlist",
        }
        out = ae.select_candidates([wl_row], set())
        self.assertEqual(out, [])

    def test_open_position_excluded(self):
        dell = _row(Ticker="DELL")
        out = ae.select_candidates([dell], {"DELL"})
        self.assertEqual(out, [])

    def test_malformed_row_is_skipped_not_fatal(self):
        # A row with a blank technical field (snapshot-fetch gap on some
        # tickers some days — has happened in production) must not crash
        # the whole run; it's logged and skipped, other candidates proceed.
        bad = _row(Ticker="BAD", **{"Dist From High%": ""})
        dell = _row(Ticker="DELL")
        out = ae.select_candidates([bad, dell], set())
        self.assertEqual([r["Ticker"] for r in out], ["DELL"])

    def test_caps_at_max_candidates(self):
        rows = [_row(Ticker=f"T{i}", **{"Quality Score": 100.0 - i}) for i in range(15)]
        out = ae.select_candidates(rows, set())
        self.assertEqual(len(out), ae.MAX_CANDIDATES)
        self.assertEqual(out[0]["Ticker"], "T0")  # highest Q first


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
