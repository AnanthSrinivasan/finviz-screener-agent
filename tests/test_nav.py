"""Tests for the shared nav bar (utils/generators/nav.py — cx-rehaul §A.1)."""

import os
import tempfile
import unittest

from utils.generators import nav


class RenderNavTests(unittest.TestCase):
    def test_renders_all_core_links_from_data_dir(self):
        html = nav.render_nav("cockpit")
        self.assertIn("site-nav", html)
        # data/ siblings link directly
        self.assertIn('href="daily.html', html)
        self.assertIn('href="etf_rotation.html', html)
        self.assertIn('href="live_portfolio.html', html)
        self.assertIn('href="claude_portfolio.html', html)
        self.assertIn('href="record.html', html)
        # root pages need ../
        self.assertIn('href="../watchlist.html', html)
        self.assertIn('href="../index.html', html)

    def test_at_root_prefixes_data_paths(self):
        html = nav.render_nav("home", at_root=True)
        self.assertIn('href="data/daily.html', html)
        self.assertIn('href="data/record.html', html)
        self.assertIn('href="watchlist.html', html)
        self.assertNotIn('href="../', html)

    def test_active_state_marked(self):
        html = nav.render_nav("flow")
        self.assertIn('class="nav-active"', html)
        # the active class sits on the Flow link only
        active_chunks = [a for a in html.split("<a ")[1:] if 'class="nav-active"' in a]
        self.assertEqual(len(active_chunks), 1)
        self.assertIn("etf_rotation.html", active_chunks[0])

    def test_no_active_when_key_unknown(self):
        html = nav.render_nav("")
        self.assertNotIn('class="nav-active"', html)

    def test_charts_links_latest_grid(self):
        old = nav.DATA_DIR
        try:
            with tempfile.TemporaryDirectory() as tmp:
                nav.DATA_DIR = tmp
                for d in ("2026-07-01", "2026-07-14"):
                    with open(os.path.join(tmp, f"finviz_chart_grid_{d}.html"), "w") as f:
                        f.write("x")
                html = nav.render_nav("charts")
                self.assertIn("finviz_chart_grid_2026-07-14.html", html)
                self.assertNotIn("finviz_chart_grid_2026-07-01.html", html)
        finally:
            nav.DATA_DIR = old

    def test_charts_item_omitted_when_no_grid(self):
        old = nav.DATA_DIR
        try:
            with tempfile.TemporaryDirectory() as tmp:
                nav.DATA_DIR = tmp
                html = nav.render_nav("cockpit")
                self.assertNotIn("Charts", html)
        finally:
            nav.DATA_DIR = old

    def test_mobile_friendly(self):
        html = nav.render_nav("cockpit")
        self.assertIn("overflow-x:auto", html)
        self.assertIn("44px", html)

    def test_self_contained_style(self):
        # Nav must carry its own CSS so legacy light pages render it correctly
        html = nav.render_nav("record")
        self.assertIn("<style>", html)
        self.assertIn("<nav", html)


if __name__ == "__main__":
    unittest.main()
