"""Unit tests for _is_rotation_catalyst predicate.

Spec: docs/specs/rotation-catalyst-block.md §1.
"""

import unittest
import pandas as pd

from agents.screener.finviz_agent import _is_rotation_catalyst


def _row(**kwargs):
    base = {
        "Ticker": "UMAC",
        "Sector": "Technology",
        "Industry": "Aerospace & Defense",
        "Stage": {"stage": 2, "perfect": True},
        "Dist From High%": -20.0,
        "SMA20%": 15.0,
        "SMA50%": 12.0,
        "ATR%": 7.5,
        "Rel Volume": 1.3,
    }
    base.update(kwargs)
    return pd.Series(base)


HOT_SNAPSHOT = {"UFO": {"rank": 1, "rank_delta_5d": -7, "rs_score": 96}}
COLD_SNAPSHOT = {"UFO": {"rank": 25, "rank_delta_5d": 5, "rs_score": 30}}


def _ufo_resolver(*_args, **_kw):
    return "UFO"


def _none_resolver(*_args, **_kw):
    return None


class TestRotationCatalyst(unittest.TestCase):

    def test_umac_05_27_fires(self):
        # UMAC 05-27: Stage 2 perfect, dist -26%, S20 +17%, RVol 1.36, ATR 9.14
        # peel-safe: sma50/atr = 16.71/9.14 = 1.83 < 6.5 (high-vol tier warn)
        row = _row(
            Ticker="UMAC",
            **{"Dist From High%": -26.09, "SMA20%": 17.36, "SMA50%": 16.71,
               "ATR%": 9.14, "Rel Volume": 1.36}
        )
        self.assertTrue(
            _is_rotation_catalyst(row, HOT_SNAPSHOT, set(), set(),
                                  ticker_to_etf=_ufo_resolver)
        )

    def test_onds_05_28_fires(self):
        # ONDS 05-28: Stage 2 perfect, dist -29%, S20 +12%, RVol 1.25, ATR 8.52
        # peel-safe: sma50%/atr% under high-vol tier warn
        row = _row(
            Ticker="ONDS",
            **{"Dist From High%": -29.32, "SMA20%": 12.3, "SMA50%": 14.0,
               "ATR%": 8.52, "Rel Volume": 1.25}
        )
        self.assertTrue(
            _is_rotation_catalyst(row, HOT_SNAPSHOT, set(), set(),
                                  ticker_to_etf=_ufo_resolver)
        )

    def test_extended_dist_above_zero_rejected(self):
        # UMAC 05-28 hypothetical extended past 52w high (+5%)
        row = _row(
            Ticker="UMAC",
            **{"Dist From High%": 5.0, "SMA20%": 30.0}
        )
        self.assertFalse(
            _is_rotation_catalyst(row, HOT_SNAPSHOT, set(), set(),
                                  ticker_to_etf=_ufo_resolver)
        )

    def test_no_parent_etf_rejected(self):
        row = _row()
        self.assertFalse(
            _is_rotation_catalyst(row, HOT_SNAPSHOT, set(), set(),
                                  ticker_to_etf=_none_resolver)
        )

    def test_cold_sector_rejected(self):
        # Clean Stage 2 setup but parent ETF is cold (rank 25, delta +5) — reject.
        row = _row()
        self.assertFalse(
            _is_rotation_catalyst(row, COLD_SNAPSHOT, set(), set(),
                                  ticker_to_etf=_ufo_resolver)
        )

    def test_close_below_sma20_rejected(self):
        # No reclaim — close below SMA20.
        row = _row(**{"SMA20%": -1.0})
        self.assertFalse(
            _is_rotation_catalyst(row, HOT_SNAPSHOT, set(), set(),
                                  ticker_to_etf=_ufo_resolver)
        )

    def test_held_rejected(self):
        row = _row(Ticker="UMAC")
        self.assertFalse(
            _is_rotation_catalyst(row, HOT_SNAPSHOT, {"UMAC"}, set(),
                                  ticker_to_etf=_ufo_resolver)
        )


if __name__ == "__main__":
    unittest.main()
