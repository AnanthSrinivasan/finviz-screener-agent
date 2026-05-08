"""
Tests for the ETF-based sector rotation tracker (agents.sector_rotation).

(`tests/test_sector_rotation.py` already exists for the screener-derived
sector rotation in finviz_agent.py — different feature, kept separate.)
"""

import unittest

import pandas as pd

from agents import sector_rotation as sr


class PercentileRankTests(unittest.TestCase):
    def test_known_returns(self):
        vals = [-0.05, -0.02, 0.00, 0.03, 0.07]
        # 0.03 is higher than 3 of 5 → ~59 percentile rank on 0-99 scale
        self.assertEqual(sr.percentile_rank(vals, 0.03), int(round(3 / 5 * 99)))
        self.assertEqual(sr.percentile_rank(vals, -0.10), 0)
        self.assertEqual(sr.percentile_rank(vals, 1.0), 99)

    def test_empty(self):
        self.assertEqual(sr.percentile_rank([], 0.5), 0)


class ReturnsAndRankTests(unittest.TestCase):
    def _bars(self, closes):
        ts = pd.date_range("2026-01-01", periods=len(closes), freq="B")
        return pd.DataFrame({"t": ts, "c": closes})

    def test_compute_returns_and_rank(self):
        # Make 25 bars so we have 5d and 20d returns plus the 20d-window check
        spy = self._bars([100 + i for i in range(25)])  # +1/day
        smh = self._bars([100 + 2 * i for i in range(25)])  # +2/day, beats SPY
        gld = self._bars([100 - 0.5 * i for i in range(25)])  # decays vs SPY

        rows = sr.compute_returns({"SMH": smh, "GLD": gld}, spy["c"])
        self.assertEqual({r["etf"] for r in rows}, {"SMH", "GLD"})
        smh_row = next(r for r in rows if r["etf"] == "SMH")
        gld_row = next(r for r in rows if r["etf"] == "GLD")
        self.assertGreater(smh_row["ret_vs_spy_20d"], 0)
        self.assertLess(gld_row["ret_vs_spy_20d"], 0)

        sr.assign_rs_and_rank(rows)
        self.assertEqual(rows[0]["etf"], "SMH")
        self.assertEqual(rows[0]["rank"], 1)
        self.assertEqual(rows[1]["rank"], 2)
        self.assertGreater(smh_row["rs_score"], gld_row["rs_score"])


class HistoryAnnotationTests(unittest.TestCase):
    def test_decay_streak_2_day_confirmation(self):
        # Synthetic history: SMH has rank 5 today, but was 1 yesterday and 2 day before.
        # That's worsening → 2d decay streak. rs_score < 50 enforced.
        history = [
            {"date": "2026-05-06", "etf": "SMH", "rank": 1, "rs_score": 95, "is_20d_rs_high": False, "ret_1d": 0.01},
            {"date": "2026-05-07", "etf": "SMH", "rank": 2, "rs_score": 80, "is_20d_rs_high": False, "ret_1d": -0.01},
        ]
        rows = [{"etf": "SMH", "rank": 5, "rs_score": 30, "is_20d_rs_high": False, "ret_vs_spy_20d": -0.01, "ret_1d": -0.02}]
        sr.annotate_with_history(rows, history, today="2026-05-08")
        self.assertEqual(rows[0]["decay_streak_days"], 2)
        # Falls back to earliest available when <5 days of history
        self.assertEqual(rows[0]["rank_5d_ago"], 1)

    def test_decay_streak_skipped_when_rs_high(self):
        # rs_score=70 → does not count as decay even if rank worsens
        history = [
            {"date": "2026-05-06", "etf": "XLK", "rank": 1, "rs_score": 95, "is_20d_rs_high": False, "ret_1d": 0.01},
            {"date": "2026-05-07", "etf": "XLK", "rank": 2, "rs_score": 90, "is_20d_rs_high": False, "ret_1d": 0.0},
        ]
        rows = [{"etf": "XLK", "rank": 4, "rs_score": 70, "is_20d_rs_high": False, "ret_vs_spy_20d": 0.05, "ret_1d": 0.0}]
        sr.annotate_with_history(rows, history, today="2026-05-08")
        self.assertEqual(rows[0]["decay_streak_days"], 0)

    def test_anticipation_requires_2_consecutive_high_days(self):
        # Yesterday hit 20d RS high; today hits again → confirmed.
        history = [
            {"date": "2026-05-07", "etf": "JETS", "rank": 12, "rs_score": 55, "is_20d_rs_high": True, "ret_1d": 0.02},
        ]
        rows = [{"etf": "JETS", "rank": 10, "rs_score": 58, "is_20d_rs_high": True, "ret_vs_spy_20d": 0.04, "ret_1d": 0.01}]
        sr.annotate_with_history(rows, history, today="2026-05-08")
        self.assertTrue(rows[0]["anticipation_confirmed"])

    def test_anticipation_not_fired_on_single_day(self):
        history = [
            {"date": "2026-05-07", "etf": "JETS", "rank": 12, "rs_score": 55, "is_20d_rs_high": False, "ret_1d": 0.02},
        ]
        rows = [{"etf": "JETS", "rank": 10, "rs_score": 58, "is_20d_rs_high": True, "ret_vs_spy_20d": 0.04, "ret_1d": 0.01}]
        sr.annotate_with_history(rows, history, today="2026-05-08")
        self.assertFalse(rows[0]["anticipation_confirmed"])

    def test_rank_delta_5d(self):
        history = []
        for i, day in enumerate(["05-01", "05-02", "05-04", "05-05", "05-06", "05-07"]):
            history.append({"date": f"2026-{day}", "etf": "XLK", "rank": 3, "rs_score": 80, "is_20d_rs_high": False, "ret_1d": 0.0})
        rows = [{"etf": "XLK", "rank": 13, "rs_score": 50, "is_20d_rs_high": False, "ret_vs_spy_20d": 0.0, "ret_1d": 0.0}]
        sr.annotate_with_history(rows, history, today="2026-05-08")
        # 5 trading days back was rank 3 (sorted asc takes index -5 from sorted history)
        self.assertEqual(rows[0]["rank_5d_ago"], 3)
        self.assertEqual(rows[0]["rank_delta_5d"], 10)


class SignalsTests(unittest.TestCase):
    def _row(self, **kw):
        base = {"etf": "X", "name": "X", "theme": "g", "rs_score": 0,
                "rank": 1, "rank_delta_5d": 0, "decay_streak_days": 0,
                "is_20d_rs_high": False, "anticipation_confirmed": False}
        base.update(kw)
        return base

    def test_leadership_change_in_threshold(self):
        snap = {"etfs": [
            self._row(etf="SMH", rank_delta_5d=-12, rs_score=80),  # qualifies
            self._row(etf="XLK", rank_delta_5d=-8,  rs_score=85),  # delta too small
            self._row(etf="GLD", rank_delta_5d=-15, rs_score=60),  # rs too low
        ]}
        sig = sr.signals(snap)
        self.assertEqual([r["etf"] for r in sig["in"]], ["SMH"])

    def test_leadership_decay_threshold(self):
        snap = {"etfs": [
            self._row(etf="REMX", rank_delta_5d=12, rs_score=20),  # qualifies
            self._row(etf="XHB",  rank_delta_5d=8,  rs_score=18),  # delta too small
            self._row(etf="JETS", rank_delta_5d=15, rs_score=55),  # rs too high
        ]}
        sig = sr.signals(snap)
        self.assertEqual([r["etf"] for r in sig["out"]], ["REMX"])


class DispersionTests(unittest.TestCase):
    def test_dispersion_calc(self):
        rows = [{"ret_1d": 0.01}, {"ret_1d": -0.02}, {"ret_1d": 0.03}, {"ret_1d": 0.0}]
        d = sr.universe_dispersion(rows)
        # pstdev of [0.01, -0.02, 0.03, 0.0]
        import statistics
        self.assertAlmostEqual(d, statistics.pstdev([0.01, -0.02, 0.03, 0.0]), places=6)

    def test_regime_classification(self):
        rows_narrow = [
            {"theme": "growth-narrow", "rs_score": 95},
            {"theme": "growth-narrow", "rs_score": 90},
            {"theme": "growth-narrow", "rs_score": 85},
            {"theme": "growth-narrow", "rs_score": 80},
            {"theme": "growth-narrow", "rs_score": 75},
        ]
        # high dispersion + SPY at 20d high → blow-off-risk
        self.assertEqual(sr.classify_regime(rows_narrow, 0.85, True), "blow-off-risk")
        # high dispersion no high → late-rotation
        self.assertEqual(sr.classify_regime(rows_narrow, 0.85, False), "late-rotation")
        # very low dispersion → correlation phase
        self.assertEqual(sr.classify_regime(rows_narrow, 0.10, False), "correlation_phase")


class BootstrappingRegimeTests(unittest.TestCase):
    def test_classify_regime_bootstrapping_when_history_thin(self):
        rows = [{"theme": "g", "rs_score": 95}] * 5
        # Fewer than MIN_HISTORY_DAYS_FOR_REGIME → bootstrapping regardless of inputs
        self.assertEqual(sr.classify_regime(rows, 1.0, True, history_days=1), "bootstrapping")
        self.assertEqual(sr.classify_regime(rows, 0.0, False, history_days=10), "bootstrapping")

    def test_classify_regime_normal_when_history_sufficient(self):
        rows = [{"theme": "g", "rs_score": 95}] * 5
        self.assertEqual(
            sr.classify_regime(rows, 0.85, True, history_days=sr.MIN_HISTORY_DAYS_FOR_REGIME),
            "blow-off-risk",
        )

    def test_classify_regime_back_compat_no_history_arg(self):
        rows = [{"theme": "g", "rs_score": 95}] * 5
        # Existing callers that don't pass history_days still work
        self.assertEqual(sr.classify_regime(rows, 0.10, False), "correlation_phase")

    def test_history_days_count_excludes_today(self):
        history = [
            {"date": "2026-05-06", "etf": "X"},
            {"date": "2026-05-06", "etf": "Y"},
            {"date": "2026-05-07", "etf": "X"},
            {"date": "2026-05-08", "etf": "X"},  # today
        ]
        self.assertEqual(sr.history_days_count(history, "2026-05-08"), 2)

    def test_bootstrapping_in_action_map(self):
        self.assertIn("bootstrapping", sr.REGIME_ACTIONS)


class RegimeActionTests(unittest.TestCase):
    def test_regime_action_lookup_covers_classifier_outputs(self):
        # Every tag classify_regime() can return must be in REGIME_ACTIONS.
        rows = [{"theme": "g", "rs_score": 95}] * 5
        produced = {
            sr.classify_regime(rows, 0.10, False),  # correlation_phase
            sr.classify_regime(rows, 0.30, False),  # early-rotation
            sr.classify_regime(rows, 0.60, False),  # mid-rotation
            sr.classify_regime(rows, 0.85, False),  # late-rotation
            sr.classify_regime(rows, 0.85, True),   # blow-off-risk
        }
        for tag in produced:
            self.assertIn(tag, sr.REGIME_ACTIONS, f"missing action for {tag}")
            for key in ("headline", "sizing", "entries", "held"):
                self.assertIn(key, sr.REGIME_ACTIONS[tag])

    def test_format_slack_includes_action_headline(self):
        snap = {
            "date": "2026-05-08",
            "regime": "mid-rotation",
            "dispersion_percentile_180d": 0.50,
            "etfs": [],
        }
        sig = {"in": [], "out": [], "anticipation": [], "decay": []}
        text = sr.format_slack(snap, sig)
        self.assertIn("Best entry tape", text)
        self.assertIn("Sizing:", text)
        self.assertIn("Entries:", text)
        self.assertIn("Held:", text)

    def test_format_slack_unknown_regime_no_crash(self):
        snap = {
            "date": "2026-05-08",
            "regime": "unknown",
            "dispersion_percentile_180d": 0.50,
            "etfs": [],
        }
        sig = {"in": [], "out": [], "anticipation": [], "decay": []}
        text = sr.format_slack(snap, sig)
        self.assertIn("`unknown`", text)
        # No action block injected for unknown tags
        self.assertNotIn("Sizing:", text)


if __name__ == "__main__":
    unittest.main()
