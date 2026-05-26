"""
Unit tests for live stale-entry alert in agents/trading/position_monitor.
Alert-only — must NOT place sells. Live system is human-driven by design.
"""

import datetime
import unittest

from agents.trading import position_monitor as pm
from agents.trading import rules


class CheckLiveStaleEntryTests(unittest.TestCase):
    TODAY = datetime.date(2026, 5, 26)

    def _pos(self, entry_date, peak=0.0, **kw):
        d = {"ticker": "FOO", "entry_price": 100.0,
             "entry_date": entry_date, "peak_gain_pct": peak,
             "status": "active"}
        d.update(kw)
        return d

    def test_14d_flat_emits_stale_event(self):
        entry = (self.TODAY - datetime.timedelta(days=14)).isoformat()
        pos = self._pos(entry, peak=2.0)
        ev = pm.check_live_stale_entry(pos, today=self.TODAY)
        self.assertIsNotNone(ev)
        self.assertEqual(ev["kind"], "stale_entry")
        self.assertEqual(ev["ticker"], "FOO")
        self.assertEqual(ev["days_open"], 14)
        self.assertIn("STALE", ev["message"])
        # Dedup field written
        self.assertEqual(pos["stale_alerted_date"], self.TODAY.isoformat())

    def test_13d_no_alert(self):
        entry = (self.TODAY - datetime.timedelta(days=13)).isoformat()
        ev = pm.check_live_stale_entry(self._pos(entry, peak=2.0), today=self.TODAY)
        self.assertIsNone(ev)

    def test_peak_above_4pct_no_alert(self):
        entry = (self.TODAY - datetime.timedelta(days=20)).isoformat()
        ev = pm.check_live_stale_entry(self._pos(entry, peak=4.5), today=self.TODAY)
        self.assertIsNone(ev)

    def test_dedup_same_day(self):
        entry = (self.TODAY - datetime.timedelta(days=15)).isoformat()
        pos = self._pos(entry, peak=1.0)
        first  = pm.check_live_stale_entry(pos, today=self.TODAY)
        second = pm.check_live_stale_entry(pos, today=self.TODAY)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_kind_in_critical_event_kinds(self):
        # Routes to immediate Slack post, not book digest
        self.assertIn("stale_entry", rules.CRITICAL_EVENT_KINDS)

    def test_does_not_call_sell(self):
        # No external broker side effect — the function is pure metadata.
        # Verify by inspecting return shape (alert-only).
        entry = (self.TODAY - datetime.timedelta(days=14)).isoformat()
        ev = pm.check_live_stale_entry(self._pos(entry, peak=0.0), today=self.TODAY)
        self.assertEqual(set(ev.keys()),
                         {"kind", "ticker", "days_open", "peak_gain_pct", "message"})


if __name__ == "__main__":
    unittest.main()
