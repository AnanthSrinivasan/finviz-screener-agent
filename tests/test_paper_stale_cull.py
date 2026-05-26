"""
Unit tests for paper stale-cull (agents/trading/alpaca_monitor.check_stale_position).
"""

import datetime
import unittest

from agents.trading import alpaca_monitor as am


class CheckStalePositionTests(unittest.TestCase):
    TODAY = datetime.date(2026, 5, 26)

    def _stop_info(self, entry_date, peak=0.0, **kw):
        d = {"entry_price": 100.0, "entry_date": entry_date,
             "peak_gain_pct": peak, "t1_peeled": False}
        d.update(kw)
        return d

    def test_14d_flat_peak_below_4_is_stale(self):
        # Entered 14d ago, peak +3.5% → stale
        entry = (self.TODAY - datetime.timedelta(days=14)).isoformat()
        is_stale, days, peak = am.check_stale_position(
            self._stop_info(entry, peak=3.5), today=self.TODAY)
        self.assertTrue(is_stale)
        self.assertEqual(days, 14)
        self.assertAlmostEqual(peak, 3.5)

    def test_13d_below_threshold_not_stale(self):
        entry = (self.TODAY - datetime.timedelta(days=13)).isoformat()
        is_stale, *_ = am.check_stale_position(
            self._stop_info(entry, peak=3.5), today=self.TODAY)
        self.assertFalse(is_stale)

    def test_14d_but_peak_above_4_not_stale(self):
        entry = (self.TODAY - datetime.timedelta(days=14)).isoformat()
        is_stale, *_ = am.check_stale_position(
            self._stop_info(entry, peak=5.0), today=self.TODAY)
        self.assertFalse(is_stale)

    def test_t1_peeled_position_never_stale(self):
        # Even at 30d and peak < 4 (impossible after T1 in practice, but defensive)
        entry = (self.TODAY - datetime.timedelta(days=30)).isoformat()
        is_stale, *_ = am.check_stale_position(
            self._stop_info(entry, peak=0.0, t1_peeled=True), today=self.TODAY)
        self.assertFalse(is_stale)

    def test_missing_entry_date_not_stale(self):
        is_stale, *_ = am.check_stale_position(
            {"entry_price": 100.0, "peak_gain_pct": 0.0}, today=self.TODAY)
        self.assertFalse(is_stale)

    def test_malformed_entry_date_not_stale(self):
        is_stale, *_ = am.check_stale_position(
            self._stop_info("not-a-date"), today=self.TODAY)
        self.assertFalse(is_stale)


if __name__ == "__main__":
    unittest.main()
