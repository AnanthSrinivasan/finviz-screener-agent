"""
Unit tests for watchlist lifecycle — 3a reactivate, 3b age-out protection,
3d focus cap, 3e entry-ready promotion.
"""

import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import pandas as pd

from agents.screener.finviz_agent import (
    _is_ready_to_enter,
    _update_watchlist,
)


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
        "Sector": "Technology",
        "Screeners": "Growth",
        "Appearances": 1,
        "EPS Y/Y TTM": 410.0,
        "EPS Q/Q": 756.0,
        "Inst Trans": -0.25,
    }
    base.update(over)
    return pd.Series(base)


def _df(rows):
    return pd.DataFrame([r.to_dict() if hasattr(r, "to_dict") else r for r in rows])


# --------------------------------------------------------------------------
# Pure predicate tests
# --------------------------------------------------------------------------

class TestIsReadyToEnter(unittest.TestCase):
    def test_mu_apr15_passes(self):
        self.assertTrue(_is_ready_to_enter(_row(), open_positions_tickers=set()))

    def test_rejects_if_in_open_positions(self):
        self.assertFalse(_is_ready_to_enter(_row(), open_positions_tickers={"MU"}))

    def test_rejects_stage_not_2_perfect(self):
        self.assertFalse(
            _is_ready_to_enter(_row(Stage={"stage": 2, "perfect": False}), set())
        )
        self.assertFalse(_is_ready_to_enter(_row(Stage={"stage": 3, "perfect": True}), set()))

    def test_rejects_vcp_below_70(self):
        self.assertFalse(_is_ready_to_enter(_row(VCP={"confidence": 65}), set()))

    def test_rejects_q_below_80(self):
        self.assertFalse(_is_ready_to_enter(_row(**{"Quality Score": 75}), set()))

    def test_rejects_too_extended(self):
        # -0.5% from high = extended, no pullback
        self.assertFalse(_is_ready_to_enter(_row(**{"Dist From High%": -0.5}), set()))

    def test_rejects_broken_base(self):
        # -15% from high = base broken
        self.assertFalse(_is_ready_to_enter(_row(**{"Dist From High%": -15}), set()))

    def test_rejects_atr_too_high(self):
        self.assertFalse(_is_ready_to_enter(_row(**{"ATR%": 8.5}), set()))

    def test_rejects_rvol_fomo(self):
        self.assertFalse(_is_ready_to_enter(_row(**{"Rel Volume": 1.5}), set()))

    def test_accepts_edge_boundaries(self):
        # Boundary values that SHOULD pass
        self.assertTrue(_is_ready_to_enter(_row(**{"Dist From High%": -1.0}), set()))
        self.assertTrue(_is_ready_to_enter(_row(**{"Dist From High%": -10.0}), set()))
        self.assertTrue(_is_ready_to_enter(_row(**{"ATR%": 7.0}), set()))
        self.assertTrue(_is_ready_to_enter(_row(**{"Rel Volume": 1.2}), set()))
        self.assertTrue(_is_ready_to_enter(_row(**{"Quality Score": 80}), set()))
        self.assertTrue(_is_ready_to_enter(_row(VCP={"confidence": 70}), set()))


# --------------------------------------------------------------------------
# _update_watchlist integration tests (use a temp watchlist.json)
# --------------------------------------------------------------------------

class TestUpdateWatchlist(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # _update_watchlist reads/writes data/watchlist.json relative to cwd
        self.old_cwd = os.getcwd()
        os.makedirs(os.path.join(self.tmpdir, "data"), exist_ok=True)
        os.chdir(self.tmpdir)

    def tearDown(self):
        os.chdir(self.old_cwd)
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_watchlist(self, entries):
        with open("data/watchlist.json", "w") as f:
            json.dump({"watchlist": entries}, f)

    def _read_watchlist(self):
        with open("data/watchlist.json") as f:
            return json.load(f)["watchlist"]

    # 3a — reactivate

    def test_reactivate_archived_age_out(self):
        self._write_watchlist([
            {
                "ticker": "MU", "status": "archived", "priority": "watching",
                "added": "2026-03-01", "source": "screener_auto",
                "archive_reason": "age_out", "archived_date": "2026-03-15",
            }
        ])
        df = _df([_row(Ticker="MU")])
        _update_watchlist(df, "2026-04-23")
        result = self._read_watchlist()
        self.assertEqual(len(result), 1, "must not duplicate MU")
        e = result[0]
        self.assertEqual(e["ticker"], "MU")
        self.assertEqual(e["status"], "watching")
        self.assertIsNone(e["archive_reason"])
        self.assertEqual(e["reactivated_date"], "2026-04-23")

    def test_no_reactivate_manual_archive(self):
        self._write_watchlist([
            {
                "ticker": "MU", "status": "archived", "priority": "watching",
                "added": "2026-03-01", "source": "manual",
                "archive_reason": "stopped_out",
            }
        ])
        df = _df([_row(Ticker="MU")])
        _update_watchlist(df, "2026-04-23")
        result = self._read_watchlist()
        self.assertEqual(len(result), 1, "must not duplicate MU")
        e = result[0]
        self.assertEqual(e["status"], "archived", "manual archive should stay")
        self.assertEqual(e["archive_reason"], "stopped_out")

    def test_no_duplicate_on_watching_rehit(self):
        self._write_watchlist([
            {
                "ticker": "MU", "status": "watching", "priority": "watching",
                "added": "2026-04-09", "source": "screener_auto",
            }
        ])
        df = _df([_row(Ticker="MU")])
        _update_watchlist(df, "2026-04-23")
        result = self._read_watchlist()
        self.assertEqual(len(result), 1)

    # 3b — age-out protection

    def test_age_out_skips_focus_priority(self):
        old_date = (date.today() - timedelta(days=20)).isoformat()
        self._write_watchlist([
            {
                "ticker": "MRVL", "status": "watching", "priority": "focus",
                "added": old_date, "source": "screener_auto",
                "focus_promoted_date": old_date,
            }
        ])
        _update_watchlist(_df([]), "2026-04-23")
        e = self._read_watchlist()[0]
        self.assertEqual(e["status"], "watching", "focus-priority must not be aged out")
        self.assertEqual(e["priority"], "focus")

    def test_age_out_skips_entry_ready_priority(self):
        old_date = (date.today() - timedelta(days=30)).isoformat()
        self._write_watchlist([
            {
                "ticker": "MU", "status": "watching", "priority": "entry-ready",
                "added": old_date, "source": "screener_auto",
            }
        ])
        _update_watchlist(_df([]), "2026-04-23")
        e = self._read_watchlist()[0]
        self.assertEqual(e["status"], "watching")
        self.assertEqual(e["priority"], "entry-ready")

    def test_age_out_archives_watching_priority(self):
        old_date = (date.today() - timedelta(days=20)).isoformat()
        self._write_watchlist([
            {
                "ticker": "OLD", "status": "watching", "priority": "watching",
                "added": old_date, "source": "screener_auto",
            }
        ])
        _update_watchlist(_df([]), "2026-04-23")
        e = self._read_watchlist()[0]
        self.assertEqual(e["status"], "archived")
        self.assertEqual(e["archive_reason"], "age_out")

    # 3d / 3e — promotions

    def test_focus_promotion_cap_is_5(self):
        # 7 watching entries all Stage 2 perfect + Q≥85 in today's screener
        self._write_watchlist([
            {
                "ticker": f"T{i}", "status": "watching", "priority": "watching",
                "added": "2026-04-15", "source": "screener_auto",
            }
            for i in range(7)
        ])
        rows = [
            _row(Ticker=f"T{i}", **{"Quality Score": 100 - i})
            for i in range(7)
        ]
        promoted_focus, _ = _update_watchlist(_df(rows), "2026-04-23")
        self.assertEqual(len(promoted_focus), 5, "cap should be 5")
        self.assertEqual(promoted_focus[:5], ["T0", "T1", "T2", "T3", "T4"])  # top Q wins

    def test_entry_ready_promotion_from_focus(self):
        self._write_watchlist([
            {
                "ticker": "MU", "status": "watching", "priority": "focus",
                "added": "2026-04-09", "source": "screener_auto",
                "focus_promoted_date": "2026-04-15",
            }
        ])
        _, promoted_er = _update_watchlist(_df([_row(Ticker="MU")]), "2026-04-23")
        self.assertEqual(promoted_er, ["MU"])
        e = self._read_watchlist()[0]
        self.assertEqual(e["priority"], "entry-ready")
        self.assertEqual(e["entry_ready_date"], "2026-04-23")

    def test_entry_ready_excludes_open_positions(self):
        self._write_watchlist([
            {
                "ticker": "MU", "status": "watching", "priority": "focus",
                "added": "2026-04-09", "source": "screener_auto",
            }
        ])
        # Mock positions.json with MU open
        with open("data/positions.json", "w") as f:
            json.dump(
                {"open_positions": [{"ticker": "MU", "status": "open"}], "closed_positions": []},
                f,
            )
        _, promoted_er = _update_watchlist(_df([_row(Ticker="MU")]), "2026-04-23")
        self.assertEqual(promoted_er, [])
        e = self._read_watchlist()[0]
        self.assertEqual(e["priority"], "focus", "still focus, not promoted to entry-ready")

    def test_full_mu_scenario_watching_to_entry_ready(self):
        """Regression test: MU sat at priority=watching while hitting criteria.
        With the fix, single run should promote watching → focus → entry-ready."""
        self._write_watchlist([
            {
                "ticker": "MU", "status": "watching", "priority": "watching",
                "added": "2026-04-09", "source": "screener_auto",
            }
        ])
        promoted_focus, promoted_er = _update_watchlist(_df([_row(Ticker="MU")]), "2026-04-23")
        self.assertIn("MU", promoted_focus)
        self.assertIn("MU", promoted_er)
        e = self._read_watchlist()[0]
        self.assertEqual(e["priority"], "entry-ready")


if __name__ == "__main__":
    unittest.main()
