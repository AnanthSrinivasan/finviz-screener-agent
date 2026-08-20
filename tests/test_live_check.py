"""utils/live_check.py — the anti-stale-data tool.

Exists because on 2026-08-19 XBI was reported to the user as "RS 51, rank 22,
mid-pack" from a cached etf_rotation.json snapshot while it was actually
breaking to RS 86 / rank 6. A stale read presented as a current fact cost a
real trade. This module fetches live and stamps every cached value with age.
"""
import unittest

from utils.live_check import atr_multiple, tier_for, _age_days, render


class TierTests(unittest.TestCase):
    def test_low_vol_tier(self):
        self.assertEqual(tier_for(2.43), (3.0, 4.0))
        self.assertEqual(tier_for(4.0), (3.0, 4.0))

    def test_mid_vol_tier(self):
        self.assertEqual(tier_for(6.59), (5.0, 6.0))

    def test_high_vol_tier(self):
        self.assertEqual(tier_for(8.28), (6.5, 8.0))

    def test_extreme_tier(self):
        self.assertEqual(tier_for(12.0), (8.5, 10.0))


class AtrMultipleTests(unittest.TestCase):
    def test_matches_tradingview_formula(self):
        # ARKG 2026-08-19: SMA50 +18.5%, ATR 3.33% -> 5.55
        self.assertAlmostEqual(atr_multiple(47.7, 18.5, 3.33), 5.55, places=1)

    def test_zero_atr_is_safe(self):
        self.assertEqual(atr_multiple(100.0, 20.0, 0.0), 0.0)

    def test_missing_price_is_safe(self):
        self.assertEqual(atr_multiple(0.0, 20.0, 3.0), 0.0)


class StalenessStampTests(unittest.TestCase):
    def test_old_date_is_marked_stale(self):
        self.assertIn("STALE", _age_days("2026-01-01"))

    def test_unparseable_date_still_flagged(self):
        self.assertIn("age unknown", _age_days("not-a-date"))

    def test_stamp_always_includes_the_date(self):
        self.assertIn("2026-08-19", _age_days("2026-08-19"))


class RenderTests(unittest.TestCase):
    def _r(self, **kw):
        base = dict(ticker="XBI", atr_pct=2.43, dist_high=-0.5, rel_vol=0.03,
                    sma20=8.7, sma50=10.6, sma200=27.5, perf_month=11.0,
                    perf_quarter=28.2, perf_year=89.3, mult=4.38,
                    warn=3.0, signal=4.0, status="PAST SIGNAL")
        base.update(kw)
        return base

    def test_render_labels_live_timestamp(self):
        self.assertIn("LIVE @", render(self._r()))

    def test_error_row_renders_without_crashing(self):
        self.assertIn("no data", render({"ticker": "ZZZ", "error": "no data"}))


if __name__ == "__main__":
    unittest.main()
