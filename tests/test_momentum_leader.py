"""⚡ Momentum Leader lane.

Every other Stage 2 block gates on ATR / RVol / VCP, which describes a quiet,
coiling stock. A name up 300%+ on the year is never quiet, so the system was
structurally blind to its own biggest winners — TWST appeared in the screener
15 times (RS 87-98) between 2026-06-24 and 2026-08-13 and never once reached a
Slack block or the watchlist while it ran $99 -> $125+.

This lane drops the ATR and RVol caps and leans on RS instead. Peel-safe stays
hard (user decision 2026-08-19): it is what correctly excluded TWST on
2026-06-26 at mult 8.63 and admitted it on 2026-08-04 at mult 2.64.
"""
import unittest

from agents.screener.finviz_agent import (
    _is_momentum_leader,
    MOMENTUM_LEADER_RS_MIN,
    MOMENTUM_LEADER_Q_MIN,
)


def _row(**kw):
    base = {
        "Ticker": "AAA",
        "Stage": {"stage": 2, "perfect": True},
        "RS Rating": 95,
        "Quality Score": 75,
        "Dist From High%": -4.0,
        "ATR%": 7.4,        # would fail Ready-to-Enter's ATR<=7
        "Rel Volume": 1.7,  # would fail Ready-to-Enter's RVol<=1.2
        "SMA50%": 19.0,     # 19.0 / 7.4 = 2.57 <= warn 6.5 -> peel-safe
    }
    base.update(kw)
    return base


class MomentumLeaderTests(unittest.TestCase):
    def test_thresholds_are_the_agreed_values(self):
        self.assertEqual(MOMENTUM_LEADER_RS_MIN, 85)
        self.assertEqual(MOMENTUM_LEADER_Q_MIN, 60)

    def test_high_atr_high_rvol_leader_passes(self):
        """The whole point: a name that fails every quiet-setup gate."""
        self.assertTrue(_is_momentum_leader(_row(), set(), set()))

    def test_twst_2026_08_04_passes(self):
        """Real row: Q71 RS93 dist -5.7 ATR 7.57 mult 2.20 — the day before the run."""
        r = _row(Ticker="TWST", **{"Quality Score": 71, "RS Rating": 93,
                                   "Dist From High%": -5.717, "ATR%": 7.572,
                                   "SMA50%": 16.7})
        self.assertTrue(_is_momentum_leader(r, set(), set()))

    def test_twst_2026_06_26_rejected_by_peel_safe(self):
        """Same stock, extended: mult 8.63 vs warn 5.0. Must stay out."""
        r = _row(Ticker="TWST", **{"Quality Score": 81, "RS Rating": 90,
                                   "Dist From High%": 1.46, "ATR%": 5.84,
                                   "SMA50%": 50.4})
        self.assertFalse(_is_momentum_leader(r, set(), set()))

    def test_rs_below_threshold_rejected(self):
        self.assertFalse(_is_momentum_leader(_row(**{"RS Rating": 84}), set(), set()))

    def test_rs_at_threshold_passes(self):
        self.assertTrue(_is_momentum_leader(_row(**{"RS Rating": 85}), set(), set()))

    def test_low_quality_rejected(self):
        self.assertFalse(_is_momentum_leader(_row(**{"Quality Score": 59}), set(), set()))

    def test_not_stage2_rejected(self):
        self.assertFalse(_is_momentum_leader(_row(Stage={"stage": 1}), set(), set()))

    def test_deep_drawdown_rejected(self):
        """Leading, not repairing — TEM at -52.7% must never appear here."""
        self.assertFalse(_is_momentum_leader(_row(**{"Dist From High%": -52.7}), set(), set()))

    def test_extended_rejected_even_with_perfect_rs(self):
        """peel-safe is NOT bypassed by a 99 RS — this is what stops top-chasing."""
        r = _row(**{"RS Rating": 99, "ATR%": 4.0, "SMA50%": 40.0})  # mult 10 vs warn 3
        self.assertFalse(_is_momentum_leader(r, set(), set()))

    def test_held_position_excluded(self):
        self.assertFalse(_is_momentum_leader(_row(), {"AAA"}, set()))

    def test_already_in_another_callout_excluded(self):
        self.assertFalse(_is_momentum_leader(_row(), set(), {"AAA"}))

    def test_missing_fields_fail_closed(self):
        for bad in ({"RS Rating": None}, {"Quality Score": None},
                    {"Dist From High%": None}, {"ATR%": None},
                    {"ATR%": 0}, {"SMA50%": None}):
            self.assertFalse(_is_momentum_leader(_row(**bad), set(), set()), bad)

    def test_missing_ticker_fails_closed(self):
        self.assertFalse(_is_momentum_leader(_row(Ticker=None), set(), set()))


class MomentumTierPromotionTests(unittest.TestCase):
    """The momentum tier must hold every current momentum leader, not just the
    ones this lane discovered first.

    Found in the 2026-08-19 production run: PSNL fired as a Momentum Leader but
    had been added earlier by Rotation Catalyst, so it kept
    source=rotation_catalyst_auto while carrying priority=momentum. The
    watchlist category grid groups by source, so the tier and the card
    disagreed — and an existing *active* row never changed tier at all.

    NOTE: _update_watchlist resolves "data/watchlist.json" relative to the CWD
    and does NOT honour DATA_DIR, so these tests chdir into a temp tree. An
    earlier version of this file set DATA_DIR only and mutated the real
    watchlist.
    """

    def setUp(self):
        import os, tempfile
        self.prev_cwd = os.getcwd()
        self.dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.dir, "data"))
        self.path = os.path.join(self.dir, "data", "watchlist.json")

    def tearDown(self):
        import os, shutil
        os.chdir(self.prev_cwd)
        shutil.rmtree(self.dir, ignore_errors=True)

    def _run(self, rows):
        import json, os
        import pandas as pd
        from agents.screener import finviz_agent as fa
        with open(self.path, "w") as f:
            json.dump({"watchlist": rows}, f)
        os.chdir(self.dir)
        try:
            fa._update_watchlist(pd.DataFrame(), "2026-08-19",
                                 momentum_leader_tickers=["PSNL"])
        finally:
            os.chdir(self.prev_cwd)
        with open(self.path) as f:
            return {r["ticker"]: r for r in json.load(f)["watchlist"]}

    def test_existing_focus_row_moves_to_momentum_tier_with_source(self):
        out = self._run([{"ticker": "PSNL", "status": "watching",
                          "priority": "focus", "source": "rotation_catalyst_auto",
                          "added": "2026-08-01"}])
        row = out["PSNL"]
        self.assertEqual(row["priority"], "momentum")
        self.assertEqual(row["source"], "momentum_leader_auto",
                         "source must move with the tier or the category grid mis-files it")
        self.assertEqual(row["prev_source"], "rotation_catalyst_auto")
        self.assertEqual(row["prev_priority"], "focus")

    def test_entry_ready_is_never_demoted(self):
        # last_seen_in_screener must be current, or the unrelated stale-demotion
        # rule (entry-ready -> focus after 5 absent days) fires first and this
        # asserts the wrong thing.
        out = self._run([{"ticker": "PSNL", "status": "watching",
                          "priority": "entry-ready", "source": "screener_auto",
                          "added": "2026-08-01",
                          "entry_ready_date": "2026-08-19",
                          "last_seen_in_screener": "2026-08-19"}])
        self.assertEqual(out["PSNL"]["priority"], "entry-ready")
        self.assertEqual(out["PSNL"]["source"], "screener_auto")

    def test_promotion_is_idempotent(self):
        out = self._run([{"ticker": "PSNL", "status": "watching",
                          "priority": "momentum", "source": "momentum_leader_auto",
                          "prev_source": "rotation_catalyst_auto",
                          "added": "2026-08-01"}])
        self.assertEqual(out["PSNL"]["prev_source"], "rotation_catalyst_auto",
                         "re-firing must not overwrite the original source record")


if __name__ == "__main__":
    unittest.main()
