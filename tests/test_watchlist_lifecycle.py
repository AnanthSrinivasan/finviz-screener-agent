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
        r = _update_watchlist(_df(rows), "2026-04-23")
        self.assertEqual(len(r["promoted_to_focus"]), 5, "cap should be 5")
        self.assertEqual(r["promoted_to_focus"][:5], ["T0", "T1", "T2", "T3", "T4"])  # top Q wins

    def test_entry_ready_promotion_from_focus(self):
        self._write_watchlist([
            {
                "ticker": "MU", "status": "watching", "priority": "focus",
                "added": "2026-04-09", "source": "screener_auto",
                "focus_promoted_date": "2026-04-15",
            }
        ])
        r = _update_watchlist(_df([_row(Ticker="MU")]), "2026-04-23")
        self.assertEqual(r["promoted_to_entry_ready"], ["MU"])
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
        # Mock positions.json with MU held (status=active)
        with open("data/positions.json", "w") as f:
            json.dump(
                {"open_positions": [{"ticker": "MU", "status": "active"}], "closed_positions": []},
                f,
            )
        r = _update_watchlist(_df([_row(Ticker="MU")]), "2026-04-23")
        self.assertEqual(r["promoted_to_entry_ready"], [])
        # Held-position auto-archive trumps the focus priority
        e = self._read_watchlist()[0]
        self.assertEqual(e["status"], "archived")
        self.assertEqual(e["archive_reason"], "entered_position")

    # Hidden Growth entry path (parallel to technical add)

    # Held-position auto-archive (real + paper)

    def test_auto_archive_when_ticker_becomes_active_position(self):
        """Watchlist entry for a ticker now in open_positions with status=active
        should be auto-archived with reason=entered_position."""
        self._write_watchlist([
            {"ticker": "CORZ", "status": "watching", "priority": "focus",
             "added": "2026-03-24", "source": "screener_auto"}
        ])
        with open("data/positions.json", "w") as f:
            json.dump(
                {"open_positions": [{"ticker": "CORZ", "status": "active"}],
                 "closed_positions": []},
                f,
            )
        _update_watchlist(_df([]), "2026-04-24")
        e = self._read_watchlist()[0]
        self.assertEqual(e["status"], "archived")
        self.assertEqual(e["archive_reason"], "entered_position")
        self.assertEqual(e["archived_date"], "2026-04-24")

    def test_stop_hit_status_does_NOT_trigger_archive(self):
        """Tickers with status=stop_hit are effectively closed — should not
        suppress re-entry signals, so must NOT trigger held-archive.
        Uses a recent `added` date to avoid age-out firing independently."""
        today = date.today().isoformat()
        self._write_watchlist([
            {"ticker": "AAOI", "status": "watching", "priority": "watching",
             "added": today, "source": "screener_auto"}
        ])
        with open("data/positions.json", "w") as f:
            json.dump(
                {"open_positions": [{"ticker": "AAOI", "status": "stop_hit"}],
                 "closed_positions": []},
                f,
            )
        _update_watchlist(_df([]), "2026-04-24")
        e = self._read_watchlist()[0]
        self.assertEqual(e["status"], "watching", "stop_hit should not archive")

    def test_paper_held_also_triggers_archive(self):
        """Paper-held tickers (present in paper_stops.json) should also auto-archive."""
        self._write_watchlist([
            {"ticker": "MTSI", "status": "watching", "priority": "entry-ready",
             "added": "2026-04-06", "source": "screener_auto"}
        ])
        with open("data/paper_stops.json", "w") as f:
            json.dump({"MTSI": {"entry_price": 280.0, "stop_price": 265.0}}, f)
        _update_watchlist(_df([]), "2026-04-24")
        e = self._read_watchlist()[0]
        self.assertEqual(e["status"], "archived")
        self.assertEqual(e["archive_reason"], "entered_position")

    def test_entry_ready_promotion_blocked_when_held(self):
        """Even if a ticker is on the watchlist and meets entry-ready criteria,
        being actively held blocks promotion (via _is_ready_to_enter's open_pos check)."""
        self._write_watchlist([
            {"ticker": "MU", "status": "watching", "priority": "watching",
             "added": "2026-04-09", "source": "screener_auto"}
        ])
        with open("data/positions.json", "w") as f:
            json.dump({"open_positions": [{"ticker": "MU", "status": "active"}],
                       "closed_positions": []}, f)
        r = _update_watchlist(_df([_row(Ticker="MU")]), "2026-04-24")
        # Ticker got archived (held) before promotion could occur
        self.assertEqual(r["promoted_to_entry_ready"], [])
        e = self._read_watchlist()[0]
        self.assertEqual(e["status"], "archived")

    def test_hidden_growth_auto_add_new_ticker(self):
        """Hidden Growth hit not yet in watchlist → added at priority=watching,
        source=hidden_growth_auto, with research note."""
        self._write_watchlist([])
        _update_watchlist(_df([]), "2026-04-23", hidden_growth_tickers=["NVTS"])
        result = self._read_watchlist()
        self.assertEqual(len(result), 1)
        e = result[0]
        self.assertEqual(e["ticker"], "NVTS")
        self.assertEqual(e["priority"], "watching")
        self.assertEqual(e["source"], "hidden_growth_auto")
        self.assertIn("Hidden Growth", e["entry_note"])

    def test_hidden_growth_no_duplicate_when_already_watching(self):
        """Hidden Growth hit already in watchlist as watching → no-op, no duplicate."""
        self._write_watchlist([
            {
                "ticker": "NVTS", "status": "watching", "priority": "watching",
                "added": "2026-04-16", "source": "screener_auto",
            }
        ])
        _update_watchlist(_df([]), "2026-04-23", hidden_growth_tickers=["NVTS"])
        result = self._read_watchlist()
        self.assertEqual(len(result), 1)
        # Source preserved from original — not overwritten
        self.assertEqual(result[0]["source"], "screener_auto")

    def test_hidden_growth_reactivates_archived(self):
        """Hidden Growth hit in archived state (age_out) → reactivated."""
        self._write_watchlist([
            {
                "ticker": "NVTS", "status": "archived", "priority": "watching",
                "added": "2026-03-01", "source": "screener_auto",
                "archive_reason": "age_out",
            }
        ])
        _update_watchlist(_df([]), "2026-04-23", hidden_growth_tickers=["NVTS"])
        e = self._read_watchlist()[0]
        self.assertEqual(e["status"], "watching")
        self.assertEqual(e["reactivated_date"], "2026-04-23")

    def test_hidden_growth_handles_empty_list(self):
        """Missing / empty hidden_growth_tickers should not blow up."""
        self._write_watchlist([])
        _update_watchlist(_df([]), "2026-04-23")
        _update_watchlist(_df([]), "2026-04-23", hidden_growth_tickers=None)
        _update_watchlist(_df([]), "2026-04-23", hidden_growth_tickers=[])
        # Just asserting no exception raised

    def test_full_mu_scenario_watching_to_entry_ready(self):
        """Regression test: MU sat at priority=watching while hitting criteria.
        With the fix, single run should promote watching → focus → entry-ready."""
        self._write_watchlist([
            {
                "ticker": "MU", "status": "watching", "priority": "watching",
                "added": "2026-04-09", "source": "screener_auto",
            }
        ])
        r = _update_watchlist(_df([_row(Ticker="MU")]), "2026-04-23")
        self.assertIn("MU", r["promoted_to_focus"])
        self.assertIn("MU", r["promoted_to_entry_ready"])
        e = self._read_watchlist()[0]
        self.assertEqual(e["priority"], "entry-ready")


if __name__ == "__main__":
    unittest.main()
