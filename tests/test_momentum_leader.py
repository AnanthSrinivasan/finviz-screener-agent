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


if __name__ == "__main__":
    unittest.main()
