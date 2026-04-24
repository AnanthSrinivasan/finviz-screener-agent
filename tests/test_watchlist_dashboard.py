"""Unit tests for watchlist.html dashboard generator — new sections."""

import unittest

from utils.generators.generate_watchlist import (
    generate,
    _hg_row,
    _hg_table,
)


class TestWatchlistDashboard(unittest.TestCase):
    def _wl(self, priority, ticker="MU", **over):
        base = {
            "ticker": ticker,
            "status": "watching",
            "priority": priority,
            "added": "2026-04-09",
            "source": "screener_auto",
            "entry_note": "Stage 2 perfect",
            "thesis": "Tech | Q=100",
        }
        base.update(over)
        return base

    def _hg(self, ticker, score=4, criteria=None, **over):
        base = {
            "ticker": ticker,
            "signal_score": score,
            "criteria": criteria or {
                "persistence": True, "eps_yy_strong": True,
                "eps_qq_strong": True, "stage2_perfect": True,
                "inst_buying": False, "ipo_lifecycle": False,
            },
            "eps_yy_ttm": 100.0,
            "eps_qq": 150.0,
            "inst_trans": 5.0,
            "appearances": 3,
        }
        base.update(over)
        return base

    def test_entry_ready_section_rendered(self):
        wl = [self._wl("entry-ready", "MU")]
        html = generate(wl, {}, {"date": "2026-04-23", "candidates": []})
        self.assertIn("Ready to Enter", html)
        self.assertIn("tbl-entry-ready", html)
        self.assertIn("entry-ready-section", html)
        self.assertIn("MU", html)

    def test_hg_section_rendered_with_count(self):
        wl = []
        hg = {"date": "2026-04-23", "candidates": [self._hg("CORZ", 5), self._hg("VIK", 4)]}
        html = generate(wl, {}, hg)
        self.assertIn("Hidden Growth", html)
        self.assertIn("tbl-hidden-growth", html)
        self.assertIn("2026-04-23", html)
        self.assertIn("CORZ", html)
        self.assertIn("VIK", html)
        self.assertIn("5/6", html)
        self.assertIn("4/6", html)

    def test_hg_row_shows_tier_badge_when_ticker_in_watchlist(self):
        # VIK is entry-ready AND hidden growth — tier badge should appear in HG row
        tier_map = {"VIK": "entry-ready", "CORZ": "watching"}
        row_vik = _hg_row(self._hg("VIK"), tier_map)
        self.assertIn("ENTRY-READY", row_vik)
        row_corz = _hg_row(self._hg("CORZ"), tier_map)
        self.assertIn("WATCH", row_corz)

    def test_hg_row_no_tier_badge_when_not_in_watchlist(self):
        row = _hg_row(self._hg("NVTS"), {})
        # No badge for research-only HG hits
        self.assertNotIn("ENTRY-READY", row)
        self.assertNotIn("FOCUS", row)
        self.assertNotIn("WATCH<", row)  # <-- check the badge specifically (not the word "Watch")

    def test_hg_criteria_pills_on_vs_off(self):
        row = _hg_row(
            self._hg(
                "X",
                criteria={
                    "persistence": True,
                    "eps_yy_strong": False,
                    "eps_qq_strong": True,
                    "inst_buying": False,
                    "stage2_perfect": True,
                    "ipo_lifecycle": False,
                },
            ),
            {},
        )
        # True criteria → hg-pill-on; False → plain hg-pill
        self.assertEqual(row.count("hg-pill-on"), 3)  # persistence, eps_qq, stage2
        # Every criterion still renders a pill (lit or unlit), so 6 total occurrences of "hg-pill"
        self.assertEqual(row.count('class="hg-pill"'), 3)  # the 3 off ones

    def test_distorted_tag_in_hg_eps(self):
        # eps_yy < -50 and eps_qq > 0 → ⚠ distorted
        row = _hg_row(self._hg("SNDK", eps_yy_ttm=-328, eps_qq=618), {})
        self.assertIn("⚠", row)
        self.assertIn("TTM -328%", row)
        self.assertIn("Q/Q +618%", row)

    def test_empty_hg_section_shows_empty_message(self):
        html = generate([], {}, {"date": "2026-04-23", "candidates": []})
        self.assertIn("No Hidden Growth candidates today", html)

    def test_tiers_split_correctly(self):
        wl = [
            self._wl("entry-ready", "A"),
            self._wl("focus", "B"),
            self._wl("watching", "C"),
            self._wl("watching", "D", status="archived"),
        ]
        html = generate(wl, {}, {"date": "2026-04-23", "candidates": []})
        # Each ticker appears in its own section (ticker letters are unique enough)
        # Basic sanity: all 4 tickers show up somewhere
        for t in ["A", "B", "C", "D"]:
            self.assertIn(f">{t}</a>", html)

    def test_stat_cards_show_new_counts(self):
        wl = [
            self._wl("entry-ready", "A"),
            self._wl("focus", "B"),
            self._wl("watching", "C"),
        ]
        hg = {"date": "2026-04-23", "candidates": [self._hg("X"), self._hg("Y"), self._hg("Z")]}
        html = generate(wl, {}, hg)
        # Order in the HTML: Entry-Ready then Focus then Watching
        self.assertIn("Entry-Ready", html)
        self.assertIn("Hidden Growth", html)
        # 3 HG candidates → stat card should show 3
        self.assertIn(">3</span>", html)


if __name__ == "__main__":
    unittest.main()
