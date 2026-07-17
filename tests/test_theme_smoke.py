"""Theme-migration smoke tests (cx-rehaul §7): every migrated generator is
import-safe and emits the shared nav + the BASE_CSS marker."""

import tempfile
import unittest

MARKER = "BASE_CSS v1"
NAV = "site-nav"


class ThemeSmokeTests(unittest.TestCase):
    def test_theme_module(self):
        from utils.generators.theme import BASE_CSS, page_shell
        self.assertIn(MARKER, BASE_CSS)
        html = page_shell("T", "<nav class='site-nav'></nav>", "<p>x</p>",
                          h1="H", subtitle="s")
        self.assertIn(MARKER, html)
        self.assertIn(NAV, html)
        self.assertIn("<h1>H</h1>", html)

    def test_index(self):
        from utils.generators.generate_index import generate_index
        reports = {"weekly": [], "daily_gallery": [], "daily_summary": [],
                   "persistence": [], "trader_mirror": []}
        html = generate_index(reports, "")
        self.assertIn(MARKER, html)
        self.assertIn(NAV, html)
        self.assertIn("Open Cockpit", html)
        self.assertIn("Report Archive", html)
        # exactly one hero button — the ten others are gone
        self.assertEqual(html.count('class="btn-cockpit"'), 1)
        for gone in ("hero-btn", "btn-dash", "btn-perf", "btn-mae"):
            self.assertNotIn(gone, html)

    def test_index_archive_keeps_every_report(self):
        from utils.generators.generate_index import generate_index
        reports = {
            "weekly": [{"date": "2026-07-11", "file": "finviz_weekly_2026-07-11.html", "path": ""}],
            "daily_gallery": [{"date": "2026-07-14", "file": "finviz_chart_grid_2026-07-14.html", "path": ""}],
            "daily_summary": [{"date": "2026-07-14", "file": "finviz_screeners_2026-07-14.html", "path": ""}],
            "persistence": [{"date": "2026-07-11", "file": "finviz_weekly_persistence_2026-07-11.csv", "path": ""}],
            "trader_mirror": [{"date": "2026-06", "file": "trader_mirror_2026-06.html", "path": ""}],
        }
        html = generate_index(reports, "")
        for fname in ("finviz_weekly_2026-07-11.html",
                      "finviz_chart_grid_2026-07-14.html",
                      "finviz_screeners_2026-07-14.html",
                      "finviz_weekly_persistence_2026-07-11.csv",
                      "trader_mirror_2026-06.html",
                      "performance_2026.html", "performance_charts.html",
                      "mae_analysis.html", "dashboard.html"):
            self.assertIn(fname, html, fname)

    def test_cockpit(self):
        from utils.generators import generate_daily_cockpit as cp
        gate = cp.gate_decision("CAUTION", "mid-rotation", "normal")
        ctx = {
            "market": {}, "trading_state": {}, "rotation": {}, "gate": gate,
            "book_rows": [], "account": {}, "equity": 0.0,
            "qualified": [], "radar": {"entry-ready": [], "focus": []},
            "record": cp.record_stats({}, []),
        }
        html = cp.render_page(ctx)
        self.assertIn(MARKER, html)
        self.assertIn(NAV, html)
        self.assertIn("cohort-pill", html)  # cohort CSS preserved

    def test_etf_rotation(self):
        from agents.sector_rotation import render_etf_rotation_html
        html = render_etf_rotation_html({"date": "2026-07-15", "regime": "mid-rotation"}, [])
        self.assertIn(MARKER, html)
        self.assertIn(NAV, html)

    def test_watchlist(self):
        from utils.generators.generate_watchlist import generate
        html = generate([], {}, {"date": "2026-07-15", "candidates": []})
        self.assertIn(MARKER, html)
        self.assertIn(NAV, html)

    def test_live_portfolio(self):
        from utils.generators.generate_live_portfolio import render_html
        html = render_html({"equity": 1000, "cash": 0, "buying_power": 0}, [])
        self.assertIn(MARKER, html)
        self.assertIn(NAV, html)

    def test_paper_portfolio(self):
        from utils.generators.generate_portfolio import generate_html
        html = generate_html({"equity": 1000}, [], {})
        self.assertIn(MARKER, html)
        self.assertIn(NAV, html)

    def test_record(self):
        from utils.generators import generate_record as gr
        with tempfile.TemporaryDirectory() as tmp:
            html = gr.render_page(tmp)
        self.assertIn(MARKER, html)
        self.assertIn(NAV, html)
        for label in ("Performance 2026 YTD", "Performance 2024–25",
                      "MAE / MFE", "Trader Mirror"):
            self.assertIn(label, html)

    def test_chart_grid_and_weekly_nav_helpers(self):
        from agents.screener.finviz_agent import _shared_nav_html
        from agents.screener.finviz_weekly_agent import _shared_nav
        self.assertIn(NAV, _shared_nav_html())
        self.assertIn(NAV, _shared_nav())


if __name__ == "__main__":
    unittest.main()
