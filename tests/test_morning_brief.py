"""Tests for the Morning Brief (agents/alerts/morning_brief.py — cx-rehaul §5).

Composition from fixture files, every-source-missing degradation, 7-line cap.
No network — compose only.
"""

import csv
import datetime
import json
import os
import tempfile
import unittest

from agents.alerts import morning_brief as mb

TODAY = datetime.date(2026, 7, 15)


def _write_json(d, name, obj):
    with open(os.path.join(d, name), "w") as f:
        json.dump(obj, f)


def _write_screener_csv(d, date_str, rows):
    cols = ["Ticker", "Company", "Sector", "Quality Score", "ATR%",
            "Dist From High%", "Rel Volume", "VCP", "SMA20%", "SMA50%", "SMA200%"]
    with open(os.path.join(d, f"finviz_screeners_{date_str}.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _qualifying_row(ticker="CRWD", q="84"):
    return {"Ticker": ticker, "Company": "Co", "Sector": "Tech",
            "Quality Score": q, "ATR%": "4", "Dist From High%": "-6",
            "Rel Volume": "1.0", "VCP": "80", "SMA20%": "2", "SMA50%": "8",
            "SMA200%": "20"}


def _full_fixture(d):
    _write_json(d, "market_monitor_2026-07-14.json", {
        "date": "2026-07-14", "market_state": "CAUTION",
        "cohort": {"label": "stress", "cohort_score": 34},
    })
    _write_json(d, "trading_state.json", {"current_sizing_mode": "normal"})
    _write_json(d, "etf_rotation.json", {
        "regime": "late-rotation",
        "money_line": {"text": "IN Memory, Cyber · OUT Cloud sw"},
    })
    _write_json(d, "positions.json", {"open_positions": [{"ticker": "DAVE"}]})
    _write_json(d, "paper_stops.json", {"BTSG": {"stop_price": 1},
                                        "HAPN": {"stop_price": 2}})
    _write_json(d, "live_alpaca_stops.json", {})
    _write_json(d, "earnings_upcoming.json",
                {"upcoming": [{"ticker": "TENB", "days_until": 0}]})
    _write_screener_csv(d, "2026-07-14", [_qualifying_row()])


class ComposeTests(unittest.TestCase):
    def test_full_composition_and_cap(self):
        with tempfile.TemporaryDirectory() as d:
            _full_fixture(d)
            lines = mb.compose_brief(d, today=TODAY)
        # header + 6 content lines = hard cap 7
        self.assertEqual(len(lines), mb.MAX_LINES)
        self.assertTrue(lines[0].startswith("☀️ Brief — "))
        joined = "\n".join(lines)
        self.assertIn("🚦 Gate:", joined)
        self.assertIn("index CAUTION", joined)
        self.assertIn("cohort STRESS (34)", joined)
        self.assertIn("💰 IN Memory, Cyber", joined)
        self.assertIn("📓 Book: 1 open · paper 2 · live 0", joined)
        self.assertIn("🎯 Today: CRWD Q84", joined)
        self.assertIn("📅 ER today/tomorrow: TENB", joined)
        self.assertIn("⚠️ Risk: regime late-rotation", joined)

    def test_gate_uses_gate_decision_overlays(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "market_monitor_2026-07-14.json",
                        {"date": "2026-07-14", "market_state": "GREEN"})
            _write_json(d, "trading_state.json", {"current_sizing_mode": "normal"})
            _write_json(d, "etf_rotation.json", {"regime": "blow-off-risk"})
            lines = mb.compose_brief(d, today=TODAY)
        joined = "\n".join(lines)
        # regime overlay tightens GREEN to NO NEW ENTRIES
        self.assertIn("NO NEW ENTRIES", joined)

    def test_every_source_missing_degrades_to_header(self):
        with tempfile.TemporaryDirectory() as d:
            lines = mb.compose_brief(d, today=TODAY)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("☀️ Brief"))

    def test_zero_qualified_is_patience_not_omitted(self):
        with tempfile.TemporaryDirectory() as d:
            _write_screener_csv(d, "2026-07-14", [])
            lines = mb.compose_brief(d, today=TODAY)
        self.assertIn("🎯 Today: 0 qualify — patience", "\n".join(lines))

    def test_held_excluded_from_today(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "positions.json",
                        {"open_positions": [{"ticker": "CRWD"}]})
            _write_screener_csv(d, "2026-07-14", [_qualifying_row("CRWD")])
            lines = mb.compose_brief(d, today=TODAY)
        self.assertIn("0 qualify", "\n".join(lines))

    def test_money_line_omitted_when_absent(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "etf_rotation.json", {"regime": "mid-rotation"})
            lines = mb.compose_brief(d, today=TODAY)
        joined = "\n".join(lines)
        self.assertNotIn("💰", joined)
        # mid-rotation is not a risk regime → no risk line either
        self.assertNotIn("⚠️ Risk", joined)

    def test_earnings_omitted_when_nothing_near(self):
        with tempfile.TemporaryDirectory() as d:
            _write_json(d, "earnings_upcoming.json",
                        {"upcoming": [{"ticker": "TENB", "days_until": 5}]})
            lines = mb.compose_brief(d, today=TODAY)
        self.assertNotIn("📅", "\n".join(lines))

    def test_build_message_appends_cockpit_link(self):
        with tempfile.TemporaryDirectory() as d:
            msg = mb.build_message(d, today=TODAY)
        self.assertIn("→ Cockpit: ", msg)
        self.assertIn("/data/daily.html", msg)

    def test_line_builder_crash_is_non_fatal(self):
        with tempfile.TemporaryDirectory() as d:
            # corrupt JSON must not raise
            with open(os.path.join(d, "etf_rotation.json"), "w") as f:
                f.write("{not json")
            lines = mb.compose_brief(d, today=TODAY)
        self.assertTrue(lines[0].startswith("☀️"))


if __name__ == "__main__":
    unittest.main()
